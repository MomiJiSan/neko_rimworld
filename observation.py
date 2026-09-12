"""Detached observations shared by model input and run evidence; no domain projection."""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any

from .protocol import Envelope
from .state import utc_now


@dataclass(frozen=True, slots=True)
class Observation:
    value: dict[str, Any]

    @classmethod
    def from_response(cls, operation: str, arguments: dict[str, Any], response: Envelope) -> Observation:
        return cls(
            {
                # A compact run-local reference is easier for the model to copy.
                # The envelope retains its full request/connection/session identities.
                "observation_id": uuid.uuid4().hex[:12],
                "received_at": utc_now(),
                "request": {"operation": operation, "arguments": copy.deepcopy(arguments)},
                "response": response.to_dict(),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.value)
