"""Old databases get the widened payments constraints without losing rows."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from db.database import Database

KEY = "ab" * 32

OLD_PAYMENTS = """
CREATE TABLE payments (
    payment_id TEXT PRIMARY KEY,
    telegram_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL CHECK (currency IN ('RUB', 'USDT')),
    months INTEGER NOT NULL CHECK (months IN (1, 3, 6, 12)),
    method TEXT NOT NULL CHECK (method IN ('sbp', 'usdt')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'paid', 'cancelled')),
    provider_id TEXT,
    paid_at INTEGER,
    tier TEXT NOT NULL DEFAULT 'basic',
    created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_payments_provider
    ON payments(provider_id) WHERE provider_id IS NOT NULL;
CREATE TABLE payment_checks (
    payment_id TEXT PRIMARY KEY REFERENCES payments(payment_id) ON DELETE CASCADE,
    next_check_at INTEGER NOT NULL
);
CREATE TABLE yookassa_creations (
    payment_id TEXT PRIMARY KEY REFERENCES payments(payment_id) ON DELETE CASCADE,
    shop_id TEXT NOT NULL,
    request_body TEXT NOT NULL,
    receipt_mode TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'ready', 'review')),
    confirmation_url TEXT,
    retry_until INTEGER NOT NULL,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    lease_until INTEGER NOT NULL DEFAULT 0
);
INSERT INTO payments VALUES
    ('a1', 7, 30000, 'RUB', 1, 'sbp', 'paid', 'yookassa:x1', 100, 'basic', 50),
    ('a2', 7, 350, 'USDT', 3, 'usdt', 'pending', 'cryptopay:9', NULL, 'prime', 60),
    ('a3', 8, 80000, 'RUB', 12, 'sbp', 'pending', NULL, NULL, 'basic', 70);
INSERT INTO payment_checks VALUES ('a2', 1000);
INSERT INTO yookassa_creations
    (payment_id, shop_id, request_body, receipt_mode, retry_until)
    VALUES ('a3', 'shop', '{}', 'self_employed', 9999);
"""


def _create_old(path: Path, key: str | None) -> None:
    if key is None:
        conn: Any = sqlite3.connect(path)
    else:
        from sqlcipher3 import dbapi2 as sqlcipher

        conn = sqlcipher.connect(str(path))
        conn.execute(f"PRAGMA key = \"x'{key}'\"")
    conn.executescript(OLD_PAYMENTS)
    conn.commit()
    conn.close()


@pytest.mark.parametrize("key", [None, KEY], ids=["sqlite", "sqlcipher"])
async def test_rebuild_keeps_rows_and_allows_new_values(
    tmp_path: Path, key: str | None
) -> None:
    path = tmp_path / "old.db"
    _create_old(path, key)

    db = Database(str(path), key)
    await db.connect()
    try:
        rows = await db._fetchall("SELECT * FROM payments ORDER BY payment_id")
        assert [r["payment_id"] for r in rows] == ["a1", "a2", "a3"]
        assert rows[0]["provider_id"] == "yookassa:x1"
        assert rows[1]["tier"] == "prime"
        checks = await db._fetchall("SELECT * FROM payment_checks")
        assert checks == [{"payment_id": "a2", "next_check_at": 1000}]
        creation = await db.get_yookassa_creation("a3")
        assert creation is not None
        assert creation["shop_id"] == "shop"

        await db.create_payment(
            payment_id="b1",
            telegram_id=7,
            amount=250,
            currency="XTR",
            months=2,
            method="stars",
            tier="standard",
        )
        stored = await db.get_payment("b1")
        assert stored is not None
        assert stored["months"] == 2

        # The unique provider index survived the rebuild.
        with pytest.raises(sqlite3.IntegrityError if key is None else Exception):
            await db.set_payment_provider_id("a3", "yookassa:x1")

        # Foreign keys are enforced again and cascade as before.
        async with db.transaction() as conn:
            await conn.execute("DELETE FROM payments WHERE payment_id = 'a2'")
        assert await db._fetchall("SELECT * FROM payment_checks") == []
    finally:
        await db.close()

    # A second start does not rebuild again and keeps the data.
    again = Database(str(path), key)
    await again.connect()
    try:
        assert await again.get_payment("b1") is not None
        meta = await again._fetchone(
            "SELECT value FROM app_meta WHERE key = 'payments_constraints_v2'"
        )
        assert meta is not None
    finally:
        await again.close()


async def test_new_database_has_wide_constraints() -> None:
    db = Database(":memory:")
    await db.connect()
    try:
        meta = await db._fetchone(
            "SELECT value FROM app_meta WHERE key = 'payments_constraints_v2'"
        )
        assert meta is None  # created directly with the new definition
        await db.create_payment(
            payment_id="c1",
            telegram_id=1,
            amount=500,
            currency="XTR",
            months=5,
            method="stars",
        )
        with pytest.raises(sqlite3.IntegrityError):
            await db.create_payment(
                payment_id="c2",
                telegram_id=1,
                amount=1,
                currency="EUR",
                months=1,
                method="stars",
            )
    finally:
        await db.close()


def test_migrate_check_reports_ok_and_leaves_the_original(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    path = tmp_path / "live.db"
    _create_old(path, KEY)
    from sqlcipher3 import dbapi2 as sqlcipher

    conn = sqlcipher.connect(str(path))
    conn.execute(f"PRAGMA key = \"x'{KEY}'\"")
    # A live database already has app_meta; the migration adds a marker row.
    conn.execute("CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO app_meta VALUES ('retry_jobs_v1_migrated', '1')")
    conn.commit()
    conn.close()
    original = path.read_bytes()
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(  # noqa: S603 -- fixed interpreter and script
        [
            sys.executable,
            str(root / "scripts" / "db_tool.py"),
            "migrate-check",
            str(path),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "DB_KEY": KEY, "SECRETS_DIR": str(tmp_path / "none")},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "MIGRATION_CHECK_OK" in result.stdout
    assert "payments: 3 -> 3" in result.stdout
    assert "app_meta: 1 -> 2" in result.stdout
    assert KEY not in result.stdout + result.stderr
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.migrate-check*"))
