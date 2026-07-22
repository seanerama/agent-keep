"""Channel-lifecycle conformance suite (ENH-06 / #125, REL-01).

**SeenIdCache** unit tests — the bounded, ephemeral seen-id set cannot
grow without limit: it evicts by SIZE (past the cap the oldest ids drop) AND
by AGE (past the TTL an id drops), and a re-sighting inside the window is a
duplicate. (No restart-safety assertion — lost-on-restart is the accepted,
documented trade-off; a fresh cache starting empty is CORRECT.)

The parametrized lifecycle-conformance layer (duplicate/replay, async-ack,
timeout, cancellation across the platform channels) rode with the webex and
slack adapters, which are not carried in this transplant; it returns with the
first platform channel intake.
"""

from agent_runtime.components.channel_lifecycle import SeenIdCache

# --------------------------------------------------------------- SeenIdCache


def test_seen_cache_recognizes_a_duplicate_within_the_window() -> None:
    cache = SeenIdCache(max_size=10, ttl_seconds=100.0, clock=lambda: 5.0)
    assert cache.seen_then_record("m1") is False  # first sight — record it
    assert cache.seen_then_record("m1") is True  # a re-sighting is a duplicate
    assert "m1" in cache


def test_seen_cache_is_bounded_by_size() -> None:
    """Past the cap, the OLDEST ids are evicted — memory stays bounded under a
    stream of distinct ids, and an evicted id is no longer a duplicate."""
    now = [0.0]
    cache = SeenIdCache(max_size=3, ttl_seconds=1e9, clock=lambda: now[0])
    for i in range(6):
        now[0] += 1.0
        assert cache.seen_then_record(f"id{i}") is False
    assert len(cache) == 3, "the set must not grow past its size cap"
    # id0..id2 were evicted (oldest-first); id0 seen again is NOT a duplicate.
    assert cache.seen_then_record("id0") is False
    # id5 is still within the (now re-shuffled) window → a duplicate.
    assert cache.seen_then_record("id5") is True


def test_seen_cache_is_bounded_by_ttl() -> None:
    """Past the TTL an id is evicted by AGE, so a genuine later re-delivery is
    not a duplicate (the same reason lost-on-restart is acceptable)."""
    now = [1000.0]
    cache = SeenIdCache(max_size=1000, ttl_seconds=60.0, clock=lambda: now[0])
    assert cache.seen_then_record("x") is False
    assert cache.seen_then_record("x") is True  # inside the window
    now[0] += 61.0  # advance PAST the TTL
    assert cache.seen_then_record("x") is False  # expired → recorded anew
    assert len(cache) == 1


def test_seen_cache_never_grows_unbounded_under_a_flood_of_distinct_ids() -> None:
    now = [0.0]
    cache = SeenIdCache(max_size=128, ttl_seconds=1e9, clock=lambda: now[0])
    for i in range(20_000):
        now[0] += 1.0
        cache.seen_then_record(f"id{i}")
    assert len(cache) <= 128, "a flood of distinct ids must stay bounded by the cap"
