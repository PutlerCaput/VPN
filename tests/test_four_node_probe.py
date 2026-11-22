"""The probe routes only through SOCKS and reports failures without endpoint data."""

from __future__ import annotations

import subprocess  # nosec B404
from unittest.mock import Mock

import pytest

from deploy.four_node import probe

URL = "https://health.example.org/egress"
ADDR = "192.0.2.100"


@pytest.mark.parametrize(
    ("reply", "ok"),
    [
        (b"192.0.2.100\n\n200", True),
        (b"192.0.2.101\n\n200", False),
        (b"192.0.2.100\n\n302", False),
        (b"invalid\n200", False),
        (b"", False),
    ],
)
def test_probe_uses_explicit_socks_and_checks_egress(
    monkeypatch: pytest.MonkeyPatch, reply: bytes, ok: bool
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=reply))
    monkeypatch.setattr(probe.subprocess, "run", run)
    assert probe.check_egress(URL, 1087, ADDR) is ok
    command = run.call_args.args[0]
    assert command[:2] == ["curl", "--disable"]
    assert command[command.index("--noproxy") + 1] == ""
    assert command[command.index("--proxy") + 1] == "socks5h://127.0.0.1:1087"
    assert "--location" not in command
    assert run.call_args.kwargs["check"] is True


def test_probe_fails_when_proxy_does_not_respond(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(side_effect=subprocess.TimeoutExpired("curl", 12))
    monkeypatch.setattr(probe.subprocess, "run", run)
    assert not probe.check_egress(URL, 1087, ADDR)


@pytest.mark.parametrize(
    "url",
    [
        "http://health.example.org",
        "https://user@health.example.org",
        "https://health.example.org/?secret=yes",
    ],
)
def test_probe_rejects_invalid_endpoint(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    run = Mock()
    monkeypatch.setattr(probe.subprocess, "run", run)
    with pytest.raises(ValueError, match="egress endpoint must be an HTTPS URL"):
        probe.check_egress(url, 1087, ADDR)
    run.assert_not_called()
