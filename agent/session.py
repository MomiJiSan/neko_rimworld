"""Request-scoped agent session state; never persisted or reused as memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .audit import AgentAuditLedger
from .contracts import AssistantTurn
from .handoff import SubmissionDraft


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

    @property
    def fallback_answer(self) -> str:
        # A claim rejection never commits text, so a blocked draft must not fall
        # through to the model's last raw message either: that message is the text
        # the check refused.
        if self.submission.claims_blocked and not self.submission.full_text:
            return "The consultation's draft answer was refused for unsupported claims; review the full result in the plugin panel."
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
        return [
            {"role": "system", "content": f"{self.messages[0]['content']}\n\n{advisory}"},
            *self.messages[1:],
        ]

    def append_assistant(self, turn: AssistantTurn) -> None:
        self.messages.append(turn.as_message())
        if turn.content:
            self.last_assistant_content = turn.content

    def append_tool_message(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    def append_recovery(self, message: str) -> None:
        self.messages.append({"role": "user", "content": message})


__all__ = ["AgentRunSession"]
