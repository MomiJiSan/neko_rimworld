"""Cumulative public-input admission; this is not provider billing telemetry."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .handoff import _tokenizer


def public_input_tokens(messages, tools):
    encoder = _tokenizer()
    def count(value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        return len(encoder.encode(text, disallowed_special=())) if encoder else len(text.encode('utf-8'))
    total = count(tools)
    for message in messages:
        total += count(message.get('content') or '')
        total += sum(count(call.get('function', {})) for call in message.get('tool_calls', []))
    return total


@dataclass
class InputBudget:
    limit: int | None = None
    used: int = 0

    @classmethod
    def from_environment(cls):
        raw = os.environ.get('NEKO_PUBLIC_INPUT_TOKEN_LIMIT', '').strip()
        if not raw:
            return cls()
        if not raw.isdecimal() or not 1 <= int(raw) <= 100_000_000:
            raise ValueError('NEKO_PUBLIC_INPUT_TOKEN_LIMIT must be an integer in 1..100000000')
        return cls(int(raw))

    def admit(self, messages, tools):
        if self.limit is None:
            return True, None
        cost = public_input_tokens(messages, tools)
        allowed = self.used + cost <= self.limit
        if allowed:
            self.used += cost
        return allowed, dict(public_input_tokens=cost, cumulative_public_input_tokens=self.used,
                             public_input_limit=self.limit, admitted=allowed,
                             private_continuation_and_provider_framing_counted=False)
