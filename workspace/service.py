"""Host-owned persistent work, serialized per role and colony."""

import asyncio
import uuid

from .runtime import WorkspaceAgent
from .storage import WorkspaceStorage


class NoCurrentWork(ValueError):
    """No retained colony work is available for this caller while offline."""


class WorkspaceService:
    def __init__(self, path, runtime, *, game_session=None, report_store=None):
        self.storage = WorkspaceStorage(path)
        self.agent = WorkspaceAgent(self.storage, runtime, report_store=report_store)
        self.game_session = game_session
        self._locks = {}
        self._tasks = set()
        self._start_lock = asyncio.Lock()
        self._started = False
        self.closed = False
        self.revision = 0
        self.active = {}

    async def consult(self, owner, colony_id, question, settings):
        if self.closed:
            raise RuntimeError("Workspace service is closed")
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            async with self._start_lock:
                if not self._started:
                    await self.storage.call("recover_interrupted")
                    self._started = True
            if colony_id is None:
                current = await self.storage.call("current_work", owner)
                if current is None:
                    raise NoCurrentWork("No retained colony work is available for this caller")
                colony_id = current["colony_id"]
            lock = self._locks.setdefault((owner, colony_id), asyncio.Lock())
            async with lock:
                if self.game_session is not None:
                    game = self.game_session()
                    if game is not None and game.colony_id != colony_id:
                        raise ValueError("The active colony changed before this work could execute")
                workspace_id = await self.storage.call("open_work", owner, colony_id)
                await self.agent.retry_history(workspace_id, owner)
                # This lock has no active execution now. A previous cancellation
                # or error may also have failed to settle its owned transaction.
                await self.storage.call("recover_interrupted", workspace_id, owner)
                turn_id = uuid.uuid4().hex
                try:
                    await self.storage.call("accept", workspace_id, owner, question, turn_id=turn_id)
                    self.active[workspace_id] = {"turn_id": turn_id}
                    self.revision += 1
                    output = await self.agent.execute(workspace_id, owner, turn_id, settings, colony_id=colony_id)
                except asyncio.CancelledError:
                    await self.storage.call("abort", workspace_id, owner, turn_id, {"status": "cancelled"})
                    raise
                except Exception:
                    await self.storage.call("abort", workspace_id, owner, turn_id, {"status": "failed"})
                    raise
                finally:
                    self.active.pop(workspace_id, None)
                    self.revision += 1
                result = output["result"]
                saved = output["history_saved"]
                result["workspace"] = {
                    "workspace_id": workspace_id,
                    "turn_id": turn_id,
                    "state": "waiting" if saved else "save_unconfirmed",
                    "history_saved": saved,
                    "checkpoint": output["checkpoint"],
                }
                return result
        finally:
            self._tasks.discard(task)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        tasks = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.storage.close()
