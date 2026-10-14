"""Offline, worker-owned tokenization independent of tiktoken's registry lock.

Keep a TokenCounter for an entire validation operation. Only successful encoders
are cached; a failed attempt returns a strict byte bound and can recover later.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)
_BPE_ASSET = Path(__file__).with_name("data") / "o200k_base.tiktoken"
_BPE_HASH = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
_MAX_BPE_BYTES = 4 * 1024 * 1024
_INITIALIZATION_SECONDS = 4.0
_RETRY_SECONDS = 5.0

# o200k_base specification from OpenAI's MIT-licensed tiktoken0.12.0:
# https://github.com/openai/tiktoken/blob/0.12.0/tiktoken_ext/openai_public.py
# Copyright (c) 2022 OpenAI, Shantanu Jain. See data/LICENSE.tiktoken.
# The bundled vocabulary is pinned by the official resource's SHA256 above.
_O200K_PATTERN = "|".join(
    [
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"\p{N}{1,3}",
        r" ?[^\s\p{L}\p{N}]+[\r\n/]*",
        r"\s*[\r\n]+",
        r"\s+(?!\S)",
        r"\s+",
    ]
)
_O200K_SPECIAL_TOKENS = {"<|endoftext|>": 199999, "<|endofprompt|>": 200018}


@dataclass(frozen=True, slots=True)
class TokenCounter:
    """An immutable choice of metric, including a safe fallback snapshot."""

    encoding: Any | None = None
    failure_reason: str | None = None

    def count(self, text: str) -> tuple[int, str]:
        # User/model prose containing special-token spellings is ordinary text.
        if self.encoding is not None:
            return len(self.encoding.encode_ordinary(text)), "o200k_base"
        return len(text.encode("utf-8", "replace")), "utf8_bytes_upper_bound"


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("tokenizer initialization budget expired")


def _create_counter(*, asset: Path | None = None, budget: float = _INITIALIZATION_SECONDS) -> TokenCounter:
    import tiktoken

    deadline = time.monotonic() + budget
    # Read at most the finite resource bound, even if an installation is corrupt.
    with (asset if asset is not None else _BPE_ASSET).open("rb") as resource:
        data = resource.read(_MAX_BPE_BYTES + 1)
    _check_deadline(deadline)
    if len(data) > _MAX_BPE_BYTES:
        raise ValueError("tokenizer resource exceeds size limit")
    if hashlib.sha256(data).hexdigest() != _BPE_HASH:
        raise ValueError("tokenizer resource hash mismatch")
    ranks: dict[bytes, int] = {}
    for index, line in enumerate(data.splitlines()):
        if index % 4096 == 0:
            _check_deadline(deadline)
        if line:
            token, rank = line.split()
            ranks[base64.b64decode(token, validate=True)] = int(rank)
    # Never call get_encoding: another host component can own its registry lock
    # while doing an unbounded network download. This private encoder is offline.
    encoding = tiktoken.Encoding(
        name="o200k_base", pat_str=_O200K_PATTERN, mergeable_ranks=ranks,
        special_tokens=dict(_O200K_SPECIAL_TOKENS),
    )
    _check_deadline(deadline)
    return TokenCounter(encoding)


class _TokenCounterProvider:
    def __init__(
        self, initializer: Callable[[], TokenCounter] = _create_counter, *,
        retry_seconds: float = _RETRY_SECONDS, clock: Callable[[], float] = time.monotonic,
        wait_seconds: float = _INITIALIZATION_SECONDS,
    ):
        self._initializer = initializer
        self._retry_seconds = retry_seconds
        self._wait_seconds = wait_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._success: TokenCounter | None = None
        self._failure = TokenCounter(failure_reason="initialization_requires_worker")
        self._retry_at = 0.0
        self._inflight: Future[TokenCounter] | None = None
        self._worker: threading.Thread | None = None

    def snapshot(self) -> TokenCounter:
        with self._lock:
            return self._success if self._success is not None else self._failure

    def _publish(self, future: Future[TokenCounter], counter: TokenCounter) -> None:
        with self._lock:
            if counter.encoding is not None:
                self._success = counter
            else:
                self._failure = counter
                self._retry_at = self._clock() + self._retry_seconds
        # Future callbacks may enter another loop/provider. Never run them
        # while holding our state lock.
        future.set_result(counter)

    def _initialize(self, future: Future[TokenCounter]) -> None:
        try:
            counter = self._initializer()
        except Exception as error:
            counter = TokenCounter(failure_reason=type(error).__name__)
            _LOGGER.warning("RimWorld tokenizer initialization failed (%s); using a byte bound and retrying later", type(error).__name__)
        self._publish(future, counter)

    def request(self) -> Future[TokenCounter]:
        with self._lock:
            if self._success is not None or self._clock() < self._retry_at:
                result: Future[TokenCounter] = Future()
                result.set_result(self._success if self._success is not None else self._failure)
                return result
            if self._inflight is not None and not self._inflight.done():
                return self._inflight
            future: Future[TokenCounter] = Future()
            self._inflight = future
            worker = threading.Thread(target=self._initialize, args=(future,), name="rimworld-tokenizer-init")
            self._worker = worker
        try:
            worker.start()
        except RuntimeError as error:
            self._publish(future, TokenCounter(failure_reason=type(error).__name__))
        return future

    async def acquire(self) -> TokenCounter:
        # Cancellation belongs to the consumer, not shared initialization.
        try:
            async with asyncio.timeout(self._wait_seconds):
                return await asyncio.shield(asyncio.wrap_future(self.request()))
        except TimeoutError:
            # A slow filesystem/native constructor is not interruptible. Keep
            # at most this one worker and let later consumers observe its result.
            return TokenCounter(failure_reason="initialization_wait_expired")


def _current_process_id() -> int:
    return os.getpid()


_PROVIDER = _TokenCounterProvider()
_PROVIDER_PID = _current_process_id()
_CHILD_PROVIDERS: dict[int, _TokenCounterProvider] = {}


def _get_current_provider() -> _TokenCounterProvider:
    global _PROVIDER, _PROVIDER_PID
    pid = _current_process_id()
    if pid != _PROVIDER_PID:
        # A fork copies locks/futures, but not the thread that can finish them.
        # Never acquire the inherited provider's lock to reset its ownership.
        # setdefault selects one provider even for concurrent first child calls.
        provider = _CHILD_PROVIDERS.setdefault(pid, _TokenCounterProvider())
        _PROVIDER = provider
        _PROVIDER_PID = pid
        return provider
    return _PROVIDER


async def acquire_token_counter() -> TokenCounter:
    return await _get_current_provider().acquire()


def get_token_counter() -> TokenCounter:
    """Cold sync callers must be workers; a loop caller only reads a snapshot."""
    provider = _get_current_provider()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return provider.request().result(timeout=provider._wait_seconds)
        except FutureTimeout:
            return TokenCounter(failure_reason="initialization_wait_expired")
    return provider.snapshot()
