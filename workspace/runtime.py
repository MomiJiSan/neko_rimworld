"""Prepare persistent work views, then use the plugin's shared bounded executor."""

import asyncio
import re
import sqlite3

from ..agent.audit import AgentAuditLedger
from ..agent.audit import private_protocol_redacted as public_protocol
from ..agent.prompt import build_system_prompt
from ..agent.session import AgentRunSession
from ..reports import plain
from .history_tools import make_history_tool
from .context import history_message


class WorkspaceAgent:
    def __init__(self, storage, runtime, *, history_chars=12000, report_store=None):
        self.storage = storage
        self.runtime = runtime
        self.history_chars = history_chars
        self.report_store = report_store
        self._pending_history = {}

    async def retry_history(self, workspace_id, owner):
        """Settle a completed exchange before accepting another input; never rerun it."""
        pending = self._pending_history.get(workspace_id)
        if pending is not None:
            saved_owner, turn_id, result, trace = pending
            if owner != saved_owner:
                raise ValueError("Unconfirmed history belongs to another owner")
            turn = await self.storage.call("turn", workspace_id, owner, turn_id)
            # A cancelled await may have completed the owned SQLite transaction.
            if turn["status"] == "running":
                await self.storage.call(
                    "finish", workspace_id, owner, turn_id, result, audit_record=trace, wait_for_input=True
                )
            self._pending_history.pop(workspace_id, None)

    def pending_result(self, workspace_id, turn_id):
        pending = self._pending_history.get(workspace_id)
        return public_protocol(pending[2]) if pending is not None and pending[1] == turn_id else None

    async def execute(self, workspace_id, owner, turn_id, settings, *, colony_id=None):
        checkpoint = await self.storage.call("context_checkpoint", workspace_id, owner, before_turn=turn_id)
        history = await self.storage.call(
            "history",
            workspace_id,
            owner,
            before_turn=turn_id,
            after_sequence=checkpoint["through_sequence"] if checkpoint else 0,
        )
        if checkpoint is not None:
            history["checkpoint"] = checkpoint
            history["anchor_records"] = [
                await self.storage.call("read", workspace_id, owner, a["turn_id"], observation_id=a["observation_id"])
                for a in checkpoint["data"]["anchors"]
            ]
        context = history_message(history, max_chars=self.history_chars)
        question = (await self.storage.call("turn", workspace_id, owner, turn_id))["question"]
        has_history = bool(history["entries"] or history.get("has_more") or checkpoint)
        explicit_report = re.search(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", question)
        capabilities = (make_history_tool(
            storage=self.storage, workspace_id=workspace_id, owner=owner, turn_id=turn_id,
            report_store=self.report_store, question=question,
        ),) if has_history or explicit_report else ()

        session = AgentRunSession.create(
            ledger=AgentAuditLedger(question, workspace_id=workspace_id, turn_id=turn_id),
            question=question,
            system_prompt=build_system_prompt(settings.preferred_cycles),
            context_messages=(context,) if context is not None else (),
            workspace_id=workspace_id,
            colony_id=colony_id,
        )
        session.capabilities = capabilities
        runtime = self.runtime
        try:
            await self.storage.call("start", workspace_id, owner, turn_id)
            result = await runtime.execute(session, settings)
        except asyncio.CancelledError:
            last = session.ledger.snapshot()
            result = session.result
            if result is None:
                result = plain(last["result"]) if last and last.get("result") else {"status": "cancelled"}
            await self.storage.call("abort", workspace_id, owner, turn_id, public_protocol(result), audit_record=last)
            raise
        except Exception:
            await self.storage.call("abort", workspace_id, owner, turn_id, {"status": "failed"})
            raise
        trace = session.ledger.snapshot()
        retained_result = public_protocol(result)
        # Retain the completed exchange before awaiting its owned transaction.
        self._pending_history[workspace_id] = (owner, turn_id, retained_result, trace)
        try:
            await self.storage.call(
                "finish", workspace_id, owner, turn_id, retained_result, audit_record=trace, wait_for_input=True
            )
        except (OSError, sqlite3.Error):
            return {
                "workspace_id": workspace_id,
                "turn_id": turn_id,
                "result": result,
                "audit": trace,
                "history_saved": False,
                "checkpoint": None,
            }
        self._pending_history.pop(workspace_id, None)
        return {
            "workspace_id": workspace_id,
            "turn_id": turn_id,
            "result": result,
            "audit": trace,
            "checkpoint": None,
            "history_saved": True,
        }
