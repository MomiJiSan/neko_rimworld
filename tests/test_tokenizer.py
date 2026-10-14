from __future__ import annotations

import asyncio
import base64
import hashlib
import shutil
import threading
from dataclasses import FrozenInstanceError
from types import FunctionType
from typing import Any

import pytest

from plugin.plugins.neko_rimworld.agent import tokenizer


def _join(provider: tokenizer._TokenCounterProvider) -> None:
    if provider._worker is not None:
        provider._worker.join(2)
        assert not provider._worker.is_alive()


def _canonical_encoding() -> Any:
    """Use the installed official constructor, replacing only its resource read.

    No registry/public resource fetch/cache mutation; the original constructor
    determines pattern/special tokens independently of this plugin's constants.
    """
    tiktoken = pytest.importorskip("tiktoken")
    from tiktoken_ext import openai_public

    data = tokenizer._BPE_ASSET.read_bytes()
    assert hashlib.sha256(data).hexdigest() == tokenizer._BPE_HASH
    ranks = {base64.b64decode(token): int(rank) for token, rank in (line.split() for line in data.splitlines())}
    original = openai_public.o200k_base

    def local_resource(url: str, expected_hash: str) -> dict[bytes, int]:
        assert url == "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
        assert expected_hash == tokenizer._BPE_HASH
        return ranks

    constructor = FunctionType(original.__code__, {**original.__globals__, "load_tiktoken_bpe": local_resource})
    return tiktoken.Encoding(**constructor())


def test_bundled_resource_license_and_canonical_counts() -> None:
    pytest.importorskip("tiktoken")
    canonical = _canonical_encoding()
    counter = tokenizer._create_counter()
    assert counter.encoding._pat_str == canonical._pat_str
    assert counter.encoding._special_tokens == canonical._special_tokens
    assert counter.encoding._mergeable_ranks == canonical._mergeable_ranks
    for text in [
        "殖民地 stable 123", "こんにちは世界", "Привет, мир", "Olá mundo", "é e\u0301",
        "\r\n\t 1234567890\n", "It's we're they've that'll I'd", "🙂👩‍🚀", "<|endoftext|> <|endofprompt|>",
        "", "surrogate \ud800 text",
    ]:
        assert counter.count(text) == (len(canonical.encode_ordinary(text)), "o200k_base")
    assert counter.encoding.n_vocab == canonical.n_vocab
    assert "MIT License" in tokenizer._BPE_ASSET.with_name("LICENSE.tiktoken").read_text(encoding="utf8")


def test_no_network_or_registry_access_even_when_registry_lock_is_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    tiktoken = pytest.importorskip("tiktoken")
    import requests

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("offline initialization must not access network or encoding registry")

    monkeypatch.setattr(tiktoken, "get_encoding", forbidden)
    monkeypatch.setattr(requests, "get", forbidden)
    entered = threading.Event()
    release = threading.Event()

    def own_registry() -> None:
        with tiktoken.registry._lock:
            entered.set()
            assert release.wait(3)

    owner = threading.Thread(target=own_registry)
    owner.start()
    provider = tokenizer._TokenCounterProvider()
    try:
        assert entered.wait(1)

        async def acquire() -> tokenizer.TokenCounter:
            return await asyncio.wait_for(provider.acquire(), 2)

        counter = asyncio.run(acquire())
        assert counter.count("你好") == (1, "o200k_base")
        assert not release.is_set()
    finally:
        release.set()
        owner.join(2)
        assert not owner.is_alive()
        _join(provider)


def test_concurrent_acquisition_is_single_flight_and_cancel_is_consumer_owned() -> None:
    pytest.importorskip("tiktoken")
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def initialize() -> tokenizer.TokenCounter:
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return tokenizer._create_counter()

    provider = tokenizer._TokenCounterProvider(initialize)

    async def scenario() -> None:
        consumers = [asyncio.create_task(provider.acquire()) for _ in range(4)]
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            consumers[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await consumers[0]
            # Cancellation must neither release shared work nor prevent a new
            # consumer from joining the same actual initialization.
            late_consumer = asyncio.create_task(provider.acquire())
            assert len(calls) == 1
            assert not provider._inflight.cancelled()
            assert await asyncio.wait_for(asyncio.sleep(0, result="context can progress"), 0.2)
            release.set()
            counters = await asyncio.wait_for(asyncio.gather(*consumers[1:], late_consumer), 2)
            assert all(counter is counters[0] for counter in counters)
            assert await provider.acquire() is counters[0]
            assert len(calls) == 1
        finally:
            release.set()
            for task in consumers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        _join(provider)


def test_sync_loop_entry_does_not_initialize_and_worker_count_stays_offloop(monkeypatch: pytest.MonkeyPatch) -> None:
    threads: list[tuple[str, int]] = []

    class Encoding:
        def encode_ordinary(self, text: str) -> list[int]:
            threads.append(("count", threading.get_ident()))
            return [1, 2, 3]

    def initialize() -> tokenizer.TokenCounter:
        threads.append(("init", threading.get_ident()))
        return tokenizer.TokenCounter(Encoding())

    provider = tokenizer._TokenCounterProvider(initialize)
    monkeypatch.setattr(tokenizer, "_PROVIDER", provider)

    async def scenario() -> None:
        loop_thread = threading.get_ident()
        cold = tokenizer.get_token_counter()
        assert cold.count("测试A") == (7, "utf8_bytes_upper_bound")
        assert not threads
        counter = await tokenizer.acquire_token_counter()
        assert tokenizer.get_token_counter() is counter
        assert await asyncio.to_thread(counter.count, "normal text") == (3, "o200k_base")
        assert all(thread != loop_thread for _, thread in threads)
        assert [operation for operation, _ in threads] == ["init", "count"]

    try:
        asyncio.run(scenario())
    finally:
        _join(provider)


def test_sync_worker_entry_can_initialize(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("tiktoken")
    main_thread = threading.get_ident()
    threads: list[int] = []

    def initialize() -> tokenizer.TokenCounter:
        threads.append(threading.get_ident())
        return tokenizer._create_counter()

    provider = tokenizer._TokenCounterProvider(initialize)
    monkeypatch.setattr(tokenizer, "_PROVIDER", provider)
    try:
        counter = tokenizer.get_token_counter()
        assert counter.count("你好") == (1, "o200k_base")
        assert tokenizer.get_token_counter() is counter
        assert len(threads) == 1 and threads[0] != main_thread
    finally:
        _join(provider)


@pytest.mark.parametrize("broken", ["missing", "corrupt", "oversize"])
def test_asset_failure_backoff_and_recovery_preserve_operation_snapshot(tmp_path: Any, broken: str) -> None:
    pytest.importorskip("tiktoken")
    asset = tmp_path / "encoding.tiktoken"
    if broken == "corrupt":
        asset.write_bytes(b"untrusted resource")
    elif broken == "oversize":
        asset.write_bytes(b"x" * (tokenizer._MAX_BPE_BYTES + 1))
    clock = [0.0]
    attempts: list[int] = []

    def initialize() -> tokenizer.TokenCounter:
        attempts.append(1)
        return tokenizer._create_counter(asset=asset)

    provider = tokenizer._TokenCounterProvider(initialize, retry_seconds=5, clock=lambda: clock[0])
    try:
        failed = asyncio.run(provider.acquire())
        assert failed.encoding is None and failed.failure_reason
        assert failed.count("测试A") == (7, "utf8_bytes_upper_bound")
        shutil.copyfile(tokenizer._BPE_ASSET, asset)
        assert asyncio.run(provider.acquire()) is failed
        assert len(attempts) == 1
        clock[0] = 5
        recovered = asyncio.run(provider.acquire())
        assert recovered.encoding is not None and len(attempts) == 2
        assert recovered.count("测试A")[1] == "o200k_base"
        # A previously chosen fallback remains fixed for that operation even
        # while later operations benefit from a recovered process encoder.
        assert failed.count("测试A") == (7, "utf8_bytes_upper_bound")
        assert asyncio.run(provider.acquire()) is recovered
        with pytest.raises(FrozenInstanceError):
            failed.encoding = recovered.encoding
    finally:
        _join(provider)


def test_budget_expiration_is_not_cached_forever() -> None:
    pytest.importorskip("tiktoken")
    expired = [True]
    provider = tokenizer._TokenCounterProvider(
        lambda: tokenizer._create_counter(budget=0 if expired[0] else 4), retry_seconds=0,
    )
    try:
        first = asyncio.run(provider.acquire())
        assert first.failure_reason == "TimeoutError"
        expired[0] = False
        recovered = asyncio.run(provider.acquire())
        assert recovered.encoding is not None
    finally:
        _join(provider)


def test_initialization_failure_is_retryable_without_tiktoken() -> None:
    attempts = [0]

    def initialize() -> tokenizer.TokenCounter:
        attempts[0] += 1
        if attempts[0] == 1:
            raise ImportError("optional tiktoken is unavailable")
        return tokenizer.TokenCounter(object())

    provider = tokenizer._TokenCounterProvider(initialize, retry_seconds=0)
    try:
        failed = asyncio.run(provider.acquire())
        assert failed.failure_reason == "ImportError"
        assert failed.count("测试A") == (7, "utf8_bytes_upper_bound")
        recovered = asyncio.run(provider.acquire())
        assert recovered.encoding is not None and attempts[0] == 2
    finally:
        _join(provider)


def test_wait_timeout_keeps_one_shared_worker_and_later_recovers() -> None:
    entered = threading.Event()
    release = threading.Event()
    attempts: list[int] = []

    class Encoding:
        def encode_ordinary(self, text: str) -> list[int]:
            return [1]

    def initialize() -> tokenizer.TokenCounter:
        attempts.append(1)
        entered.set()
        assert release.wait(2)
        return tokenizer.TokenCounter(Encoding())

    provider = tokenizer._TokenCounterProvider(initialize, wait_seconds=0.03)

    async def scenario() -> None:
        first = asyncio.create_task(provider.acquire())
        assert await asyncio.to_thread(entered.wait, 1)
        other_consumers = [asyncio.create_task(provider.acquire()) for _ in range(3)]
        fallbacks = await asyncio.wait_for(asyncio.gather(first, *other_consumers), 0.5)
        assert all(counter.failure_reason == "initialization_wait_expired" for counter in fallbacks)
        assert all(counter.count("测试A") == (7, "utf8_bytes_upper_bound") for counter in fallbacks)
        assert len(attempts) == 1 and not provider._inflight.cancelled()
        later = await asyncio.wait_for(provider.acquire(), 0.5)
        assert later.encoding is None and len(attempts) == 1
        release.set()
        await asyncio.to_thread(_join, provider)
        recovered = await provider.acquire()
        assert recovered.count("测试A") == (1, "o200k_base")
        assert len(attempts) == 1
        assert all(counter.count("测试A")[1] == "utf8_bytes_upper_bound" for counter in fallbacks)

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        _join(provider)


@pytest.mark.parametrize("entry", ["async", "sync"])
def test_child_pid_never_uses_inherited_locked_provider_or_future(monkeypatch, entry) -> None:
    from concurrent.futures import Future

    pytest.importorskip("tiktoken")
    parent = tokenizer._TokenCounterProvider(wait_seconds=0.05)
    parent._inflight = Future()
    entered = threading.Event()
    release = threading.Event()

    def hold_parent_lock() -> None:
        with parent._lock:
            entered.set()
            release.wait(2)

    owner = threading.Thread(target=hold_parent_lock)
    owner.start()
    monkeypatch.setattr(tokenizer, "_PROVIDER", parent)
    monkeypatch.setattr(tokenizer, "_PROVIDER_PID", 1001)
    monkeypatch.setattr(tokenizer, "_current_process_id", lambda: 1002)
    monkeypatch.setattr(tokenizer, "_CHILD_PROVIDERS", {})
    try:
        assert entered.wait(1)
        if entry == "async":
            async def acquire():
                return await asyncio.wait_for(tokenizer.acquire_token_counter(), 1)
            counter = asyncio.run(acquire())
        else:
            counter = tokenizer.get_token_counter()
        assert counter.encoding is not None
        assert counter.count("你好") == (1, "o200k_base")
        assert tokenizer._PROVIDER is not parent
        assert tokenizer._PROVIDER_PID == 1002
        assert parent._lock.locked()
        assert not parent._inflight.done()
        _join(tokenizer._PROVIDER)
    finally:
        release.set()
        owner.join(2)
        assert not owner.is_alive()
        if tokenizer._PROVIDER is not parent:
            _join(tokenizer._PROVIDER)


def test_concurrent_first_child_calls_select_one_provider(monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    parent = tokenizer._TokenCounterProvider()
    original = tokenizer._TokenCounterProvider
    barrier = threading.Barrier(4)

    def candidate():
        barrier.wait(timeout=2)
        return original()

    monkeypatch.setattr(tokenizer, "_PROVIDER", parent)
    monkeypatch.setattr(tokenizer, "_PROVIDER_PID", 1001)
    monkeypatch.setattr(tokenizer, "_current_process_id", lambda: 1002)
    monkeypatch.setattr(tokenizer, "_CHILD_PROVIDERS", {})
    monkeypatch.setattr(tokenizer, "_TokenCounterProvider", candidate)
    with ThreadPoolExecutor(max_workers=4) as workers:
        providers = list(workers.map(lambda _: tokenizer._get_current_provider(), range(4)))
    assert all(provider is providers[0] for provider in providers)
    assert providers[0] is not parent
    assert providers[0]._worker is None
    assert tokenizer._PROVIDER is providers[0]
