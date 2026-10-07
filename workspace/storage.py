"""Run SQLite and source serialization on one owned worker, outside the UI loop."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from .store import WorkspaceStore


class WorkspaceStorage:
    def __init__(self, path):
        self.path = path
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rimworld-workspace")
        self._store = None
        self.closed = False

    def _call(self, method, args, kwargs):
        if self._store is None:
            self._store = WorkspaceStore(self.path)
        return getattr(self._store, method)(*args, **kwargs)

    async def call(self, method, *args, **kwargs):
        if self.closed:
            raise RuntimeError("Workspace storage is closed")
        future = asyncio.get_running_loop().run_in_executor(self._worker, partial(self._call, method, args, kwargs))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await asyncio.shield(future)
            raise

    async def close(self):
        if self.closed:
            return
        try:
            if self._store is not None:
                await self.call("close")
        finally:
            self.closed = True
            self._worker.shutdown(wait=False)
