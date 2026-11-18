"""Regression tests for the secret-safe, read-only resilience preflight."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.resilience_preflight import audit, main


def _container(
    name: str, *, running: bool = True, env: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "Name": f"/{name}",
        "State": {"Running": running},
        "Config": {"Env": [f"{key}={value}" for key, value in (env or {}).items()]},
    }


def test_missing_bot_is_detected() -> None:
    result = audit([_container("web")])
    assert [item.code for item in result] == ["BOT_NOT_RUNNING"]


def test_report_never_exposes_secret_values() -> None:
    private_value = "UNSAFE_VALUE_MUST_NEVER_APPEAR_IN_REPORT"
    result = audit(
        [
            _container(
                "node-rental-bot-1",
                env={
                    "BOT_TOKEN": private_value,
                    "YOOKASSA_SECRET_KEY": private_value,
                    "SMTP_PASSWORD": private_value,
                },
            )
        ]
    )
    finding = next(item for item in result if item.code == "CREDENTIALS_IN_DOCKER_ENV")
    assert "3 credential variable(s)" in finding.message
    assert private_value not in json.dumps([item.message for item in result])


def test_enabled_awg_without_local_agent_is_not_claimed_healthy() -> None:
    result = audit(
        [_container("node-rental-bot-1", env={"AWG_ENABLED": "true"})],
        "udp UNCONN 0 0 0.0.0.0:36099 0.0.0.0:*\n",
    )
    codes = {item.code for item in result}
    assert "AWG_AGENT_UNVERIFIED" in codes
    assert "AWG_UDP_UNVERIFIED" in codes


def test_awg_present_and_port_open_does_not_trigger_local_warnings() -> None:
    result = audit(
        [
            _container(
                "node-rental-bot-1",
                env={
                    "AWG_ENABLED": "true",
                    "ENTRY_SHARDING_ENABLED": "true",
                    "CONFIG_DELIVERY": "subscription",
                },
            ),
            _container("cyberdefency-awg"),
        ],
        "udp UNCONN 0 0 0.0.0.0:39051 0.0.0.0:*\n",
    )
    codes = {item.code for item in result}
    assert "AWG_AGENT_UNVERIFIED" not in codes
    assert "AWG_UDP_UNVERIFIED" not in codes
    assert "ENTRY_SHARDING_OFF" not in codes
    assert "SINGLE_LINK_DELIVERY" not in codes


def test_public_bot_listener_is_detected() -> None:
    result = audit(
        [_container("node-rental-bot-1")],
        "tcp LISTEN 0 4096 0.0.0.0:8080 0.0.0.0:*\n",
    )
    assert "PUBLIC_BOT_PORT" in {item.code for item in result}


def test_invalid_input_reports_only_exception_class(
    tmp_path: Path, capsys: Any
) -> None:
    path = tmp_path / "inspect.json"
    path.write_text("PRIVATE_CREDENTIAL_NOT_VALID_JSON")
    assert main(["--inspect", str(path)]) == 2
    output = capsys.readouterr()
    assert "PREFLIGHT_INPUT_ERROR: JSONDecodeError" in output.err
    assert "PRIVATE_CREDENTIAL" not in output.err
