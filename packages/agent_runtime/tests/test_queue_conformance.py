"""MessageQueue interface conformance — ONE suite, run against every implementation.

The in-process queue is the reference behavior; any later queue implementation
(e.g. a redis-backed one, by later intake) must be indistinguishable through
the interface: FIFO order, blocking get, and the producer's reply future
resolving when the consumer resolves the delivered item.
"""

import asyncio
from collections.abc import Callable

import pytest

from agent_runtime.components.dev_http import DevHttpAdapter
from agent_runtime.components.memory_queue import InProcessQueue
from agent_runtime.messages import InternalMessage
from agent_runtime.queues import MessageQueue, QueueItem


@pytest.fixture(params=["in-process"])
def make_queue(request: pytest.FixtureRequest) -> Callable[[], MessageQueue]:
    return InProcessQueue


async def _close(queue: MessageQueue) -> None:
    pass


def _message(text: str, conversation_id: str = "c-1") -> InternalMessage:
    adapter = DevHttpAdapter(InProcessQueue())
    return adapter.normalize(
        {"text": text, "conversation_id": conversation_id, "sender_id": "conformance"}
    )


def test_roundtrip_preserves_message_and_reply_future(
    make_queue: Callable[[], MessageQueue],
) -> None:
    """put -> get returns the same message, and resolving the delivered item's
    reply resolves the future the producer is awaiting."""

    async def run() -> None:
        queue = make_queue()
        try:
            sent = _message("hello queue")
            producer_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            await queue.put(QueueItem(message=sent, reply=producer_future))

            item = await asyncio.wait_for(queue.get(), timeout=10)
            assert item.message.id == sent.id
            assert item.message.text() == "hello queue"
            assert item.message.channel == sent.channel
            assert item.message.sender == sent.sender
            assert item.message.provenance == sent.provenance

            item.reply.set_result("the reply")
            assert await asyncio.wait_for(producer_future, timeout=5) == "the reply"
        finally:
            await _close(queue)

    asyncio.run(run())


def test_fifo_order_preserved(make_queue: Callable[[], MessageQueue]) -> None:
    """Strict FIFO — the property serial-per-conversation dispatch rides on."""

    async def run() -> None:
        queue = make_queue()
        try:
            loop = asyncio.get_running_loop()
            sent = [_message(f"msg-{i}", conversation_id="c-order") for i in range(5)]
            for message in sent:
                await queue.put(QueueItem(message=message, reply=loop.create_future()))
            received = [await asyncio.wait_for(queue.get(), timeout=10) for _ in sent]
            assert [item.message.id for item in received] == [m.id for m in sent]
        finally:
            await _close(queue)

    asyncio.run(run())


def test_get_blocks_until_put(make_queue: Callable[[], MessageQueue]) -> None:
    async def run() -> None:
        queue = make_queue()
        try:
            getter = asyncio.create_task(queue.get())
            await asyncio.sleep(0.2)
            assert not getter.done(), "get() must block while the queue is empty"

            sent = _message("late arrival")
            await queue.put(
                QueueItem(message=sent, reply=asyncio.get_running_loop().create_future())
            )
            item = await asyncio.wait_for(getter, timeout=10)
            assert item.message.id == sent.id
        finally:
            await _close(queue)

    asyncio.run(run())
