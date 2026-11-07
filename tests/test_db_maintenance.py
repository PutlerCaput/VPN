"""Database cleanup removes only technical rows; the CLI keeps a copy first."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from db.database import Database
from db.maintenance import clean_apply, clean_counts, report

KEY = "ab" * 32
DAY = 86400
ROOT = Path(__file__).resolve().parent.parent


def _open(path: Path) -> Any:
    from sqlcipher3 import dbapi2 as sqlcipher

    conn = sqlcipher.connect(str(path))
    conn.execute(f"PRAGMA key = \"x'{KEY}'\"")
    return conn


async def _seed(path: Path) -> None:
    db = Database(str(path), KEY)
    await db.connect()
    now = int(time.time())
    old = now - 40 * DAY
    await db.create_user(1, "one", None)
    # payment id, status, created_at, draft state (None = no draft), has a check
    rows = [
        ("paid-old", "paid", old, None, True),
        ("cancel-old", "cancelled", old, "ready", True),
        ("review-old", "cancelled", old, "review", False),
        ("pending-old", "pending", old, None, True),
        ("paid-new", "paid", now, None, True),
    ]
    for payment_id, status, created, state, check in rows:
        await db.create_payment(
            payment_id=payment_id,
            telegram_id=1,
            amount=29900,
            currency="RUB",
            months=1,
            method="sbp",
            provider_id=f"yookassa:{payment_id}",
        )
        await db._execute(
            "UPDATE payments SET status = ?, created_at = ? WHERE payment_id = ?",
            (status, created, payment_id),
        )
        if state is not None:
            await db._execute(
                "INSERT INTO yookassa_creations (payment_id, shop_id, request_body, "
                "receipt_mode, state, retry_until) VALUES (?, ?, ?, ?, ?, ?)",
                (payment_id, "123", "{}", "self_employed", state, now + DAY),
            )
        if check:
            await db.schedule_payment_check(payment_id, now + 60)
    await db.close()


def test_counts_and_report_change_nothing(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    asyncio.run(_seed(path))
    now = int(time.time())
    conn = _open(path)
    before = clean_counts(conn, now)
    summary = report(conn, now)
    assert clean_counts(conn, now) == before
    assert summary["rows payments"] == 5
    conn.close()
    assert before == {
        "payment checks of finished orders": 2,
        "provider drafts of cancelled orders": 1,
    }


def test_clean_removes_only_finished_technical_rows(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    asyncio.run(_seed(path))
    now = int(time.time())
    conn = _open(path)
    deleted = clean_apply(conn, now)
    assert deleted == {
        "payment checks of finished orders": 2,
        "provider drafts of cancelled orders": 1,
    }
    # Every payment stays: payments are kept under the retention rules.
    assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 5
    # The draft under review and the checks of open and recent orders stay.
    assert conn.execute("SELECT COUNT(*) FROM yookassa_creations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM payment_checks").fetchone()[0] == 2
    conn.close()


def test_autoban_evidence_survives_the_retention_purge(tmp_path: Path) -> None:
    async def run() -> None:
        db = Database(str(tmp_path / "evidence.db"), KEY)
        await db.connect()
        old = int(time.time()) - 4 * 365 * DAY
        for kind in ("AUTOBAN", "scan"):
            await db._execute(
                "INSERT INTO anomalies (telegram_id, node_username, kind, detail, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (1, "u1", kind, "x", old),
            )
        for action in ("autoban", "test"):
            await db._execute(
                "INSERT INTO audit_log (telegram_id, action, detail, created_at) "
                "VALUES (?, ?, ?, ?)",
                (1, action, "x", old),
            )
        removed = await db.purge_retained_records(int(time.time()))
        kind_rows = await db._fetchall("SELECT kind FROM anomalies", ())
        action_rows = await db._fetchall("SELECT action FROM audit_log", ())
        kinds = [row["kind"] for row in kind_rows]
        actions = [row["action"] for row in action_rows]
        await db.close()
        assert removed == 2
        assert kinds == ["AUTOBAN"]
        assert actions == ["autoban"]

    asyncio.run(run())


def test_cli_keeps_a_checked_copy_before_it_cleans(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "db_key").write_text(KEY)
    path = data / "bot.db"
    asyncio.run(_seed(path))
    env = {**os.environ, "SECRETS_DIR": str(secrets)}
    result = subprocess.run(  # noqa: S603  # nosec B603 -- fixed arguments
        [
            sys.executable,
            str(ROOT / "scripts" / "db_maintenance.py"),
            "clean",
            str(path),
            "--apply",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CLEAN_OK" in result.stdout
    copies = list(data.glob("before-clean-*.db"))
    assert len(copies) == 1
    conn = _open(path)
    assert conn.execute("SELECT COUNT(*) FROM yookassa_creations").fetchone()[0] == 1
    conn.close()
    conn = _open(copies[0])
    assert conn.execute("SELECT COUNT(*) FROM yookassa_creations").fetchone()[0] == 2
    conn.close()


def test_cli_dry_run_deletes_nothing(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "db_key").write_text(KEY)
    path = data / "bot.db"
    asyncio.run(_seed(path))
    env = {**os.environ, "SECRETS_DIR": str(secrets)}
    result = subprocess.run(  # noqa: S603  # nosec B603 -- fixed arguments
        [
            sys.executable,
            str(ROOT / "scripts" / "db_maintenance.py"),
            "clean",
            str(path),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DRY RUN" in result.stdout
    assert not list(data.glob("before-clean-*.db"))
    conn = _open(path)
    assert conn.execute("SELECT COUNT(*) FROM yookassa_creations").fetchone()[0] == 2
    conn.close()
