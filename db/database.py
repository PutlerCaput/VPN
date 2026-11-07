"""SQLite (WAL) persistence layer, encrypted at rest with SQLCipher when a key is set.

All timestamps are unix seconds (UTC). Money is stored in minor units:
kopecks for RUB, cents for USDT. Every query is parameterized.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

import aiosqlite

MINIMUM_REMINDER_PERIOD_SECONDS = 3 * 24 * 60 * 60
# An unpaid order is cancelled after this long; a provider-bound order is
# cancelled only once the provider confirms it is still unpaid.
UNPAID_ORDER_TTL_SECONDS = 60 * 60
# A cancelled order still counts as a possible late payment for this long.
LATE_PAYMENT_WINDOW_SECONDS = 24 * 60 * 60

# Kept separate so an old database can be rebuilt with the same definition:
# SQLite cannot change CHECK constraints in place. XTR is Telegram Stars
# (whole stars, no minor unit); RUB/USDT amounts stay in kopecks/cents.
PAYMENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
    payment_id TEXT PRIMARY KEY,
    telegram_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL CHECK (currency IN ('RUB', 'USDT', 'XTR')),
    months INTEGER NOT NULL CHECK (months BETWEEN 1 AND 12),
    method TEXT NOT NULL CHECK (method IN ('sbp', 'usdt', 'stars')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'paid', 'cancelled', 'refunded')),
    provider_id TEXT,
    paid_at INTEGER,
    tier TEXT NOT NULL DEFAULT 'basic',
    created_at INTEGER NOT NULL,
    subscription_id TEXT,
    channel TEXT
);
"""
PAYMENTS_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(telegram_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_provider
    ON payments(provider_id) WHERE provider_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_payments_pending ON payments(status, created_at);
"""
PAYMENTS_COLUMNS = (
    "payment_id, telegram_id, amount, currency, months, method, status, "
    "provider_id, paid_at, tier, created_at, subscription_id, channel"
)

SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'suspended', 'expired', 'banned')),
    expires_at INTEGER,
    node_username TEXT UNIQUE,
    referred_by INTEGER,
    config_link TEXT,
    trial_used INTEGER NOT NULL DEFAULT 0,
    expiry_reminded_for INTEGER,
    receipt_email TEXT,
    tier TEXT NOT NULL DEFAULT 'basic',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by);
CREATE INDEX IF NOT EXISTS idx_users_status_expires ON users(status, expires_at);
"""
    + PAYMENTS_TABLE_SQL.format(table="payments")
    + PAYMENTS_INDEXES_SQL
    + """

-- A persisted schedule makes reconciliation fair across restarts and outages.
CREATE TABLE IF NOT EXISTS payment_checks (
    payment_id TEXT PRIMARY KEY REFERENCES payments(payment_id) ON DELETE CASCADE,
    next_check_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS yookassa_creations (
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
CREATE INDEX IF NOT EXISTS idx_yookassa_creation_retry
    ON yookassa_creations(state, next_attempt_at);

CREATE TABLE IF NOT EXISTS conn_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    node_username TEXT NOT NULL,
    connected_at INTEGER NOT NULL,
    disconnected_at INTEGER,
    bytes_up INTEGER NOT NULL DEFAULT 0,
    bytes_down INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_conn_logs_user ON conn_logs(telegram_id, connected_at);

CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER,
    node_username TEXT,
    kind TEXT NOT NULL,
    detail TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS offer_acceptances (
    telegram_id INTEGER PRIMARY KEY,
    accepted_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    jti TEXT PRIMARY KEY,
    telegram_id INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS referral_bonuses (
    invitee_id INTEGER PRIMARY KEY,
    inviter_id INTEGER NOT NULL,
    inviter_days INTEGER NOT NULL,
    invitee_days INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

-- External provisioning is not part of the SQLite transaction. These small
-- job tables make retries safe after a process crash or a temporary Marzban
-- outage. Expiration targets are absolute, so retrying never adds the same
-- paid period or referral bonus twice.
CREATE TABLE IF NOT EXISTS payment_activations (
    payment_id TEXT PRIMARY KEY,
    target_expires_at INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'completed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_attempt_at INTEGER,
    created_at INTEGER NOT NULL,
    completed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_payment_activations_status
    ON payment_activations(status, created_at);

CREATE TABLE IF NOT EXISTS referral_bonus_jobs (
    invitee_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'completed')),
    inviter_target_expires_at INTEGER,
    invitee_target_expires_at INTEGER,
    inviter_applied_at INTEGER,
    invitee_applied_at INTEGER,
    inviter_attempts INTEGER NOT NULL DEFAULT 0,
    invitee_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    completed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_referral_bonus_jobs_status
    ON referral_bonus_jobs(status, created_at);

CREATE TABLE IF NOT EXISTS pending_bans (
    telegram_id INTEGER PRIMARY KEY,
    node_username TEXT,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    last_attempt_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pending_bans_created
    ON pending_bans(created_at);

CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS traffic_daily (
    node_username TEXT NOT NULL,
    day TEXT NOT NULL,
    baseline_bytes INTEGER NOT NULL,
    last_bytes INTEGER NOT NULL,
    last_online_at TEXT,
    soft_flagged INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (node_username, day)
);

CREATE TABLE IF NOT EXISTS support_messages (
    admin_message_id INTEGER PRIMARY KEY,
    telegram_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

-- Monthly Telegram Stars subscriptions (auto-renewal). Each charge becomes
-- its own row in payments, so every renewal is credited exactly once.
CREATE TABLE IF NOT EXISTS star_subscriptions (
    subscription_id TEXT PRIMARY KEY,
    telegram_id INTEGER NOT NULL,
    tier TEXT NOT NULL,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'canceled', 'failed', 'ended')),
    first_charge_id TEXT,
    last_charge_id TEXT,
    next_charge_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_star_subscriptions_user
    ON star_subscriptions(telegram_id, status);

-- AmneziaWG: one key pair per subscriber device (private key kept so the
-- config can be sent again; the file is encrypted with SQLCipher in production).
CREATE TABLE IF NOT EXISTS awg_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    public_key TEXT NOT NULL UNIQUE,
    private_key TEXT NOT NULL,
    preshared_key TEXT NOT NULL,
    address TEXT NOT NULL UNIQUE,
    last_counter INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_awg_devices_user ON awg_devices(telegram_id);

-- AmneziaWG traffic in the user's current Marzban traffic window
-- (window_key = Marzban lifetime minus current usage; -1 = not known yet).
CREATE TABLE IF NOT EXISTS awg_usage (
    telegram_id INTEGER PRIMARY KEY,
    window_key INTEGER NOT NULL,
    bytes INTEGER NOT NULL DEFAULT 0
);

-- Quota notices are sent once per traffic window and threshold. window_start
-- is the traffic counted before the current window (lifetime minus current
-- usage); it changes exactly when the panel resets the counter. No history
-- of visited resources or connection times is kept here.
CREATE TABLE IF NOT EXISTS traffic_notices (
    node_username TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    level INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (node_username, window_start, level)
);
"""
)


# Columns added after the first release: (table, column, definition).
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("users", "receipt_email", "TEXT"),
    ("users", "tier", "TEXT NOT NULL DEFAULT 'basic'"),
    ("payments", "tier", "TEXT NOT NULL DEFAULT 'basic'"),
    ("payments", "subscription_id", "TEXT"),
    # 'telegram' = ruble invoice paid inside Telegram (YooKassa provider token).
    ("payments", "channel", "TEXT"),
)
HEX_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def now() -> int:
    return int(time.time())


class DatabaseKeyError(RuntimeError):
    pass


class PendingPaymentLimitError(RuntimeError):
    pass


class Database:
    def __init__(
        self,
        path: str,
        key: str | None = None,
        *,
        activity_logging_enabled: bool = False,
    ) -> None:
        if key is not None and not HEX_KEY_RE.fullmatch(key):
            # Validated here because the key is interpolated into PRAGMA key.
            raise DatabaseKeyError("database key must be 64 hex characters")
        self.activity_logging_enabled = activity_logging_enabled
        self._traffic_memory: OrderedDict[tuple[str, str], dict[str, Any]] = (
            OrderedDict()
        )
        self.path = path
        self._key = key
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def encrypted(self) -> bool:
        return self._key is not None

    # ------------------------------------------------------------------ infra
    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if self._key is None:
            self._conn = await aiosqlite.connect(self.path)
            self._conn.row_factory = aiosqlite.Row
        else:
            from sqlcipher3 import dbapi2 as sqlcipher

            self._conn = await aiosqlite.Connection(
                partial(sqlcipher.connect, self.path, check_same_thread=False),
                iter_chunk_size=64,
            )
            self._conn.row_factory = sqlcipher.Row
            await self._conn.execute(f"PRAGMA key = \"x'{self._key}'\"")
            try:
                await self._conn.execute("SELECT count(*) FROM sqlite_master")
            except sqlcipher.DatabaseError:
                await self._conn.close()
                self._conn = None
                raise DatabaseKeyError(
                    "cannot open the database with DB_KEY: wrong key, or the file "
                    "is still unencrypted (run scripts/db_tool.py encrypt)"
                ) from None
            # sqlcipher3 opens an implicit transaction here; WAL needs none.
            await self._conn.commit()
        try:
            await self._initialize()
        except BaseException:
            await self.close()
            raise

    async def _initialize(self) -> None:
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
                columns = {row[1] for row in await cur.fetchall()}
            if column not in columns:
                await self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )
        await self._rebuild_payments_constraints()
        # Run the one-time migration baseline exactly once. If it ran on every
        # startup, a paid webhook interrupted between marking the payment and
        # creating its activation job could be mistaken for historical work
        # and silently marked completed on the next restart.
        async with self.conn.execute(
            "SELECT value FROM app_meta WHERE key = 'retry_jobs_v1_migrated'"
        ) as cur:
            migration = await cur.fetchone()
        if migration is None:
            async with self.transaction() as conn:
                await conn.execute(
                    "INSERT OR IGNORE INTO payment_activations "
                    "(payment_id, status, created_at, completed_at) "
                    "SELECT payment_id, 'completed', COALESCE(paid_at, created_at), "
                    "COALESCE(paid_at, created_at) FROM payments WHERE status = 'paid'"
                )
                # Historical referral rows only prove that a reward was
                # attempted, not which side actually received it. Avoid
                # duplicate rewards; old misses need manual reconciliation.
                await conn.execute(
                    "INSERT OR IGNORE INTO referral_bonus_jobs "
                    "(invitee_id, status, inviter_applied_at, invitee_applied_at, "
                    "created_at, completed_at) "
                    "SELECT invitee_id, 'completed', created_at, created_at, "
                    "created_at, created_at FROM referral_bonuses"
                )
                await conn.execute(
                    "INSERT INTO app_meta (key, value) "
                    "VALUES ('retry_jobs_v1_migrated', '1')"
                )
        await self.conn.commit()

    async def _rebuild_payments_constraints(self) -> None:
        """Widen the payments CHECK constraints of a database created earlier.

        Old releases allowed only 1/3/6/12 months and SBP/USDT. SQLite cannot
        alter a CHECK constraint, so the table is rebuilt in one transaction
        (create, copy, drop, rename). Foreign keys must be off while the old
        table is dropped: otherwise ON DELETE CASCADE would delete every
        payment_checks and yookassa_creations row. Every row is preserved;
        a foreign-key violation aborts and rolls the whole rebuild back.
        """
        row = await self._fetchone(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'payments'"
        )
        if row is None or "'stars'" in str(row["sql"] or ""):
            return
        await self.conn.commit()
        await self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                await self.conn.execute(
                    PAYMENTS_TABLE_SQL.format(table="payments_rebuild")
                )
                await self.conn.execute(
                    f"INSERT INTO payments_rebuild ({PAYMENTS_COLUMNS}) "  # noqa: S608 -- constant column list
                    f"SELECT {PAYMENTS_COLUMNS} FROM payments"
                )
                async with self.conn.execute("SELECT COUNT(*) FROM payments") as cur:
                    before = (await cur.fetchone() or (0,))[0]
                async with self.conn.execute(
                    "SELECT COUNT(*) FROM payments_rebuild"
                ) as cur:
                    after = (await cur.fetchone() or (-1,))[0]
                if before != after:
                    raise RuntimeError("payments rebuild lost rows")
                await self.conn.execute("DROP TABLE payments")
                await self.conn.execute(
                    "ALTER TABLE payments_rebuild RENAME TO payments"
                )
                for statement in PAYMENTS_INDEXES_SQL.split(";"):
                    if statement.strip():
                        await self.conn.execute(statement)
                async with self.conn.execute("PRAGMA foreign_key_check") as cur:
                    if await cur.fetchone() is not None:
                        raise RuntimeError("payments rebuild broke a foreign key")
                await self.conn.execute(
                    "INSERT OR REPLACE INTO app_meta (key, value) "
                    "VALUES ('payments_constraints_v2', ?)",
                    (str(now()),),
                )
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise
        finally:
            await self.conn.execute("PRAGMA foreign_keys=ON")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock:
            try:
                yield self.conn
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise

    async def _fetchone(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> dict[str, Any] | None:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def _fetchall(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        async with self.transaction() as conn:
            cur = await conn.execute(sql, params)
            return cur.rowcount

    async def ping(self) -> bool:
        row = await self._fetchone("SELECT 1 AS ok")
        return bool(row and row["ok"] == 1)

    # ---------------------------------------------------------- receipts
    async def set_receipt_email(self, telegram_id: int, email: str) -> None:
        await self._execute(
            "UPDATE users SET receipt_email = ? WHERE telegram_id = ?",
            (email, telegram_id),
        )

    # ------------------------------------------------------------------ users
    async def get_user(self, telegram_id: int) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )

    async def get_user_by_node(self, node_username: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM users WHERE node_username = ?",
            (node_username,),
        )

    async def create_user(
        self,
        telegram_id: int,
        username: str | None,
        referred_by: int | None,
    ) -> bool:
        rowcount = await self._execute(
            "INSERT OR IGNORE INTO users "
            "(telegram_id, username, status, referred_by, created_at) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (telegram_id, username, referred_by, now()),
        )
        return rowcount == 1

    async def update_profile(
        self,
        telegram_id: int,
        username: str | None,
        first_name: str | None = None,
    ) -> None:
        await self._execute(
            "UPDATE users SET username = ?, "
            "first_name = COALESCE(?, first_name) WHERE telegram_id = ?",
            (username, first_name, telegram_id),
        )

    async def set_user_status(self, telegram_id: int, status: str) -> None:
        await self._execute(
            "UPDATE users SET status = ? WHERE telegram_id = ?",
            (status, telegram_id),
        )

    async def set_subscription(
        self,
        telegram_id: int,
        *,
        status: str,
        expires_at: int,
        node_username: str,
        config_link: str | None,
    ) -> None:
        await self._execute(
            "UPDATE users SET status = ?, expires_at = ?, node_username = ?, "
            "config_link = COALESCE(?, config_link), expiry_reminded_for = NULL "
            "WHERE telegram_id = ?",
            (status, expires_at, node_username, config_link, telegram_id),
        )

    async def set_user_tier(self, telegram_id: int, tier: str) -> None:
        await self._execute(
            "UPDATE users SET tier = ? WHERE telegram_id = ?",
            (tier, telegram_id),
        )

    async def set_config_link(self, telegram_id: int, config_link: str) -> None:
        await self._execute(
            "UPDATE users SET config_link = ? WHERE telegram_id = ?",
            (config_link, telegram_id),
        )

    async def mark_trial_used(self, telegram_id: int) -> None:
        await self._execute(
            "UPDATE users SET trial_used = 1 WHERE telegram_id = ?",
            (telegram_id,),
        )

    async def count_referrals(self, telegram_id: int) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM users WHERE referred_by = ?",
            (telegram_id,),
        )
        return int(row["n"]) if row else 0

    async def referral_bonus_days(self, telegram_id: int) -> int:
        row = await self._fetchone(
            "SELECT COALESCE(SUM(inviter_days), 0) AS d "
            "FROM referral_bonuses WHERE inviter_id = ?",
            (telegram_id,),
        )
        return int(row["d"]) if row else 0

    async def record_referral_bonus(
        self,
        inviter_id: int,
        invitee_id: int,
        inviter_days: int,
        invitee_days: int,
    ) -> bool:
        """Returns True only the first time a bonus is recorded for an invitee."""
        timestamp = now()
        async with self.transaction() as conn:
            cur = await conn.execute(
                "INSERT OR IGNORE INTO referral_bonuses "
                "(invitee_id, inviter_id, inviter_days, invitee_days, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (invitee_id, inviter_id, inviter_days, invitee_days, timestamp),
            )
            created = cur.rowcount == 1
            if created:
                await conn.execute(
                    "INSERT OR IGNORE INTO referral_bonus_jobs "
                    "(invitee_id, status, created_at) VALUES (?, 'pending', ?)",
                    (invitee_id, timestamp),
                )
            else:
                # A legacy row without a job is treated as completed by
                # _initialize; retain that safe default if encountered later.
                await conn.execute(
                    "INSERT OR IGNORE INTO referral_bonus_jobs "
                    "(invitee_id, status, inviter_applied_at, invitee_applied_at, "
                    "created_at, completed_at) "
                    "VALUES (?, 'completed', ?, ?, ?, ?)",
                    (invitee_id, timestamp, timestamp, timestamp, timestamp),
                )
        return created

    async def get_referral_bonus_job(self, invitee_id: int) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT j.*, r.inviter_id, r.inviter_days, r.invitee_days "
            "FROM referral_bonus_jobs j JOIN referral_bonuses r "
            "ON r.invitee_id = j.invitee_id WHERE j.invitee_id = ?",
            (invitee_id,),
        )

    async def pending_referral_bonus_jobs(self) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT j.invitee_id FROM referral_bonus_jobs j "
            "WHERE j.status = 'pending' ORDER BY j.created_at"
        )

    async def pending_referral_invitees(self) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT u.telegram_id FROM users u "
            "WHERE u.referred_by IS NOT NULL AND ("
            "EXISTS (SELECT 1 FROM referral_bonus_jobs j "
            "WHERE j.invitee_id = u.telegram_id AND j.status = 'pending') "
            "OR (SELECT COUNT(*) FROM payments p WHERE p.telegram_id = u.telegram_id "
            "AND p.status = 'paid') = 1 AND EXISTS ("
            "SELECT 1 FROM payments p JOIN payment_activations a "
            "ON a.payment_id = p.payment_id WHERE p.telegram_id = u.telegram_id "
            "AND p.status = 'paid' AND a.status = 'completed') AND NOT EXISTS ("
            "SELECT 1 FROM referral_bonuses r WHERE r.invitee_id = u.telegram_id)"
            ") ORDER BY u.created_at"
        )

    async def set_referral_bonus_target(
        self, invitee_id: int, role: str, target_expires_at: int
    ) -> int:
        if role not in {"inviter", "invitee"}:
            raise ValueError("invalid referral bonus role")
        column = f"{role}_target_expires_at"
        await self._execute(
            f"UPDATE referral_bonus_jobs SET {column} = COALESCE({column}, ?) "  # noqa: S608 -- role allowlist above
            "WHERE invitee_id = ?",
            (target_expires_at, invitee_id),
        )
        row = await self._fetchone(
            f"SELECT {column} AS target FROM referral_bonus_jobs "  # noqa: S608 -- role allowlist above
            "WHERE invitee_id = ?",
            (invitee_id,),
        )
        if row is None or row["target"] is None:
            raise RuntimeError("referral bonus job is missing")
        return int(row["target"])

    async def mark_referral_bonus_applied(self, invitee_id: int, role: str) -> None:
        if role not in {"inviter", "invitee"}:
            raise ValueError("invalid referral bonus role")
        applied = f"{role}_applied_at"
        attempts = f"{role}_attempts"
        async with self.transaction() as conn:
            await conn.execute(
                f"UPDATE referral_bonus_jobs SET {applied} = COALESCE({applied}, ?), "  # noqa: S608 -- role allowlist above
                f"{attempts} = {attempts} + 1 WHERE invitee_id = ?",
                (now(), invitee_id),
            )
            await conn.execute(
                "UPDATE referral_bonus_jobs SET status = 'completed', "
                "completed_at = ? WHERE invitee_id = ? "
                "AND inviter_applied_at IS NOT NULL "
                "AND invitee_applied_at IS NOT NULL",
                (now(), invitee_id),
            )

    async def record_referral_bonus_error(
        self, invitee_id: int, role: str, error: str
    ) -> None:
        if role not in {"inviter", "invitee"}:
            raise ValueError("invalid referral bonus role")
        attempts = f"{role}_attempts"
        await self._execute(
            f"UPDATE referral_bonus_jobs SET {attempts} = {attempts} + 1, "  # noqa: S608 -- role allowlist above
            "last_error = ? WHERE invitee_id = ?",
            (error[:500], invitee_id),
        )

    async def users_to_expire(self, at: int) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT * FROM users WHERE status IN ('active', 'suspended') "
            "AND expires_at IS NOT NULL AND expires_at <= ?",
            (at,),
        )

    async def users_to_remind(self, start: int, end: int) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT * FROM users WHERE status = 'active' "
            "AND expires_at > ? AND expires_at <= ? "
            "AND expires_at - created_at >= ? "
            "AND (expiry_reminded_for IS NULL OR expiry_reminded_for != expires_at)",
            (start, end, MINIMUM_REMINDER_PERIOD_SECONDS),
        )

    async def mark_reminded(self, telegram_id: int, expires_at: int) -> None:
        await self._execute(
            "UPDATE users SET expiry_reminded_for = ? WHERE telegram_id = ?",
            (expires_at, telegram_id),
        )

    # ------------------------------------------------------------------ offer
    async def accept_offer(self, telegram_id: int) -> None:
        await self._execute(
            "INSERT OR IGNORE INTO offer_acceptances (telegram_id, accepted_at) "
            "VALUES (?, ?)",
            (telegram_id, now()),
        )

    async def has_accepted_offer(self, telegram_id: int) -> bool:
        row = await self._fetchone(
            "SELECT 1 AS ok FROM offer_acceptances WHERE telegram_id = ?",
            (telegram_id,),
        )
        return row is not None

    # --------------------------------------------------------------- payments
    async def create_payment(
        self,
        *,
        payment_id: str,
        telegram_id: int,
        amount: int,
        currency: str,
        months: int,
        method: str,
        provider_id: str | None = None,
        tier: str = "basic",
    ) -> None:
        await self._execute(
            "INSERT INTO payments (payment_id, telegram_id, amount, currency, "
            "months, method, status, provider_id, tier, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                payment_id,
                telegram_id,
                amount,
                currency,
                months,
                method,
                provider_id,
                tier,
                now(),
            ),
        )

    async def set_payment_provider_id(self, payment_id: str, provider_id: str) -> None:
        await self._execute(
            "UPDATE payments SET provider_id = ? WHERE payment_id = ?",
            (provider_id, payment_id),
        )

    async def open_yookassa_creation(
        self, telegram_id: int, months: int, tier: str
    ) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT p.*, c.shop_id, c.request_body, c.receipt_mode, c.state, "
            "c.confirmation_url, c.retry_until FROM payments p "
            "JOIN yookassa_creations c ON c.payment_id = p.payment_id "
            "WHERE p.telegram_id = ? AND p.months = ? AND p.tier = ? "
            "AND p.status = 'pending' ORDER BY p.created_at LIMIT 1",
            (telegram_id, months, tier),
        )

    async def prepare_yookassa_creation(
        self,
        *,
        payment_id: str,
        telegram_id: int,
        amount: int,
        months: int,
        tier: str,
        shop_id: str,
        request_body: str,
        receipt_mode: str,
        retry_until: int,
        max_pending: int,
    ) -> dict[str, Any]:
        async with self.transaction() as conn:
            existing = await self.open_yookassa_creation(telegram_id, months, tier)
            if existing is not None:
                return existing
            if await self.count_pending_payments(telegram_id) >= max_pending:
                raise PendingPaymentLimitError("too_many_pending")
            await conn.execute(
                "INSERT INTO payments (payment_id, telegram_id, amount, currency, "
                "months, method, tier, created_at) "
                "VALUES (?, ?, ?, 'RUB', ?, 'sbp', ?, ?)",
                (payment_id, telegram_id, amount, months, tier, now()),
            )
            await conn.execute(
                "INSERT INTO yookassa_creations (payment_id, shop_id, request_body, "
                "receipt_mode, retry_until) VALUES (?, ?, ?, ?, ?)",
                (payment_id, shop_id, request_body, receipt_mode, retry_until),
            )
        created = await self.get_yookassa_creation(payment_id)
        if created is None:
            raise RuntimeError("payment creation was not persisted")
        return created

    async def get_yookassa_creation(self, payment_id: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT p.*, c.shop_id, c.request_body, c.receipt_mode, c.state, "
            "c.confirmation_url, c.retry_until FROM payments p "
            "JOIN yookassa_creations c ON c.payment_id = p.payment_id "
            "WHERE p.payment_id = ?",
            (payment_id,),
        )

    async def claim_yookassa_creation(self, payment_id: str, at: int) -> bool:
        updated = await self._execute(
            "UPDATE yookassa_creations SET lease_until = ?, next_attempt_at = ? "
            "WHERE payment_id = ? AND state = 'pending' AND lease_until <= ? "
            "AND retry_until > ?",
            (at + 60, at + 120, payment_id, at, at),
        )
        return updated == 1

    async def release_yookassa_creation(self, payment_id: str) -> None:
        await self._execute(
            "UPDATE yookassa_creations SET lease_until = 0 WHERE payment_id = ?",
            (payment_id,),
        )

    async def review_yookassa_creation(self, payment_id: str) -> None:
        await self._execute(
            "UPDATE yookassa_creations SET state = 'review', lease_until = 0 "
            "WHERE payment_id = ? AND state = 'pending'",
            (payment_id,),
        )

    async def complete_yookassa_creation(
        self, payment_id: str, provider_id: str, confirmation_url: str | None
    ) -> None:
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE payments SET provider_id = ? WHERE payment_id = ? "
                "AND provider_id IS NULL",
                (provider_id, payment_id),
            )
            saved = await self.get_payment(payment_id)
            if saved is None or saved["provider_id"] != provider_id:
                raise RuntimeError("payment provider binding conflict")
            await conn.execute(
                "UPDATE yookassa_creations SET state = 'ready', confirmation_url = ?, "
                "lease_until = 0 WHERE payment_id = ?",
                (confirmation_url, payment_id),
            )

    async def yookassa_creations_to_retry(
        self, at: int, limit: int = 5
    ) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT p.*, c.shop_id, c.request_body, c.receipt_mode, c.state, "
            "c.confirmation_url, c.retry_until FROM payments p "
            "JOIN yookassa_creations c ON c.payment_id = p.payment_id "
            "WHERE p.status = 'pending' AND c.state = 'pending' "
            "AND c.next_attempt_at <= ? AND c.lease_until <= ? "
            "ORDER BY c.next_attempt_at, p.created_at LIMIT ?",
            (at, at, limit),
        )

    async def get_payment(self, payment_id: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM payments WHERE payment_id = ?",
            (payment_id,),
        )

    async def get_payment_by_provider(self, provider_id: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM payments WHERE provider_id = ?",
            (provider_id,),
        )

    async def mark_payment_paid(self, payment_id: str) -> bool:
        """Atomically moves pending -> paid. False if already processed."""
        rowcount = await self._execute(
            "UPDATE payments SET status = 'paid', paid_at = ? "
            "WHERE payment_id = ? AND status IN ('pending', 'cancelled')",
            (now(), payment_id),
        )
        return rowcount == 1

    async def open_stars_payment(
        self, telegram_id: int, months: int, tier: str, since: int
    ) -> dict[str, Any] | None:
        """A recent unpaid Stars order of the same plan, reused for a new invoice."""
        return await self._fetchone(
            "SELECT * FROM payments WHERE telegram_id = ? AND months = ? AND tier = ? "
            "AND method = 'stars' AND status = 'pending' AND provider_id IS NULL "
            "AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
            (telegram_id, months, tier, since),
        )

    async def bind_stars_charge(self, payment_id: str, provider_id: str) -> bool:
        """Attach Telegram's charge ID to the order the stars were paid for.

        Telegram has already taken the stars, so an order cancelled meanwhile
        as stale is reopened instead of losing the payment. Returns False if
        the order is bound to a different charge (never rebinds).
        """
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE payments SET provider_id = ?, status = CASE "
                "WHEN status = 'cancelled' THEN 'pending' ELSE status END "
                "WHERE payment_id = ? AND method = 'stars' AND provider_id IS NULL",
                (provider_id, payment_id),
            )
            cur = await conn.execute(
                "SELECT provider_id FROM payments WHERE payment_id = ?",
                (payment_id,),
            )
            row = await cur.fetchone()
        return row is not None and row[0] == provider_id

    # ------------------------------------------------------------ amneziawg
    async def awg_devices(self, telegram_id: int) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT * FROM awg_devices WHERE telegram_id = ? ORDER BY id",
            (telegram_id,),
        )

    async def all_awg_devices(self) -> list[dict[str, Any]]:
        return await self._fetchall("SELECT * FROM awg_devices ORDER BY id")

    async def get_awg_device(self, device_id: int) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM awg_devices WHERE id = ?", (device_id,)
        )

    async def add_awg_device(
        self,
        *,
        telegram_id: int,
        name: str,
        public_key: str,
        private_key: str,
        preshared_key: str,
        address: str,
    ) -> dict[str, Any]:
        async with self.transaction() as conn:
            cur = await conn.execute(
                "INSERT INTO awg_devices (telegram_id, name, public_key, private_key, "
                "preshared_key, address, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    telegram_id,
                    name,
                    public_key,
                    private_key,
                    preshared_key,
                    address,
                    now(),
                ),
            )
            device_id = cur.lastrowid
        device = await self.get_awg_device(int(device_id or 0))
        if device is None:
            raise RuntimeError("device was not stored")
        return device

    async def delete_awg_device(self, device_id: int) -> None:
        await self._execute("DELETE FROM awg_devices WHERE id = ?", (device_id,))

    async def set_awg_counter(self, device_id: int, counter: int) -> None:
        await self._execute(
            "UPDATE awg_devices SET last_counter = ? WHERE id = ?",
            (counter, device_id),
        )

    async def get_awg_usage(self, telegram_id: int) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM awg_usage WHERE telegram_id = ?", (telegram_id,)
        )

    async def set_awg_usage(self, telegram_id: int, window_key: int, delta: int) -> int:
        """Add traffic to the window; a new window starts from this traffic.

        Traffic counted while the window was unknown (-1) is carried over.
        Returns the bytes of the current window.
        """
        async with self.transaction() as conn:
            cur = await conn.execute(
                "SELECT window_key, bytes FROM awg_usage WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = await cur.fetchone()
            if row is not None and int(row[0]) in (window_key, -1):
                total = int(row[1]) + delta
            else:
                total = delta
            await conn.execute(
                "INSERT INTO awg_usage (telegram_id, window_key, bytes) "
                "VALUES (?, ?, ?) ON CONFLICT(telegram_id) DO UPDATE SET "
                "window_key = excluded.window_key, bytes = excluded.bytes",
                (telegram_id, window_key, total),
            )
        return total

    async def add_awg_usage_bytes(self, telegram_id: int, delta: int) -> None:
        """Traffic seen while the Marzban window is unknown."""
        await self._execute(
            "INSERT INTO awg_usage (telegram_id, window_key, bytes) VALUES (?, -1, ?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET bytes = bytes + excluded.bytes",
            (telegram_id, delta),
        )

    # ------------------------------------------------------ star subscriptions
    async def create_star_subscription(
        self, subscription_id: str, telegram_id: int, tier: str, amount: int
    ) -> None:
        timestamp = now()
        await self._execute(
            "INSERT INTO star_subscriptions (subscription_id, telegram_id, tier, "
            "amount, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (subscription_id, telegram_id, tier, amount, timestamp, timestamp),
        )

    async def get_star_subscription(
        self, subscription_id: str
    ) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM star_subscriptions WHERE subscription_id = ?",
            (subscription_id,),
        )

    async def user_star_subscriptions(self, telegram_id: int) -> list[dict[str, Any]]:
        """Every subscription of a user, newest first; callers filter by status."""
        return await self._fetchall(
            "SELECT * FROM star_subscriptions WHERE telegram_id = ? "
            "ORDER BY created_at DESC",
            (telegram_id,),
        )

    async def open_star_subscription(
        self, telegram_id: int, tier: str, since: int
    ) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM star_subscriptions WHERE telegram_id = ? AND tier = ? "
            "AND status = 'pending' AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT 1",
            (telegram_id, tier, since),
        )

    async def record_subscription_charge(
        self,
        *,
        subscription_id: str,
        payment_id: str,
        provider_id: str,
        next_charge_at: int,
    ) -> str:
        """Store one subscription charge as a pending payment; returns its ID.

        A charge delivered twice (update + reconciliation) maps to the same
        payment through the unique provider_id.
        """
        timestamp = now()
        async with self.transaction() as conn:
            cur = await conn.execute(
                "SELECT payment_id FROM payments WHERE provider_id = ?",
                (provider_id,),
            )
            existing = await cur.fetchone()
            if existing is not None:
                return str(existing[0])
            cur = await conn.execute(
                "SELECT telegram_id, tier, amount FROM star_subscriptions "
                "WHERE subscription_id = ?",
                (subscription_id,),
            )
            sub = await cur.fetchone()
            if sub is None:
                raise RuntimeError("subscription is missing")
            await conn.execute(
                "INSERT INTO payments (payment_id, telegram_id, amount, currency, "
                "months, method, status, provider_id, tier, created_at, "
                "subscription_id) VALUES (?, ?, ?, 'XTR', 1, 'stars', 'pending', "
                "?, ?, ?, ?)",
                (
                    payment_id,
                    sub[0],
                    sub[2],
                    provider_id,
                    sub[1],
                    timestamp,
                    subscription_id,
                ),
            )
            charge_id = provider_id.split(":", 1)[-1]
            await conn.execute(
                "UPDATE star_subscriptions SET status = 'active', "
                "first_charge_id = COALESCE(first_charge_id, ?), "
                "last_charge_id = ?, "
                "next_charge_at = MAX(COALESCE(next_charge_at, 0), ?), "
                "updated_at = ? WHERE subscription_id = ?",
                (charge_id, charge_id, next_charge_at, timestamp, subscription_id),
            )
        return payment_id

    async def set_star_subscription_status(
        self, subscription_id: str, status: str
    ) -> None:
        await self._execute(
            "UPDATE star_subscriptions SET status = ?, updated_at = ? "
            "WHERE subscription_id = ?",
            (status, now(), subscription_id),
        )

    async def end_lapsed_star_subscriptions(self, before: int) -> int:
        """Active subscriptions whose renewal never arrived are no longer active."""
        return await self._execute(
            "UPDATE star_subscriptions SET status = 'ended', updated_at = ? "
            "WHERE status IN ('active', 'failed') AND next_charge_at IS NOT NULL "
            "AND next_charge_at < ?",
            (now(), before),
        )

    async def create_telegram_rub_order(
        self,
        *,
        payment_id: str,
        telegram_id: int,
        amount: int,
        months: int,
        tier: str,
    ) -> None:
        await self._execute(
            "INSERT INTO payments (payment_id, telegram_id, amount, currency, "
            "months, method, status, tier, created_at, channel) "
            "VALUES (?, ?, ?, 'RUB', ?, 'sbp', 'pending', ?, ?, 'telegram')",
            (payment_id, telegram_id, amount, months, tier, now()),
        )

    async def open_telegram_rub_order(
        self, telegram_id: int, months: int, tier: str, since: int
    ) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM payments WHERE telegram_id = ? AND months = ? AND tier = ? "
            "AND method = 'sbp' AND channel = 'telegram' AND status = 'pending' "
            "AND provider_id IS NULL AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT 1",
            (telegram_id, months, tier, since),
        )

    async def bind_telegram_rub_charge(self, payment_id: str, provider_id: str) -> bool:
        """Attach YooKassa's payment number to a Telegram ruble order, once.

        As with Stars, money already taken reopens an order cancelled as stale.
        """
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE payments SET provider_id = ?, status = CASE "
                "WHEN status = 'cancelled' THEN 'pending' ELSE status END "
                "WHERE payment_id = ? AND method = 'sbp' AND channel = 'telegram' "
                "AND provider_id IS NULL",
                (provider_id, payment_id),
            )
            cur = await conn.execute(
                "SELECT provider_id FROM payments WHERE payment_id = ?",
                (payment_id,),
            )
            row = await cur.fetchone()
        return row is not None and row[0] == provider_id

    async def mark_payment_refunded(self, payment_id: str) -> bool:
        """paid -> refunded, once. Refunded orders never count as paid again."""
        rowcount = await self._execute(
            "UPDATE payments SET status = 'refunded' "
            "WHERE payment_id = ? AND status = 'paid'",
            (payment_id,),
        )
        return rowcount == 1

    async def get_meta(self, key: str) -> str | None:
        row = await self._fetchone("SELECT value FROM app_meta WHERE key = ?", (key,))
        return str(row["value"]) if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._execute(
            "INSERT INTO app_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def claim_traffic_notice(
        self, node_username: str, window_start: int, level: int
    ) -> bool:
        """True the first time a quota level is reached in a traffic window."""
        rowcount = await self._execute(
            "INSERT OR IGNORE INTO traffic_notices "
            "(node_username, window_start, level, created_at) VALUES (?, ?, ?, ?)",
            (node_username, window_start, level, now()),
        )
        return rowcount == 1

    async def release_traffic_notice(
        self, node_username: str, window_start: int, level: int
    ) -> None:
        """Undo a claim whose message was not delivered, so it is retried."""
        await self._execute(
            "DELETE FROM traffic_notices WHERE node_username = ? "
            "AND window_start = ? AND level = ?",
            (node_username, window_start, level),
        )

    async def purge_traffic_notices(self, older_than: int) -> int:
        return await self._execute(
            "DELETE FROM traffic_notices WHERE created_at < ?",
            (older_than,),
        )

    async def get_payment_activation(self, payment_id: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM payment_activations WHERE payment_id = ?",
            (payment_id,),
        )

    async def prepare_payment_activation(
        self, payment_id: str, target_expires_at: int | None
    ) -> dict[str, Any]:
        timestamp = now()
        async with self.transaction() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO payment_activations "
                "(payment_id, target_expires_at, status, created_at) "
                "VALUES (?, ?, 'pending', ?)",
                (payment_id, target_expires_at, timestamp),
            )
            if target_expires_at is not None:
                await conn.execute(
                    "UPDATE payment_activations SET target_expires_at = "
                    "COALESCE(target_expires_at, ?) WHERE payment_id = ?",
                    (target_expires_at, payment_id),
                )
            cur = await conn.execute(
                "SELECT * FROM payment_activations WHERE payment_id = ?",
                (payment_id,),
            )
            row = await cur.fetchone()
        if row is None:
            raise RuntimeError("payment activation job was not created")
        return dict(row)

    async def complete_payment_activation(self, payment_id: str) -> None:
        await self._execute(
            "UPDATE payment_activations SET status = 'completed', "
            "completed_at = ?, last_error = NULL WHERE payment_id = ?",
            (now(), payment_id),
        )

    async def record_payment_activation_error(
        self, payment_id: str, error: str
    ) -> None:
        await self._execute(
            "UPDATE payment_activations SET attempts = attempts + 1, "
            "last_attempt_at = ?, last_error = ? WHERE payment_id = ?",
            (now(), error[:500], payment_id),
        )

    async def payments_pending_activation(self) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT p.payment_id FROM payments p "
            "LEFT JOIN payment_activations a ON a.payment_id = p.payment_id "
            "WHERE p.status = 'paid' "
            "AND (a.payment_id IS NULL OR a.status = 'pending') "
            "ORDER BY p.paid_at, p.created_at"
        )

    async def count_paid_payments(self, telegram_id: int) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM payments "
            "WHERE telegram_id = ? AND status = 'paid'",
            (telegram_id,),
        )
        return int(row["n"]) if row else 0

    async def payment_history(
        self, telegram_id: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT payment_id, amount, currency, months, method, status, tier, "
            "created_at, paid_at FROM payments WHERE telegram_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (telegram_id, limit),
        )

    async def count_pending_payments(self, telegram_id: int) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM payments "
            "WHERE telegram_id = ? AND status = 'pending'",
            (telegram_id,),
        )
        return int(row["n"]) if row else 0

    # A YooKassa payment still being created is never cancelled here: the
    # provider call may finish later and the reconciler would issue it.
    async def open_payment(self, telegram_id: int) -> dict[str, Any] | None:
        """The customer's newest unpaid order with its payment page, if any."""
        return await self._fetchone(
            "SELECT p.*, c.confirmation_url, c.state FROM payments p "
            "LEFT JOIN yookassa_creations c ON c.payment_id = p.payment_id "
            "WHERE p.telegram_id = ? AND p.status = 'pending' "
            "ORDER BY p.created_at DESC, p.payment_id DESC LIMIT 1",
            (telegram_id,),
        )

    async def cancel_own_payment(self, telegram_id: int, payment_id: str) -> bool:
        """The customer drops an unpaid order. A late payment is still credited."""
        rowcount = await self._execute(
            "UPDATE payments SET status = 'cancelled' WHERE payment_id = ? "
            "AND telegram_id = ? AND status = 'pending' AND "
            "NOT EXISTS (SELECT 1 FROM yookassa_creations c "
            "WHERE c.payment_id = payments.payment_id AND c.state != 'ready')",
            (payment_id, telegram_id),
        )
        return rowcount == 1

    async def cancel_all_unpaid(self) -> int:
        """Owner command: cancel every unpaid order. Late payments still count."""
        return await self._execute(
            "UPDATE payments SET status = 'cancelled' WHERE status = 'pending' AND "
            "NOT EXISTS (SELECT 1 FROM yookassa_creations c "
            "WHERE c.payment_id = payments.payment_id AND c.state != 'ready')",
            (),
        )

    async def cancel_stale_payments(self, older_than: int) -> int:
        """Cancel unpaid orders that never reached a payment provider.

        Provider-bound orders are not cancelled by a timer: the reconciler asks
        the provider first, because a timer alone never proves nobody paid.
        """
        return await self._execute(
            "UPDATE payments SET status = 'cancelled' WHERE status = 'pending' "
            "AND created_at < ? AND provider_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM yookassa_creations c "
            "WHERE c.payment_id = payments.payment_id AND c.state != 'ready')",
            (older_than,),
        )

    async def payments_to_check(self, at: int, limit: int = 25) -> list[dict[str, Any]]:
        """Open orders, plus recently cancelled ones a late payment can still hit."""
        return await self._fetchall(
            "SELECT p.* FROM payments p LEFT JOIN payment_checks c "
            "ON c.payment_id = p.payment_id WHERE (p.status = 'pending' "
            "OR (p.status = 'cancelled' AND p.paid_at IS NULL AND p.created_at >= ?)) "
            "AND (p.provider_id LIKE 'yookassa:%' OR "
            "p.provider_id LIKE 'cryptopay:%') "
            "AND (c.next_check_at IS NULL OR c.next_check_at <= ?) "
            "ORDER BY COALESCE(c.next_check_at, 0), p.created_at, p.payment_id LIMIT ?",
            (at - LATE_PAYMENT_WINDOW_SECONDS, at, limit),
        )

    async def schedule_payment_check(self, payment_id: str, at: int) -> None:
        await self._execute(
            "INSERT INTO payment_checks (payment_id, next_check_at) VALUES (?, ?) "
            "ON CONFLICT(payment_id) DO UPDATE "
            "SET next_check_at = excluded.next_check_at",
            (payment_id, at),
        )

    async def cancel_rejected_creation(self, payment_id: str) -> None:
        """YooKassa refused the order: void it so the customer can choose again."""
        await self._execute(
            "UPDATE payments SET status = 'cancelled' WHERE payment_id = ? "
            "AND status = 'pending' AND provider_id IS NULL",
            (payment_id,),
        )

    async def cancel_confirmed_payment(self, payment_id: str) -> None:
        await self._execute(
            "UPDATE payments SET status = 'cancelled' "
            "WHERE payment_id = ? AND status = 'pending'",
            (payment_id,),
        )

    # ---------------------------------------------------------- pending bans
    async def get_pending_ban(self, telegram_id: int) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM pending_bans WHERE telegram_id = ?",
            (telegram_id,),
        )

    async def queue_pending_ban(
        self,
        telegram_id: int,
        node_username: str | None,
        reason: str,
        detail: str,
    ) -> None:
        await self._execute(
            "INSERT OR IGNORE INTO pending_bans "
            "(telegram_id, node_username, reason, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (telegram_id, node_username, reason, detail, now()),
        )

    async def pending_bans(self) -> list[dict[str, Any]]:
        return await self._fetchall("SELECT * FROM pending_bans ORDER BY created_at")

    async def record_pending_ban_error(self, telegram_id: int, error: str) -> None:
        await self._execute(
            "UPDATE pending_bans SET attempts = attempts + 1, "
            "last_error = ?, last_attempt_at = ? WHERE telegram_id = ?",
            (error[:500], now(), telegram_id),
        )

    async def delete_pending_ban(self, telegram_id: int) -> None:
        await self._execute(
            "DELETE FROM pending_bans WHERE telegram_id = ?",
            (telegram_id,),
        )

    # ------------------------------------------------- anomalies / audit / logs
    async def add_anomaly(
        self,
        telegram_id: int | None,
        node_username: str | None,
        kind: str,
        detail: str,
    ) -> None:
        if not self.activity_logging_enabled and kind != "AUTOBAN":
            return
        await self._execute(
            "INSERT INTO anomalies "
            "(telegram_id, node_username, kind, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (telegram_id, node_username, kind, detail, now()),
        )

    async def recent_anomalies(
        self, telegram_id: int, kind: str, since: int
    ) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT * FROM anomalies WHERE telegram_id = ? AND kind = ? "
            "AND created_at >= ? ORDER BY id",
            (telegram_id, kind, since),
        )

    async def list_anomalies(self, telegram_id: int) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT * FROM anomalies WHERE telegram_id = ? ORDER BY id DESC",
            (telegram_id,),
        )

    async def add_audit(
        self,
        telegram_id: int | None,
        action: str,
        target: str | None,
        detail: str | None,
    ) -> None:
        if not self.activity_logging_enabled and action != "autoban":
            return
        await self._execute(
            "INSERT INTO audit_log (telegram_id, action, target, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (telegram_id, action, target, detail, now()),
        )

    async def add_conn_log(
        self,
        telegram_id: int,
        node_username: str,
        connected_at: int,
        bytes_up: int,
        bytes_down: int,
    ) -> None:
        if not self.activity_logging_enabled:
            return
        await self._execute(
            "INSERT INTO conn_logs (telegram_id, node_username, connected_at, "
            "bytes_up, bytes_down) VALUES (?, ?, ?, ?, ?)",
            (telegram_id, node_username, connected_at, bytes_up, bytes_down),
        )

    async def get_open_conn_log(self, node_username: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM conn_logs WHERE node_username = ? "
            "AND disconnected_at IS NULL ORDER BY id DESC LIMIT 1",
            (node_username,),
        )

    async def add_conn_bytes(self, log_id: int, bytes_down: int) -> None:
        await self._execute(
            "UPDATE conn_logs SET bytes_down = bytes_down + ? WHERE id = ?",
            (bytes_down, log_id),
        )

    async def close_open_conn_logs(self, node_username: str, at: int) -> None:
        await self._execute(
            "UPDATE conn_logs SET disconnected_at = ? "
            "WHERE node_username = ? AND disconnected_at IS NULL",
            (at, node_username),
        )

    async def purge_conn_logs(self, older_than: int) -> int:
        return await self._execute(
            "DELETE FROM conn_logs WHERE connected_at < ?",
            (older_than,),
        )

    async def get_traffic_day(
        self, node_username: str, day: str
    ) -> dict[str, Any] | None:
        if not self.activity_logging_enabled:
            row = self._traffic_memory.get((node_username, day))
            return dict(row) if row is not None else None
        return await self._fetchone(
            "SELECT * FROM traffic_daily WHERE node_username = ? AND day = ?",
            (node_username, day),
        )

    async def upsert_traffic_day(
        self,
        node_username: str,
        day: str,
        *,
        baseline_bytes: int,
        last_bytes: int,
        last_online_at: str | None,
    ) -> None:
        if not self.activity_logging_enabled:
            for key in list(self._traffic_memory):
                if key[1] < day:
                    del self._traffic_memory[key]
            key = (node_username, day)
            previous = self._traffic_memory.get(key, {})
            self._traffic_memory[key] = {
                "baseline_bytes": baseline_bytes,
                "last_bytes": last_bytes,
                "last_online_at": None,
                "soft_flagged": previous.get("soft_flagged", 0),
            }
            self._traffic_memory.move_to_end(key)
            while len(self._traffic_memory) > 10000:
                self._traffic_memory.popitem(last=False)
            return
        await self._execute(
            "INSERT INTO traffic_daily (node_username, day, baseline_bytes, "
            "last_bytes, last_online_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(node_username, day) DO UPDATE SET "
            "last_bytes = excluded.last_bytes, "
            "last_online_at = excluded.last_online_at",
            (node_username, day, baseline_bytes, last_bytes, last_online_at),
        )

    async def set_traffic_soft_flag(self, node_username: str, day: str) -> None:
        if not self.activity_logging_enabled:
            row = self._traffic_memory.get((node_username, day))
            if row is not None:
                row["soft_flagged"] = 1
            return
        await self._execute(
            "UPDATE traffic_daily SET soft_flagged = 1 "
            "WHERE node_username = ? AND day = ?",
            (node_username, day),
        )

    async def purge_retained_records(self, now_ts: int) -> int:
        """Retention from the privacy policy (legal/privacy.md, section 7)."""
        year = 365 * 86400
        removed = await self._execute(
            "DELETE FROM support_messages WHERE created_at < ?", (now_ts - year,)
        )
        # Incidents and the admin audit trail: the 3-year limitation period.
        # AUTOBAN evidence stays: scripts/compliance_archiver.py archives it and
        # only then removes the row.
        removed += await self._execute(
            "DELETE FROM anomalies WHERE created_at < ? AND kind != 'AUTOBAN'",
            (now_ts - 3 * year,),
        )
        removed += await self._execute(
            "DELETE FROM audit_log WHERE created_at < ? AND action != 'autoban'",
            (now_ts - 3 * year,),
        )
        return removed

    async def purge_traffic_days(self, before_day: str) -> int:
        return await self._execute(
            "DELETE FROM traffic_daily WHERE day < ?",
            (before_day,),
        )

    # ------------------------------------------------------------------- push
    async def upsert_push_subscription(
        self,
        telegram_id: int,
        endpoint: str,
        p256dh: str,
        auth: str,
    ) -> None:
        await self._execute(
            "INSERT INTO push_subscriptions "
            "(telegram_id, endpoint, p256dh, auth, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET "
            "telegram_id = excluded.telegram_id, p256dh = excluded.p256dh, "
            "auth = excluded.auth",
            (telegram_id, endpoint, p256dh, auth, now()),
        )

    async def delete_push_subscription(
        self, telegram_id: int, endpoint: str | None
    ) -> int:
        if endpoint is None:
            return await self._execute(
                "DELETE FROM push_subscriptions WHERE telegram_id = ?",
                (telegram_id,),
            )
        return await self._execute(
            "DELETE FROM push_subscriptions WHERE telegram_id = ? AND endpoint = ?",
            (telegram_id, endpoint),
        )

    async def delete_push_by_endpoint(self, endpoint: str) -> None:
        await self._execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ?",
            (endpoint,),
        )

    async def list_push_subscriptions(self, telegram_id: int) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions "
            "WHERE telegram_id = ?",
            (telegram_id,),
        )

    # --------------------------------------------------------- refresh tokens
    async def add_refresh_token(
        self, jti: str, telegram_id: int, expires_at: int
    ) -> None:
        await self._execute(
            "INSERT INTO refresh_tokens "
            "(jti, telegram_id, expires_at, revoked, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (jti, telegram_id, expires_at, now()),
        )

    async def get_refresh_token(self, jti: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM refresh_tokens WHERE jti = ?",
            (jti,),
        )

    async def revoke_refresh_token(self, jti: str) -> bool:
        rowcount = await self._execute(
            "UPDATE refresh_tokens SET revoked = 1 WHERE jti = ? AND revoked = 0",
            (jti,),
        )
        return rowcount == 1

    async def revoke_all_refresh_tokens(self, telegram_id: int) -> int:
        return await self._execute(
            "UPDATE refresh_tokens SET revoked = 1 "
            "WHERE telegram_id = ? AND revoked = 0",
            (telegram_id,),
        )

    async def purge_refresh_tokens(self, at: int) -> int:
        return await self._execute(
            "DELETE FROM refresh_tokens WHERE expires_at < ?",
            (at,),
        )

    # ---------------------------------------------------------------- support
    async def add_support_message(
        self, admin_message_id: int, telegram_id: int
    ) -> None:
        await self._execute(
            "INSERT OR REPLACE INTO support_messages "
            "(admin_message_id, telegram_id, created_at) VALUES (?, ?, ?)",
            (admin_message_id, telegram_id, now()),
        )

    async def get_support_target(self, admin_message_id: int) -> int | None:
        row = await self._fetchone(
            "SELECT telegram_id FROM support_messages WHERE admin_message_id = ?",
            (admin_message_id,),
        )
        return int(row["telegram_id"]) if row else None
