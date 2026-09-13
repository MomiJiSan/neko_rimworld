"""Request-scoped agent session state; never persisted or reused as memory."""

from __future__ import annotations

import json

from dataclasses import dataclass, field
from typing import Any

from .audit import AgentAuditLedger
from .contracts import AssistantTurn
from .handoff import SubmissionDraft


def project_read_batches(messages, read_tools):
    """Project completed read batches, including failures, without closing tools.

    Writing/mixed/unknown-tool batches and malformed arguments stay native so
    their protocol and correction context are never partially dismantled.
    """
    projected = []
    index = 0
    while index < len(messages):
        message = messages[index]
        calls = message.get("tool_calls", [])
        if message.get("role") != "assistant" or not calls or any(
                c["function"]["name"] not in read_tools for c in calls):
            projected.append(message); index += 1; continue
        results = messages[index+1:index+1+len(calls)]
        identities = [c["id"] for c in calls]
        if (len(set(identities)) != len(identities) or len(results) != len(calls)
                or any(r.get("role") != "tool" for r in results)
                or {r.get("tool_call_id") for r in results} != set(identities)):
            raise ValueError("Completed read batch has missing or ambiguous tool results")
        try:
            arguments = [json.loads(c["function"]["arguments"]) for c in calls]
            if not all(isinstance(a,dict) for a in arguments): raise ValueError("Invalid read arguments")
        except (ValueError,TypeError):
            projected.extend([message,*results]);index += 1+len(calls);continue
        by_id = {r["tool_call_id"]:r for r in results}
        evidence = [dict(operation=c["function"]["name"], arguments=a,
                         result=json.loads(by_id[c["id"]]["content"])) for c,a in zip(calls,arguments)]
        if message.get("content"):
            retained = dict(message);retained.pop("tool_calls")
            projected.append(retained)
        projected.append({"role":"user", "content":json.dumps({"observations":evidence},ensure_ascii=False,separators=(",", ":"))})
        index += 1+len(calls)
    return projected


@dataclass(slots=True)
class AgentRunSession:
    ledger: AgentAuditLedger
    messages: list[dict[str, Any]]
    model_calls: int = 0
    tool_calls: int = 0
    observations: list[dict[str, Any]] = field(default_factory=list)
    last_assistant_content: str = ""
    submission: SubmissionDraft = field(default_factory=SubmissionDraft)
    interaction: Any = None  # Optional per-run adapter; never shared by the catalog cache.
    finalization_started: bool = False
    read_tools: frozenset[str] = frozenset()

    @property
    def fallback_answer(self) -> str:
        if self.submission.full_text:
            return self.submission.full_text
        if self.submission.revision or self.submission.errors:
            return "The consultation has an unfinished submission. Its saved inputs require correction or confirmation."
        return self.last_assistant_content

    @classmethod
    def create(cls, *, ledger: AgentAuditLedger, system_prompt: str, question: str) -> AgentRunSession:
        return cls(
            ledger=ledger,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
        )

    def request_messages(self, advisory: str) -> list[dict[str, Any]]:
        messages = [
            {"role": "system", "content": f"{self.messages[0]['content']}\n\n{advisory}"},
            *self.messages[1:],
        ]
        if self.read_tools and self.interaction is None:
            return project_read_batches(messages, self.read_tools)
        return messages

    def append_assistant(self, turn: AssistantTurn) -> None:
        self.messages.append(turn.as_message())
        if turn.content:
            self.last_assistant_content = turn.content

    def append_tool_message(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    def append_recovery(self, message: str) -> None:
        self.messages.append({"role": "user", "content": message})


__all__ = ["AgentRunSession", "project_read_batches"]
