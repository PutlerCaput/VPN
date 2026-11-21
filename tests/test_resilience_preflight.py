"""Regression tests for the secret-safe, read-only resilience preflight."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

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
    assert "BOT_NOT_RUNNING" in {item.code for item in result}
    assert "PORTS_NOT_CHECKED" in {item.code for item in result}


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


@pytest.mark.parametrize("running", [True, False])
@pytest.mark.parametrize("field", ["Env", "Cmd", "Entrypoint"])
def test_all_containers_are_checked_even_without_bot(running: bool, field: str) -> None:
    marker = "test-credential-" + "x" * 30
    container = _container("cloudflared", running=running)
    container["Config"][field] = (
        ["BOT_TOKEN=" + marker] if field == "Env" else ["tunnel", "--token", marker]
    )
    result = audit([container])
    expected = (
        "CREDENTIALS_IN_DOCKER_ENV"
        if field == "Env"
        else "CREDENTIALS_IN_DOCKER_COMMAND"
    )
    assert expected in {item.code for item in result}
    assert marker not in str(result)


def test_token_argument_can_cross_entrypoint_cmd_boundary() -> None:
    container = _container("cloudflared")
    container["Config"].update(Entrypoint=["tunnel", "--token"], Cmd=["test-value"])
    assert "CREDENTIALS_IN_DOCKER_COMMAND" in {f.code for f in audit([container])}


def test_compose_labels_and_explicit_name_select_renamed_bot() -> None:
    bot = _container("new-project-bot-2")
    bot["Config"]["Labels"] = {"com.docker.compose.service": "bot"}
    assert "BOT_NOT_RUNNING" not in {f.code for f in audit([bot])}
    bot["Config"].pop("Labels")
    assert "BOT_NOT_RUNNING" not in {
        f.code for f in audit([bot], bot_name="new-project-bot-2")
    }


def test_multiple_bots_are_ambiguous_not_silently_first() -> None:
    bots = [_container("first"), _container("second")]
    for bot in bots:
        bot["Config"]["Labels"] = {"com.docker.compose.service": "bot"}
    assert "BOT_IDENTITY_AMBIGUOUS" in {f.code for f in audit(bots)}


@pytest.mark.parametrize(
    ("host", "exposed"),
    [
        ("192.0.2.1", True),
        ("0.0.0.0", True),  # noqa: S104 - snapshot fixture, no bind
        ("*", True),
        ("[::]", True),
        ("[2001:db8::1]", True),
        ("127.0.0.1", False),
        ("[::1]", False),
        ("[::ffff:127.0.0.1]", False),
    ],
)
def test_listener_parser_handles_specific_addresses(host: str, exposed: bool) -> None:
    result = audit(
        [_container("node-rental-bot-1")], f"tcp LISTEN 0 4096 {host}:8080 0.0.0.0:*\n"
    )
    assert ("PUBLIC_BOT_PORT" in {f.code for f in result}) is exposed


def test_peer_port_does_not_count_as_local_awg_listener() -> None:
    bot = _container("node-rental-bot-1", env={"AWG_ENABLED": "true"})
    agent = _container("cyberdefency-awg", env={"AWG_PORT": "51820"})
    wrong = audit([bot, agent], "udp UNCONN 0 0 0.0.0.0:12345 192.0.2.1:51820\n")
    assert "AWG_UDP_UNVERIFIED" in {f.code for f in wrong}
    correct = audit([bot, agent], "udp UNCONN 0 0 [::]:51820 [::]:*\n")
    assert "AWG_UDP_UNVERIFIED" not in {f.code for f in correct}
    assert "AWG_TUNNEL_UNVERIFIED" in {f.code for f in correct}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Config", "test-secret"),
        ("State", []),
        ("Name", 42),
        ("Config", {"Env": "BOT_TOKEN=test-secret"}),
        ("Config", {"Cmd": [42]}),
        ("Config", {"Labels": []}),
        ("State", {"Running": "true"}),
    ],
)
def test_invalid_nested_input_is_redacted_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], field: str, value: Any
) -> None:
    container = _container("node-rental-bot-1")
    container[field] = value
    path = tmp_path / "inspect.json"
    path.write_text(json.dumps([container]))
    assert main(["--inspect", str(path), "--json"]) == 2
    output = capsys.readouterr()
    assert "test-secret" not in output.err
    assert "PREFLIGHT_INPUT_ERROR: ValueError" in output.err
    assert "Traceback" not in output.err
    assert output.out == ""


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("fail_high", [False, True])
def test_cli_redacts_all_output_and_preserves_exit_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], json_mode: bool, fail_high: bool
) -> None:
    marker = "test-sensitive-" + "z" * 40
    bot = _container("node-rental-bot-1", env={"BOT_TOKEN": marker})
    bot["Config"]["Cmd"] = ["tunnel", "--token=" + marker]
    path = tmp_path / "inspect.json"
    path.write_text(json.dumps([bot]))
    args = (
        ["--inspect", str(path)]
        + (["--json"] if json_mode else [])
        + (["--fail-high"] if fail_high else [])
    )
    assert main(args) == int(fail_high)
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    if json_mode:
        assert isinstance(json.loads(output.out), list)
    assert "PORTS_NOT_CHECKED" in output.out
