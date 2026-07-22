"""In-process asyncio message queue — the walking skeleton's gateway queue."""

import asyncio

from agent_runtime.queues import QueueItem


class InProcessQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[QueueItem] = asyncio.Queue()

    async def put(self, item: QueueItem) -> None:
        await self._queue.put(item)

    async def get(self) -> QueueItem:
        return await self._queue.get()
