"""Shared channel lifecycle — the reliability half of ENH-06 / #125, the
application-layer companion to Stage 36's shared ``BoundedHttpReceiver``.

Slack (`slack_channel.py`) proved the shape a hardened webhook channel wants:
verify → admit → **enqueue and fast-ack (202) immediately** → deliver the turn's
reply on a tracked BACKGROUND task (bounded by a reply timeout) → and drop a
redelivered activation with an audited note and a uniform SUCCESS ack (never a
non-2xx for a deliberate policy drop — a platform disables an endpoint that
answers its retries with errors). This module lifts that shape ONCE so a channel
adapter provides only its thin platform surface (``verify`` / ``normalize`` /
``is_duplicate`` / ``send_reply``) and gets the async-ack + background-delivery
lifecycle for free.

Two pieces live here:

- :class:`ReplyDelivery` — the async-ack + background reply delivery core. The
  adapter, once a message is fully admitted, calls :meth:`ReplyDelivery.accept`,
  which enqueues the item, returns the fast 202 ack, and spawns a strongly
  referenced background task that awaits the reply future (bounded by the reply
  timeout) and posts it via the adapter's ``send_reply``. The task set +
  ``add_done_callback`` discard keep a delivery from being GC'd mid-flight;
  :meth:`ReplyDelivery.cancel_inflight` cancels in-flight tasks at shutdown with
  no orphan and no never-retrieved exception (Slack's ``aclose`` behavior, now
  shared).

- :class:`SeenIdCache` — the BOUNDED, ephemeral in-memory seen-id set WebEx uses
  to recognize a redelivery. WebEx (unlike Slack) sends NO retry / redelivery /
  attempt header, so recognizing a duplicate message id REQUIRES remembering
  ids, i.e. STATE — this is honestly stateful, not stateless. The set is bounded
  in BOTH size (an LRU cap — the oldest ids are evicted past the cap) and age (a
  short TTL — ids older than the window are evicted), so it cannot grow without
  limit or leak memory. Lost-on-restart is ACCEPTABLE: the dedup window is
  seconds-to-minutes, nothing is in-flight across a restart, and the async-202
  fast-ack already makes platform retries rare — the seen-set turns "rare" into
  "at most one turn per delivery" inside the window.

Both channels drop duplicates but by DIFFERENT mechanisms because their
platforms differ: Slack reads a header and remembers nothing (stateless); WebEx
consults + records this bounded set (stateful). The uniform policy-drop body
below is shared so a signing-secret holder cannot read WHICH policy fired from
the HTTP body — the audit log records which.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from agent_runtime.messages import InternalMessage
from agent_runtime.queues import MessageQueue, QueueItem

logger = logging.getLogger(__name__)

#: One UNIFORM body for EVERY deliberate policy drop (duplicate/replay drop,
#: roster drop, self-echo drop). Distinct bodies were byte-distinguishable,
#: giving a signing-secret holder an oracle for WHICH policy fired; identical
#: bodies close that side channel. The AUDIT LOG — not the HTTP body — records
#: which policy fired (Slack #79, generalized).
POLICY_DROP_BODY = {"status": "dropped"}

#: The fast-ack body an admitted delivery returns immediately (the turn's reply
#: rides the background task, never this response).
ACCEPTED_BODY = {"status": "accepted"}

#: Default bounds for the WebEx seen-id set. Both are honest ceilings, not
#: tuning knobs a caller must set: the cap keeps memory bounded under a flood of
#: distinct ids; the TTL keeps a stale id from lingering past the window a
#: redelivery can plausibly arrive in.
DEFAULT_SEEN_MAX_SIZE = 4096
DEFAULT_SEEN_TTL_SECONDS = 300.0


class SeenIdCache:
    """A bounded, ephemeral in-memory set of recently-seen ids (LRU + TTL).

    ``seen_then_record(key)`` is the drop hook: it returns True if ``key`` was
    already recorded within the TTL window (a duplicate — drop it), else records
    ``key`` and returns False (first sight — process it). The set is bounded in
    BOTH dimensions so it cannot grow without limit:

    - **size:** past ``max_size`` entries the OLDEST id is evicted (an ordered
      map, oldest-first), so a stream of distinct ids stays capped.
    - **age:** an entry older than ``ttl_seconds`` is evicted on the next touch,
      so ids do not linger past the window a redelivery can arrive in.

    ``clock`` is injectable (a monotonic clock in production) so the eviction
    tests can advance time deterministically. Lost-on-restart is the accepted,
    documented trade-off — a fresh cache starting empty is CORRECT, not a bug.
    """

    def __init__(
        self,
        *,
        max_size: int = DEFAULT_SEEN_MAX_SIZE,
        ttl_seconds: float = DEFAULT_SEEN_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_size < 1:
            raise ValueError("SeenIdCache max_size must be >= 1")
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._clock = clock
        #: id -> insertion timestamp, oldest first (insertion order == time
        #: order under a monotonic clock, so the front is always the oldest).
        self._seen: OrderedDict[str, float] = OrderedDict()

    def __len__(self) -> int:
        return len(self._seen)

    def __contains__(self, key: str) -> bool:
        return key in self._seen

    def _evict_expired(self, now: float) -> None:
        # Front-to-back: the oldest entry is at the front, so stop at the first
        # one still inside the window.
        while self._seen:
            oldest_key = next(iter(self._seen))
            if now - self._seen[oldest_key] >= self._ttl:
                self._seen.popitem(last=False)
            else:
                break

    def seen_then_record(self, key: str) -> bool:
        """True iff ``key`` was already recorded within the window (a
        duplicate). On first sight records ``key`` and returns False; enforces
        the size cap and TTL on every call so the set stays bounded."""
        now = self._clock()
        self._evict_expired(now)
        if key in self._seen:
            return True
        self._seen[key] = now
        # New id at the back (already there via insertion); evict oldest past cap.
        while len(self._seen) > self._max_size:
            self._seen.popitem(last=False)
        return False


#: The adapter's outbound-reply callback: post ``reply_text`` for ``message``.
SendReply = Callable[[InternalMessage, str], Awaitable[None]]


class ReplyDelivery:
    """Async-ack + background reply delivery, shared across channel adapters.

    Construct one per adapter with the queue, the reply timeout, and the
    adapter's ``send_reply``. Once a message is fully admitted the adapter calls
    :meth:`accept`, which enqueues the item, returns the fast 202 ack, and
    spawns a tracked background task that awaits the reply future and posts it —
    the webhook connection is never held across the turn.
    """

    def __init__(
        self,
        queue: MessageQueue,
        *,
        send_reply: SendReply,
        reply_timeout_seconds: float,
        platform: str,
    ) -> None:
        self._queue = queue
        self._send_reply = send_reply
        self._reply_timeout = reply_timeout_seconds
        self._platform = platform
        #: Strong references to in-flight reply tasks — the 202 ack precedes the
        #: turn, so someone must keep the delivery task alive until it completes.
        self._reply_tasks: set[asyncio.Task[None]] = set()

    @property
    def reply_tasks(self) -> set[asyncio.Task[None]]:
        return self._reply_tasks

    async def accept(self, message: InternalMessage) -> tuple[int, dict[str, Any]]:
        """Enqueue ``message``, spawn its background reply task, and return the
        fast 202 ack immediately — the reply posts once the turn completes."""
        loop = asyncio.get_running_loop()
        item = QueueItem(message=message, reply=loop.create_future())
        await self._queue.put(item)
        task = loop.create_task(self._deliver(message, item.reply))
        self._reply_tasks.add(task)
        task.add_done_callback(self._reply_tasks.discard)
        return 202, dict(ACCEPTED_BODY)

    async def _deliver(self, message: InternalMessage, reply: asyncio.Future[str]) -> None:
        """Await the turn's reply and post it — the 202 ack already went out.

        Failures are logged, never raised: the webhook connection is long gone,
        so an exception here would only kill the task. The adapter's ONLY
        outbound call happens on this path — strictly after admission.
        """
        message_id = message.id
        try:
            reply_text = await asyncio.wait_for(reply, timeout=self._reply_timeout)
        except TimeoutError:
            logger.error(
                "%s turn %s timed out before a reply was produced", self._platform, message_id
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the core surfaces handling failures on the future
            logger.error("%s turn %s failed: %s", self._platform, message_id, exc)
            return
        try:
            await self._send_reply(message, reply_text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "%s reply for turn %s could not be posted: %s", self._platform, message_id, exc
            )

    async def cancel_inflight(self) -> None:
        """Cancel every in-flight delivery task cleanly (no orphan, no
        never-retrieved exception) — the shutdown half of the lifecycle."""
        for task in list(self._reply_tasks):
            task.cancel()
        if self._reply_tasks:
            await asyncio.gather(*self._reply_tasks, return_exceptions=True)
