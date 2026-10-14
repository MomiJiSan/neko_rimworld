from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from plugin.plugins.neko_rimworld.agent import tokenizer_work


class ObservedPool(tokenizer_work._TokenizerWorkPool):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.admitted = asyncio.Queue()
        self.waiting = asyncio.Queue()

    def _try_admit(self, waiter):
        result = super()._try_admit(waiter)
        (self.admitted if result else self.waiting).put_nowait(waiter)
        return result


async def notification(notifications: asyncio.Queue):
    return await asyncio.wait_for(notifications.get(), 2)


async def retire(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_physical_jobs_keep_four_slots_and_24_waiters_leave_no_jobs():
    pool = ObservedPool()
    loop = asyncio.get_running_loop()
    release = threading.Event()
    started = asyncio.Queue()
    executed = []

    def work(index):
        executed.append(index)
        loop.call_soon_threadsafe(started.put_nowait, index)
        if not release.wait(5):
            raise AssertionError("test did not release its pure workers")
        return index

    physical = [asyncio.create_task(pool.run(work, index)) for index in range(4)]
    waiting = []
    try:
        for _ in range(4):
            await notification(pool.admitted)
        for _ in range(2):
            await notification(started)
        await retire(physical)
        assert pool.snapshot()["used"] == 4
        assert len(pool._executor._threads) == 2
        assert pool._executor._work_queue.qsize() == 2
        waiting = [asyncio.create_task(pool.run(work, index)) for index in range(4, 28)]
        for _ in waiting:
            await notification(pool.waiting)
        assert pool.snapshot()["waiting"] == 24
        await retire(waiting)
        assert pool.snapshot()["waiting"] == 0
        assert pool.snapshot()["used"] == 4
        assert pool._executor._work_queue.qsize() == 2
        assert set(executed) == {0, 1}
    finally:
        release.set()
        await retire(physical + waiting)
        pool.shutdown()
    assert set(executed) == set(range(4))
    assert pool.snapshot()["used"] == 0
    assert not any(thread.is_alive() for thread in pool._executor._threads)


@pytest.mark.asyncio
async def test_public_pure_work_does_not_wait_for_saturated_default_executor(monkeypatch):
    loop = asyncio.get_running_loop()
    default = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-owned-default")
    loop.set_default_executor(default)
    entered = asyncio.Queue()
    release = threading.Event()
    pool = tokenizer_work._TokenizerWorkPool()
    monkeypatch.setattr(tokenizer_work, "_get_pool", lambda: pool)

    def blocker():
        loop.call_soon_threadsafe(entered.put_nowait, True)
        if not release.wait(5):
            raise AssertionError("test did not release default-executor blockers")

    blockers = [loop.run_in_executor(None, blocker) for _ in range(2)]
    try:
        for _ in blockers:
            await notification(entered)
        thread = threading.get_ident()
        def count(a, *, b):
            return a + b, threading.get_ident()
        result, worker = await asyncio.wait_for(tokenizer_work.run_tokenizer_work(count, 2, b=3), 2)
        assert result == 5
        assert worker != thread
        assert not release.is_set()
        assert default._work_queue.qsize() == 0
    finally:
        release.set()
        await asyncio.gather(*blockers)
        default.shutdown()
        pool.shutdown()


@pytest.mark.asyncio
async def test_late_worker_exception_releases_slot_without_unretrieved_failure():
    pool = ObservedPool(max_workers=1, capacity=1)
    loop = asyncio.get_running_loop()
    release = threading.Event()
    errors = []
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: errors.append(context))
    def failure():
        if not release.wait(5):
            raise AssertionError("test did not release late failure")
        raise ValueError("late pure-worker failure")
    first = asyncio.create_task(pool.run(failure))
    second = None
    try:
        await notification(pool.admitted)
        await retire([first])
        second = asyncio.create_task(pool.run(lambda: "next"))
        await notification(pool.waiting)
        assert pool.snapshot()["used"] == 1
        release.set()
        assert await asyncio.wait_for(second, 2) == "next"
        assert pool.snapshot()["used"] == 0
        assert errors == []
    finally:
        release.set()
        await retire([first] + ([second] if second is not None else []))
        pool.shutdown()
        loop.set_exception_handler(old_handler)


@pytest.mark.asyncio
async def test_cancel_after_notification_before_consume_does_not_strand_capacity(monkeypatch):
    pool = ObservedPool(max_workers=1, capacity=1)
    release = threading.Event()
    first = asyncio.create_task(pool.run(lambda: release.wait(5)))
    second = None
    original_signal = pool._signal
    def signal(waiter):
        original_signal(waiter)
        second.cancel()
    monkeypatch.setattr(pool, "_signal", signal)
    try:
        await notification(pool.admitted)
        second = asyncio.create_task(pool.run(lambda: pytest.fail("cancelled notified waiter ran")))
        await notification(pool.waiting)
        release.set()
        await asyncio.wait_for(first, 2)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(second, 2)
        assert pool.snapshot()["used"] == 0
        assert pool.snapshot()["waiting"] == 0
        monkeypatch.setattr(pool, "_signal", original_signal)
        assert await pool.run(lambda: "usable") == "usable"
    finally:
        release.set()
        await retire([first] + ([second] if second is not None else []))
        pool.shutdown()


@pytest.mark.asyncio
async def test_contextvars_are_separate_for_concurrent_worker_submissions():
    pool = tokenizer_work._TokenizerWorkPool()
    value = contextvars.ContextVar("tokenizer_test_context", default="unset")
    release = threading.Event()
    started = asyncio.Queue()
    loop = asyncio.get_running_loop()
    def work():
        selected = value.get()
        loop.call_soon_threadsafe(started.put_nowait, selected)
        if not release.wait(5):
            raise AssertionError("test did not release context workers")
        return value.get()
    tasks = []
    try:
        for selected in ["first", "second"]:
            token = value.set(selected)
            try:
                tasks.append(asyncio.create_task(pool.run(work)))
            finally:
                value.reset(token)
        assert {await notification(started), await notification(started)} == {"first", "second"}
        release.set()
        assert await asyncio.gather(*tasks) == ["first", "second"]
        assert value.get() == "unset"
    finally:
        release.set()
        await retire(tasks)
        pool.shutdown()


@pytest.mark.asyncio
async def test_shutdown_wakes_admission_waiter_and_failed_submit_returns_slot(monkeypatch):
    pool = ObservedPool(max_workers=1, capacity=1)
    release = threading.Event()
    first = asyncio.create_task(pool.run(lambda: release.wait(5)))
    second = None
    try:
        await notification(pool.admitted)
        second = asyncio.create_task(pool.run(lambda: "must not run"))
        await notification(pool.waiting)
        pool.shutdown(wait=False)
        with pytest.raises(RuntimeError, match="closed"):
            await second
        assert pool.snapshot()["used"] == 1
        release.set()
        await first
        assert pool.snapshot()["used"] == 0
    finally:
        release.set()
        await retire([first] + ([second] if second is not None else []))
        pool.shutdown()
    another = tokenizer_work._TokenizerWorkPool()
    def submit(*args, **kwargs):
        raise RuntimeError("controlled submit failure")
    monkeypatch.setattr(another._executor, "submit", submit)
    try:
        with pytest.raises(RuntimeError, match="submit failure"):
            await another.run(lambda: None)
        assert another.snapshot()["used"] == 0
    finally:
        another.shutdown()


@pytest.mark.asyncio
async def test_pid_change_replaces_inherited_pool_and_creation_lock(monkeypatch):
    parent = tokenizer_work._TokenizerWorkPool()
    inherited_lock = threading.Lock()
    inherited_lock.acquire()
    monkeypatch.setattr(tokenizer_work, "_POOL", parent)
    monkeypatch.setattr(tokenizer_work, "_CREATION_LOCK", inherited_lock)
    pid = tokenizer_work._POOL_PID + 1
    monkeypatch.setattr(tokenizer_work, "os", SimpleNamespace(getpid=lambda: pid))
    monkeypatch.setattr(tokenizer_work, "_POOL_PID", pid - 1)
    child = None
    try:
        child = tokenizer_work._get_pool()
        assert child is not parent
        assert tokenizer_work._CREATION_LOCK is not inherited_lock
        assert await child.run(lambda: "fresh child pool") == "fresh child pool"
        assert tokenizer_work._POOL_PID == pid
    finally:
        inherited_lock.release()
        if child is not None:
            child.shutdown()
        parent.shutdown()


@pytest.mark.asyncio
async def test_cancelled_owner_loop_can_close_before_physical_job_releases_other_loop(monkeypatch):
    pool = tokenizer_work._TokenizerWorkPool(max_workers=1, capacity=1)
    main_loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    owner_closed = asyncio.Event()
    waiting = asyncio.Event()
    release = threading.Event()
    owner_loop = asyncio.new_event_loop()
    owner_task = []
    failures = []
    original_admit = pool._try_admit

    def admit(waiter):
        result = original_admit(waiter)
        if not result:
            main_loop.call_soon_threadsafe(waiting.set)
        return result

    monkeypatch.setattr(pool, "_try_admit", admit)

    def work():
        main_loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise AssertionError("test did not release cross-loop worker")

    def owner():
        try:
            task = owner_loop.create_task(pool.run(work))
            owner_task.append(task)
            try:
                owner_loop.run_until_complete(task)
            except asyncio.CancelledError:
                pass
        except BaseException as error:
            failures.append(error)
        finally:
            owner_loop.close()
            main_loop.call_soon_threadsafe(owner_closed.set)

    thread = threading.Thread(target=owner, name="test-owned-tokenizer-loop")
    thread.start()
    follower = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        follower = asyncio.create_task(pool.run(lambda: "other loop accepted"))
        await asyncio.wait_for(waiting.wait(), 2)
        owner_loop.call_soon_threadsafe(owner_task[0].cancel)
        await asyncio.wait_for(owner_closed.wait(), 2)
        assert pool.snapshot()["used"] == 1
        assert pool.snapshot()["waiting"] == 1
        assert not release.is_set()
        release.set()
        assert await asyncio.wait_for(follower, 2) == "other loop accepted"
        assert pool.snapshot()["used"] == 0
        assert pool.snapshot()["waiting"] == 0
        assert failures == []
    finally:
        release.set()
        if not owner_loop.is_closed() and owner_task:
            owner_loop.call_soon_threadsafe(owner_task[0].cancel)
        await asyncio.wait_for(owner_closed.wait(), 2)
        await asyncio.to_thread(thread.join, 2)
        assert not thread.is_alive()
        await retire([follower] if follower is not None else [])
        pool.shutdown()


@pytest.mark.asyncio
async def test_closed_loop_notification_never_reserves_a_slot():
    pool = tokenizer_work._TokenizerWorkPool(max_workers=1, capacity=1)
    closed_loop = asyncio.new_event_loop()
    future = closed_loop.create_future()
    # This is the reachable handshake state after notification but before the
    # consumer resumes. A notification holds no physical-work reservation.
    future.set_result(None)
    waiter = tokenizer_work._Waiter(closed_loop, future, queued=True, notified=True)
    pool._waiters.append(waiter)
    closed_loop.close()
    try:
        assert await pool.run(lambda: "closed owner skipped") == "closed owner skipped"
        assert pool.snapshot()["used"] == 0
        assert pool.snapshot()["waiting"] == 0
        assert waiter.queued is False
    finally:
        pool.shutdown()


@pytest.mark.asyncio
async def test_later_request_wakes_fifo_after_closed_head_loses_notification():
    pool = ObservedPool(max_workers=1, capacity=1)
    release = threading.Event()
    first = asyncio.create_task(pool.run(lambda: release.wait(5)))
    abandoned_loop = asyncio.new_event_loop()
    abandoned = tokenizer_work._Waiter(abandoned_loop, abandoned_loop.create_future(), queued=True, notified=True)
    tasks = [first]
    executed = []
    def work(name):
        assert pool.snapshot()["used"] == 1
        executed.append(name)
        return name
    try:
        await notification(pool.admitted)
        # The head's notification was scheduled to an owner which is about to
        # close. It never reserved a slot or reached a worker submission.
        pool._waiters.append(abandoned)
        second = asyncio.create_task(pool.run(work, "B"))
        tasks.append(second)
        await notification(pool.waiting)
        release.set()
        await asyncio.wait_for(first, 2)
        assert pool.snapshot()["used"] == 0
        abandoned_loop.close()
        third = asyncio.create_task(pool.run(work, "C"))
        tasks.append(third)
        assert await asyncio.wait_for(asyncio.gather(second, third), 2) == ["B", "C"]
        assert executed == ["B", "C"]
        assert abandoned.queued is False
        assert pool.snapshot()["used"] == 0
        assert pool.snapshot()["waiting"] == 0
    finally:
        release.set()
        if not abandoned_loop.is_closed():
            abandoned_loop.close()
        await retire(tasks)
        pool.shutdown()


@pytest.mark.asyncio
async def test_runtime_single_cancel_finishes_before_full_owned_pool_is_released(monkeypatch):
    from plugin.plugins.neko_rimworld.agent import loop as agent_loop
    from plugin.plugins.neko_rimworld.agent.audit import AgentAuditRegistry
    from plugin.plugins.neko_rimworld.agent.tokenizer import TokenCounter
    from plugin.plugins.neko_rimworld.tests.test_tokenizer_runtime import WordEncoding, runtime, settings

    pool = ObservedPool()
    monkeypatch.setattr(tokenizer_work, "_get_pool", lambda: pool)
    monkeypatch.setattr(agent_loop, "CANCEL_HANDOFF_TIMEOUT_SECONDS", 0.02)
    async def acquire():
        return TokenCounter(WordEncoding())
    monkeypatch.setattr(agent_loop, "acquire_token_counter", acquire)
    release = threading.Event()
    model_started = asyncio.Event()
    def occupied():
        if not release.wait(5):
            raise AssertionError("test did not release owned tokenizer jobs")
    class WaitingModel:
        async def complete(self, *args, **kwargs):
            model_started.set()
            await asyncio.Event().wait()
    jobs = [asyncio.create_task(pool.run(occupied)) for _ in range(4)]
    task = None
    registry = AgentAuditRegistry()
    runner = runtime(WaitingModel(), registry)
    try:
        for _ in jobs:
            await notification(pool.admitted)
        task = asyncio.create_task(runner.run("cancellation while all tokenizer slots are busy", settings()))
        await asyncio.wait_for(model_started.wait(), 2)
        stop = asyncio.create_task(runner.cancel_all())
        await notification(pool.waiting)
        await asyncio.wait_for(stop, 2)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not release.is_set()
        terminal = registry.snapshot()
        assert terminal["active_count"] == 0
        assert terminal["latest_run"]["state"] == "cancelled"
        assert terminal["latest_run"]["result"]["status"] == "cancelled"
        assert terminal["latest_run"]["result"]["delivery"]["tokenizer"] == "utf8_bytes_upper_bound"
        assert pool.snapshot()["used"] == 4
        assert pool.snapshot()["waiting"] == 0
        release.set()
        await asyncio.gather(*jobs)
        assert pool.snapshot()["used"] == 0
        assert registry.snapshot() == terminal
    finally:
        release.set()
        await retire(jobs + ([task] if task is not None else []))
        pool.shutdown()


@pytest.mark.asyncio
async def test_runtime_cancel_all_does_not_enqueue_into_blocked_default_pool(monkeypatch):
    from plugin.plugins.neko_rimworld.agent import loop as agent_loop
    from plugin.plugins.neko_rimworld.agent.audit import AgentAuditRegistry
    from plugin.plugins.neko_rimworld.agent.tokenizer import TokenCounter
    from plugin.plugins.neko_rimworld.tests.test_tokenizer_runtime import WordEncoding, runtime, settings

    loop = asyncio.get_running_loop()
    default = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-owned-runtime-default")
    loop.set_default_executor(default)
    entered = asyncio.Queue()
    release = threading.Event()
    pool = tokenizer_work._TokenizerWorkPool()
    monkeypatch.setattr(tokenizer_work, "_get_pool", lambda: pool)
    async def acquire():
        return TokenCounter(WordEncoding())
    monkeypatch.setattr(agent_loop, "acquire_token_counter", acquire)
    def blocker():
        loop.call_soon_threadsafe(entered.put_nowait, True)
        if not release.wait(5):
            raise AssertionError("test did not release shared-executor blockers")
    blockers = [loop.run_in_executor(None, blocker) for _ in range(2)]
    model_started = asyncio.Event()
    class WaitingModel:
        async def complete(self, *args, **kwargs):
            model_started.set()
            await asyncio.Event().wait()
    registry = AgentAuditRegistry()
    runner = runtime(WaitingModel(), registry)
    task = None
    try:
        for _ in blockers:
            await notification(entered)
        task = asyncio.create_task(runner.run("cancellation with a busy host executor", settings()))
        await asyncio.wait_for(model_started.wait(), 2)
        await asyncio.wait_for(runner.cancel_all(), 2)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not release.is_set()
        assert default._work_queue.qsize() == 0
        assert registry.snapshot()["active_count"] == 0
        assert registry.snapshot()["latest_run"]["result"]["status"] == "cancelled"
        assert registry.snapshot()["latest_run"]["result"]["delivery"]["tokenizer"] == "o200k_base"
    finally:
        release.set()
        await asyncio.gather(*blockers)
        await retire([task] if task is not None else [])
        default.shutdown()
        pool.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_worker", [1, 2])
async def test_real_thread_start_failures_never_queue_business_jobs_and_can_recover(monkeypatch, failed_worker):
    monkeypatch.setattr(tokenizer_work, "_POOL", None)
    monkeypatch.setattr(tokenizer_work, "_CREATION_LOCK", threading.Lock())
    original_start = threading.Thread.start
    attempted = []
    business_calls = []
    healthy = None

    def start(thread, *args, **kwargs):
        if thread.name.startswith("rimworld-tokenizer-work"):
            attempted.append(thread)
            if failed_worker == 1 or len(attempted) % 2 == 0:
                raise RuntimeError("controlled OS thread startup failure")
        return original_start(thread, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", start)
    try:
        for index in range(8):
            with pytest.raises(RuntimeError, match="startup failure"):
                await tokenizer_work.run_tokenizer_work(lambda index=index: business_calls.append(index))
            assert tokenizer_work._POOL is None
            assert business_calls == []
        monkeypatch.setattr(threading.Thread, "start", original_start)
        for thread in attempted:
            if thread.ident is not None:
                await asyncio.to_thread(thread.join, 2)
                assert not thread.is_alive()
        assert business_calls == []
        assert await tokenizer_work.run_tokenizer_work(lambda: business_calls.append("healthy")) is None
        healthy = tokenizer_work._POOL
        assert healthy is not None
        assert len(healthy._executor._threads) == 2
        assert healthy.snapshot()["used"] == 0
        assert all(future.done() for future in healthy._startup)
        assert business_calls == ["healthy"]
    finally:
        monkeypatch.setattr(threading.Thread, "start", original_start)
        if healthy is None:
            healthy = tokenizer_work._POOL
        if healthy is not None:
            healthy.shutdown()
        for thread in attempted:
            if thread.ident is not None:
                await asyncio.to_thread(thread.join, 2)
                assert not thread.is_alive()


@pytest.mark.asyncio
async def test_cancelling_startup_wait_does_not_cancel_shared_startup_or_admit_work(monkeypatch):
    loop = asyncio.get_running_loop()
    entered = asyncio.Queue()
    release = threading.Event()
    original_start = threading.Thread.start
    business_calls = []

    def start(thread, *args, **kwargs):
        if thread.name.startswith("rimworld-tokenizer-work"):
            target = thread._target
            def delayed_target(*target_args, **target_kwargs):
                loop.call_soon_threadsafe(entered.put_nowait, True)
                if not release.wait(5):
                    raise AssertionError("test did not release startup threads")
                return target(*target_args, **target_kwargs)
            thread._target = delayed_target
        return original_start(thread, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", start)
    pool = tokenizer_work._TokenizerWorkPool()
    first = asyncio.create_task(pool.run(lambda: business_calls.append("cancelled")))
    second = None
    try:
        for _ in range(2):
            await notification(entered)
        await retire([first])
        assert pool.snapshot()["used"] == 0
        assert pool.snapshot()["waiting"] == 0
        assert all(not future.cancelled() and not future.done() for future in pool._startup)
        assert business_calls == []
        second = asyncio.create_task(pool.run(lambda: business_calls.append("healthy")))
        release.set()
        await asyncio.wait_for(second, 2)
        assert all(future.done() and not future.cancelled() for future in pool._startup)
        assert business_calls == ["healthy"]
        assert pool.snapshot()["used"] == 0
    finally:
        release.set()
        await retire([first] + ([second] if second is not None else []))
        pool.shutdown()
