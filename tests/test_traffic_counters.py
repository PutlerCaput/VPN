"""Traffic counters: cheap polls and a correct baseline after a panel reset."""

from __future__ import annotations

import os
from typing import Any

from db.database import Database


async def test_memory_counters_drop_older_days_when_the_day_changes(
    db: Database,
) -> None:
    for node, base in (("n1", 1), ("n2", 3)):
        await db.upsert_traffic_day(
            node,
            "2026-11-06",
            baseline_bytes=base,
            last_bytes=base,
            last_online_at=None,
        )
    for node, base in (("n1", 5), ("n2", 6)):
        await db.upsert_traffic_day(
            node,
            "2026-11-07",
            baseline_bytes=base,
            last_bytes=base,
            last_online_at=None,
        )

    assert await db.get_traffic_day("n1", "2026-11-06") is None
    assert await db.get_traffic_day("n2", "2026-11-06") is None
    assert (await db.get_traffic_day("n1", "2026-11-07"))["baseline_bytes"] == 5
    assert (await db.get_traffic_day("n2", "2026-11-07"))["baseline_bytes"] == 6


async def test_database_counter_reset_replaces_the_baseline() -> None:
    database = Database(
        ":memory:", os.environ.get("TEST_DB_KEY") or None, activity_logging_enabled=True
    )
    await database.connect()
    try:
        await database.upsert_traffic_day(
            "n1", "2026-11-07", baseline_bytes=100, last_bytes=150, last_online_at=None
        )
        # The panel counter was reset: the poll passes the new, lower baseline.
        await database.upsert_traffic_day(
            "n1", "2026-11-07", baseline_bytes=10, last_bytes=10, last_online_at=None
        )
        row = await database.get_traffic_day("n1", "2026-11-07")
        assert row is not None
        assert row["baseline_bytes"] == 10
        assert row["last_bytes"] == 10
    finally:
        await database.close()


async def test_unchanged_counters_are_not_written_again(
    services: Any, monkeypatch: Any
) -> None:
    writes: list[int] = []
    original = services.db.upsert_traffic_day

    async def counting(node: str, day: str, **values: Any) -> None:
        writes.append(values["last_bytes"])
        await original(node, day, **values)

    monkeypatch.setattr(services.db, "upsert_traffic_day", counting)
    monitor = services.monitor

    assert await monitor._track_traffic("n1", 1000, None) == (0, 0.0)
    assert await monitor._track_traffic("n1", 1000, None) == (0, 0.0)
    delta, _ = await monitor._track_traffic("n1", 3000, None)

    assert delta == 2000
    assert writes == [1000, 3000]
