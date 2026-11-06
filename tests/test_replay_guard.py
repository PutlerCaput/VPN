from __future__ import annotations

import asyncio

from api.auth import ReplayGuard


def test_rejects_reuse_within_ttl() -> None:
    guard = ReplayGuard(ttl=300)
    assert guard.check_and_add("h1", now=0)
    assert not guard.check_and_add("h1", now=299)
    assert guard.check_and_add("h1", now=301)


def test_bounded_evicts_oldest() -> None:
    guard = ReplayGuard(ttl=300, max_entries=3)
    for i, key in enumerate(["a", "b", "c", "d"]):
        assert guard.check_and_add(key, now=float(i))
    assert len(guard) == 3
    assert guard.check_and_add("a", now=5)  # evicted, so accepted again


def test_cleanup_removes_expired_only() -> None:
    guard = ReplayGuard(ttl=10)
    guard.check_and_add("old", now=0)
    guard.check_and_add("new", now=8)
    guard.cleanup(now=11)
    assert len(guard) == 1
    assert not guard.check_and_add("new", now=12)


def test_memory_is_bounded_under_flood() -> None:
    guard = ReplayGuard(ttl=300, max_entries=10_000)
    for i in range(25_000):
        guard.check_and_add(f"h{i}", now=1.0)
    assert len(guard) == 10_000


async def test_cleanup_task_lifecycle() -> None:
    guard = ReplayGuard(ttl=0.01)
    guard.check_and_add("x")
    guard.start(interval=0.02)
    await asyncio.sleep(0.08)
    assert len(guard) == 0
    await guard.stop()
