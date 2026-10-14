"""Bounded pure tokenizer work, isolated from the host's default executor.

Cancelled consumers leave admitted jobs accounted for until physical completion.
Admission notifications never reserve slots: a closed loop cannot strand a grant.
"""
from __future__ import annotations

import asyncio
import atexit
import contextvars
import os
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable


@dataclass(eq=False, slots=True)
class _Waiter:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[None]
    queued: bool = False
    notified: bool = False


class _TokenizerWorkPool:
    def __init__(self, *, max_workers: int = 2, capacity: int = 4) -> None:
        if max_workers < 1 or capacity < max_workers:
            raise ValueError("invalid tokenizer worker capacity")
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rimworld-tokenizer-work")
        self._capacity = capacity
        self._used = 0
        self._closed = False
        self._waiters: deque[_Waiter] = deque()
        self._lock = threading.Lock()
        # submit() queues its WorkItem before starting a missing worker. Never
        # let a Thread.start failure strand a real operation whose caller saw
        # an exception. Only these data-free startup jobs may reach that path.
        gate = threading.Event()
        self._startup: tuple[Future[Any], ...] = ()
        try:
            self._startup = tuple(self._executor.submit(gate.wait) for _ in range(max_workers))
        except BaseException:
            self._closed = True
            self._executor.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            gate.set()

    def _clean_locked(self) -> None:
        for waiter in tuple(self._waiters):
            if waiter.loop.is_closed() or waiter.future.cancelled():
                self._waiters.remove(waiter)
                waiter.queued = False

    def _notify_head(self) -> None:
        with self._lock:
            self._clean_locked()
            if self._closed or self._used >= self._capacity or not self._waiters:
                return
            waiter = self._waiters[0]
            if waiter.notified:
                return
            waiter.notified = True
        try:
            waiter.loop.call_soon_threadsafe(self._signal, waiter)
        except RuntimeError:
            # The loop may close between the state check and notification.
            self._discard(waiter)

    def _signal(self, waiter: _Waiter) -> None:
        if waiter.future.done():
            self._discard(waiter)
        else:
            waiter.future.set_result(None)

    def _discard(self, waiter: _Waiter) -> None:
        with self._lock:
            if waiter.queued:
                self._waiters.remove(waiter)
                waiter.queued = False
        self._notify_head()

    def _try_admit(self, waiter: _Waiter) -> bool:
        with self._lock:
            if self._closed:
                raise RuntimeError("tokenizer work pool is closed")
            self._clean_locked()
            if self._used < self._capacity and (not self._waiters or self._waiters[0] is waiter):
                if waiter.queued:
                    self._waiters.popleft()
                    waiter.queued = False
                self._used += 1
                return True
            if not waiter.queued:
                self._waiters.append(waiter)
                waiter.queued = True
        return False

    def _completed(self, _future: Future[Any] | None = None) -> None:
        with self._lock:
            self._used -= 1
        self._notify_head()

    async def run(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        # All fixed workers exist and their startup WorkItems have finished
        # before capacity accounting/admission begins. Cancellation owns only
        # this consumer, never startup shared with another loop.
        for startup in self._startup:
            await asyncio.shield(asyncio.wrap_future(startup, loop=loop))
        waiter = _Waiter(loop, loop.create_future())
        try:
            if not self._try_admit(waiter):
                self._notify_head()
                await waiter.future
                # Only this consumer can take its FIFO slot. No permit was
                # reserved while its task/loop was waiting to resume.
                if not self._try_admit(waiter):
                    raise RuntimeError("tokenizer admission lost its available slot")
        except BaseException:
            self._discard(waiter)
            raise
        self._notify_head()
        # No await separates admission and submit. A failed submit also gives
        # back the reservation, while cancellation after submit cannot do so.
        try:
            context = contextvars.copy_context()
            job = self._executor.submit(partial(context.run, function, *args, **kwargs))
        except BaseException:
            self._completed()
            raise
        job.add_done_callback(self._completed)
        result = asyncio.wrap_future(job, loop=loop)
        # Retrieve a late failure even after the consumer was cancelled.
        result.add_done_callback(lambda future: None if future.cancelled() else future.exception())
        return await asyncio.shield(result)

    def snapshot(self) -> dict[str, int | bool]:
        with self._lock:
            self._clean_locked()
            return {"used": self._used, "waiting": len(self._waiters), "capacity": self._capacity, "closed": self._closed}

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
            waiters = tuple(self._waiters)
            self._waiters.clear()
            for waiter in waiters:
                waiter.queued = False
        for waiter in waiters:
            def reject(waiter: _Waiter = waiter) -> None:
                if not waiter.future.done():
                    waiter.future.set_exception(RuntimeError("tokenizer work pool is closed"))
            try:
                waiter.loop.call_soon_threadsafe(reject)
            except RuntimeError:
                pass
        self._executor.shutdown(wait=wait, cancel_futures=False)


_POOL: _TokenizerWorkPool | None = None
_POOL_PID = os.getpid()
_CREATION_LOCK = threading.Lock()


def _reset_after_fork() -> None:
    global _POOL, _POOL_PID, _CREATION_LOCK
    # Only the forking thread survives: neither a parent pool's workers nor
    # its possibly held locks can be used or shut down by the child.
    _POOL = None
    _POOL_PID = os.getpid()
    _CREATION_LOCK = threading.Lock()


def _get_pool() -> _TokenizerWorkPool:
    global _POOL
    if os.getpid() != _POOL_PID:
        _reset_after_fork()
    with _CREATION_LOCK:
        if _POOL is None:
            _POOL = _TokenizerWorkPool()
        return _POOL


def _shutdown_global_pool() -> None:
    if os.getpid() == _POOL_PID and _POOL is not None:
        _POOL.shutdown()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)
atexit.register(_shutdown_global_pool)


async def run_tokenizer_work(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await _get_pool().run(function, *args, **kwargs)


__all__ = ["run_tokenizer_work"]
