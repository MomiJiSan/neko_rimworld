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
