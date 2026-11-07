"""Report and cleanup of technical rows in the bot's live database.

Payments, receipts, offer acceptances, users and the admin trail are never
removed here: they follow legal/privacy.md, section 7. The bot already removes
expired login tokens, old connection logs, traffic days and support links every
hour (services/scheduler.py, housekeeping). The report counts the rows that are
still waiting for that job: a large number means the job is not running.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

DAY = 86400
# A finished order does not need its bookkeeping rows after this long: the
# late-payment window is 24 hours and checkout links expire long before.
FINISHED_ORDER_SECONDS = 30 * DAY
# The same periods as the housekeeping job and legal/privacy.md, section 7.
CONN_LOG_DAYS = 180
TRAFFIC_DAY_DAYS = 7
SUPPORT_DAYS = 365
INCIDENT_DAYS = 3 * 365

TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# label: (count statement, delete statement). Both take one parameter: the cutoff.
# Drafts in state 'review' are kept: an administrator may still need them.
TECHNICAL: dict[str, tuple[str, str]] = {
    "payment checks of finished orders": (
        "SELECT COUNT(*) FROM payment_checks WHERE payment_id IN (SELECT payment_id "
        "FROM payments WHERE status IN ('paid', 'cancelled') AND created_at < ?)",
        "DELETE FROM payment_checks WHERE payment_id IN (SELECT payment_id "
        "FROM payments WHERE status IN ('paid', 'cancelled') AND created_at < ?)",
    ),
    "provider drafts of cancelled orders": (
        "SELECT COUNT(*) FROM yookassa_creations WHERE state != 'review' "
        "AND payment_id IN (SELECT payment_id FROM payments "
        "WHERE status = 'cancelled' AND created_at < ?)",
        "DELETE FROM yookassa_creations WHERE state != 'review' "
        "AND payment_id IN (SELECT payment_id FROM payments "
        "WHERE status = 'cancelled' AND created_at < ?)",
    ),
}


def _cutoff(now: int) -> int:
    return now - FINISHED_ORDER_SECONDS


def _table_names(conn: Any) -> list[str]:
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    for name in names:
        if not TABLE_NAME_RE.fullmatch(name):
            raise ValueError(f"unexpected table name: {name!r}")
    return names


def _backlog(conn: Any, now: int) -> dict[str, int]:
    """Rows the bot's own housekeeping removes on its next hourly run."""
    day_cutoff = (
        datetime.fromtimestamp(now, UTC) - timedelta(days=TRAFFIC_DAY_DAYS)
    ).strftime("%Y-%m-%d")
    queries: dict[str, tuple[str, tuple[Any, ...]]] = {
        "expired login tokens": (
            "SELECT COUNT(*) FROM refresh_tokens WHERE expires_at < ?",
            (now,),
        ),
        f"connection logs over {CONN_LOG_DAYS} days": (
            "SELECT COUNT(*) FROM conn_logs WHERE connected_at < ?",
            (now - CONN_LOG_DAYS * DAY,),
        ),
        f"traffic days over {TRAFFIC_DAY_DAYS} days": (
            "SELECT COUNT(*) FROM traffic_daily WHERE day < ?",
            (day_cutoff,),
        ),
        f"support links over {SUPPORT_DAYS} days": (
            "SELECT COUNT(*) FROM support_messages WHERE created_at < ?",
            (now - SUPPORT_DAYS * DAY,),
        ),
        "incidents over 3 years, not AUTOBAN": (
            "SELECT COUNT(*) FROM anomalies WHERE created_at < ? AND kind != 'AUTOBAN'",
            (now - INCIDENT_DAYS * DAY,),
        ),
        "admin trail over 3 years, not autoban": (
            "SELECT COUNT(*) FROM audit_log WHERE created_at < ? "
            "AND action != 'autoban'",
            (now - INCIDENT_DAYS * DAY,),
        ),
    }
    return {
        label: int(conn.execute(sql, params).fetchone()[0])
        for label, (sql, params) in queries.items()
    }


def report(conn: Any, now: int) -> dict[str, int]:
    """Row counts, reclaimable space and what would be removed. Changes nothing."""
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    result: dict[str, int] = {"reclaimable bytes": free_pages * page_size}
    for name in _table_names(conn):
        count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]  # noqa: S608  # nosec B608 -- validated identifier
        result[f"rows {name}"] = int(count)
    for label, (count_sql, _) in TECHNICAL.items():
        count = conn.execute(count_sql, (_cutoff(now),)).fetchone()[0]
        result[f"technical, removed by clean: {label}"] = int(count)
    for label, value in _backlog(conn, now).items():
        result[f"housekeeping backlog: {label}"] = value
    return result


def clean_counts(conn: Any, now: int) -> dict[str, int]:
    """How many rows clean_apply would delete. Changes nothing."""
    return {
        label: int(conn.execute(count_sql, (_cutoff(now),)).fetchone()[0])
        for label, (count_sql, _) in TECHNICAL.items()
    }


def clean_apply(conn: Any, now: int) -> dict[str, int]:
    """Delete the technical rows of finished orders and commit."""
    deleted: dict[str, int] = {}
    for label, (_, delete_sql) in TECHNICAL.items():
        deleted[label] = int(conn.execute(delete_sql, (_cutoff(now),)).rowcount)
    conn.commit()
    return deleted
