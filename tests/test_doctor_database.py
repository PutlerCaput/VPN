"""The doctor's view of the database and of the connection records switch.

The switch that matters is the one next to the data (on the gateway when the
database is on the other server), and it must agree with the policy the bot
publishes: people are told before anything about them is recorded.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp.test_utils import TestServer, unused_port
from pydantic import SecretStr

from db.database import RETENTION_YEARS, Database
from db.gateway import create_gateway_app
from tests.conftest import GATEWAY_TOKEN

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "doctor_bot", ROOT / "scripts" / "doctor_bot.py"
)
assert spec is not None
assert spec.loader is not None
doctor: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)

OLD_POLICY = (
    "3.2. Оператор **не обрабатывает**: содержимое трафика Пользователя; адреса "
    "посещённых ресурсов; журнал подключений; данные банковских карт."
)


def settings(**values: Any) -> SimpleNamespace:
    base = {
        "database_url": None,
        "db_gateway_token": None,
        "db_key": SecretStr("ab" * 32),
        "activity_logging_enabled": False,
        "conn_log_retention_days": 1826,
    }
    return SimpleNamespace(**{**base, **values})


def test_the_published_policy_describes_connection_records() -> None:
    assert doctor.policy_keeps_connection_records() is True
    assert doctor.policy_keeps_connection_records(OLD_POLICY) is False
    # Naming the records while still listing them as "not processed" is not it.
    assert (
        doctor.policy_keeps_connection_records(OLD_POLICY + " Учёт подключений")
        is False
    )


def test_the_check_works_piped_into_the_container(tmp_path: Path) -> None:
    """update_bot.sh and db_cutover.sh run it as `python3 - < doctor_bot.py`."""

    def piped(policy: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        with (ROOT / "scripts" / "doctor_bot.py").open() as script:
            return subprocess.run(  # noqa: S603
                [sys.executable, "-", *arguments],
                stdin=script,
                env={**os.environ, "DOCTOR_PRIVACY_PATH": str(policy)},
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

    flag = "--policy-keeps-connection-records"
    assert piped(ROOT / "legal" / "privacy.md", flag).returncode == 0
    old = tmp_path / "privacy.md"
    old.write_text(OLD_POLICY)
    assert piped(old, flag).returncode == 1
    assert piped(tmp_path / "missing.md", flag).returncode == 1
    # Loading the file this way must not need a path of its own (no __file__ here).
    assert "NameError" not in piped(ROOT / "legal" / "privacy.md", flag).stderr


def test_policy_and_code_name_the_same_term() -> None:
    policy = (ROOT / "legal" / "privacy.md").read_text(encoding="utf-8")
    term = f"{RETENTION_YEARS} лет"
    retention = policy.split("## 7. Сроки хранения")[1].split("## 8.")[0]
    for row in (
        "Учётная запись",
        "Связь обращения с Пользователем",
        "Технические признаки инцидентов",
        "Сведения о подключениях",
    ):
        line = next(text for text in retention.splitlines() if row in text)
        assert term in line, row
    assert RETENTION_YEARS * 365 == doctor.RETENTION_DAYS
    # The maintenance report and the settings count from the same term.
    from config import Settings
    from db import maintenance

    assert maintenance.RETENTION_YEARS == RETENTION_YEARS
    assert (
        maintenance.INCIDENT_DAYS == maintenance.SUPPORT_DAYS == RETENTION_YEARS * 365
    )
    assert Settings.model_fields["conn_log_retention_days"].default == (
        maintenance.CONN_LOG_DAYS
    )
    # What is collected is said where a person accepts the offer, too.
    offer = (ROOT / "legal" / "oferta.md").read_text(encoding="utf-8")
    assert "не ведёт журнал посещённых ресурсов и подключений" not in offer
    assert "сведения о подключениях" in offer
    for screen in ("pwa/src/pages/LegalPage.tsx", "tma/src/i18n/ru.ts"):
        assert "сведения о подключениях (время и объём) хранятся 5 лет" in (
            ROOT / screen
        ).read_text(encoding="utf-8")
    # Still never recorded, and still said so.
    assert "адреса посещённых ресурсов и DNS-запросы" in policy
    assert "содержимое трафика" in policy


@pytest.mark.parametrize(
    ("switch", "policy", "level", "words"),
    [
        (True, True, "OK", "kept, as the policy says"),
        (False, False, "OK", "off, as the policy says"),
        (True, False, "FAIL", "published policy says they are not"),
        (False, True, "WARN", "the switch is off"),
        (None, True, "WARN", "could not be read"),
    ],
)
def test_the_switch_must_agree_with_the_policy(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    switch: bool | None,
    policy: bool,
    level: str,
    words: str,
) -> None:
    monkeypatch.setattr(doctor, "policy_keeps_connection_records", lambda: policy)
    doctor.check_connection_records(settings(), switch)
    out = capsys.readouterr().out
    assert out.startswith(f"{level} ")
    assert words in out


def test_the_fix_named_depends_on_where_the_data_is(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(doctor, "policy_keeps_connection_records", lambda: True)
    doctor.check_connection_records(settings(), False)
    assert "--set ACTIVITY_LOGGING_ENABLED=true" in capsys.readouterr().out
    doctor.check_connection_records(
        settings(database_url="http://10.204.0.1:8444"), False
    )
    assert "db_cutover.sh connection-log on" in capsys.readouterr().out


def test_a_short_retention_setting_is_flagged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    doctor.check_connection_records(settings(conn_log_retention_days=180), True)
    out = capsys.readouterr().out
    assert "WARN CONN_LOG_RETENTION_DAYS=180" in out
    assert "--set CONN_LOG_RETENTION_DAYS=1826" in out
    doctor.check_connection_records(settings(), True)
    assert "CONN_LOG_RETENTION_DAYS" not in capsys.readouterr().out


async def test_a_local_database_uses_the_bots_own_setting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert await doctor.check_database(settings()) is False
    assert "OK database: local file, encrypted" in capsys.readouterr().out
    assert await doctor.check_database(settings(activity_logging_enabled=True)) is True
    await doctor.check_database(settings(db_key=None))
    assert "WARN database: local file, NOT encrypted" in capsys.readouterr().out


@pytest.mark.parametrize("recording", [False, True])
async def test_a_remote_database_reports_the_gateways_switch(
    capsys: pytest.CaptureFixture[str], recording: bool
) -> None:
    database = Database(":memory:", "ab" * 32, activity_logging_enabled=recording)
    await database.connect()
    server = TestServer(create_gateway_app(database, GATEWAY_TOKEN))
    await server.start_server()
    try:
        # The bot's own setting says the opposite: it is not the one in force.
        seen = await doctor.check_database(
            settings(
                database_url=str(server.make_url("/")).rstrip("/"),
                db_gateway_token=SecretStr(GATEWAY_TOKEN),
                activity_logging_enabled=not recording,
            )
        )
    finally:
        await server.close()
        await database.close()
    assert seen is recording
    out = capsys.readouterr().out
    assert "OK database: on the other server through the gateway, encrypted" in out
    assert GATEWAY_TOKEN not in out


async def test_an_unreachable_or_unwilling_gateway_is_a_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    down = settings(
        database_url=f"http://127.0.0.1:{unused_port()}",
        db_gateway_token=SecretStr(GATEWAY_TOKEN),
    )
    assert await doctor.check_database(down) is None
    assert "FAIL database gateway does not answer" in capsys.readouterr().out

    database = Database(":memory:")
    await database.connect()
    server = TestServer(create_gateway_app(database, GATEWAY_TOKEN))
    await server.start_server()
    try:
        wrong = settings(
            database_url=str(server.make_url("/")).rstrip("/"),
            db_gateway_token=SecretStr("wrong-token-" + "0" * 30),
        )
        assert await doctor.check_database(wrong) is None
    finally:
        await server.close()
        await database.close()
    out = capsys.readouterr().out
    assert "FAIL database gateway refused the bot" in out
    assert "wrong-token" not in out


async def test_an_unfinished_token_rotation_is_flagged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gateway accepts two tokens only while a token change is open."""
    import db.remote as remote_module

    class Client:
        encrypted = True
        clock_skew_seconds = 0
        activity_logging_enabled = False

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.tokens_accepted = accepted[0]

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

    accepted = [1]
    monkeypatch.setattr(remote_module, "RemoteDatabase", Client)
    configured = settings(
        database_url="http://10.204.0.1:8444", db_gateway_token=SecretStr("t" * 40)
    )
    await doctor.check_database(configured)
    assert "two tokens" not in capsys.readouterr().out

    accepted[0] = 2
    await doctor.check_database(configured)
    out = capsys.readouterr().out
    assert "WARN the gateway still accepts two tokens" in out
    assert "--finish-token-rotation" in out
