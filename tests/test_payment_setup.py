"""Merchant preflight must not publish secrets or modify the running service."""

from __future__ import annotations

import base64
import getpass
import json
import stat
import subprocess
import warnings
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from scripts import prepare_yookassa as setup

SHOP_ID = "987654321"
KEY = "live_sensitive_test_value"
ACCOUNT = {
    "account_id": SHOP_ID,
    "test": False,
    "status": "enabled",
    "fiscalization_enabled": True,
    "payment_methods": ["bank_card", "sbp"],
}


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"account_id": "1"}, "STORE_ID_MISMATCH"),
        ({"test": True}, "LIVE_STORE_REQUIRED"),
        ({"test": "false"}, "LIVE_STORE_REQUIRED"),
        ({"status": "disabled"}, "STORE_NOT_ENABLED"),
        ({"fiscalization_enabled": False}, "FISCALIZATION_NOT_ENABLED"),
        ({"fiscalization_enabled": None}, "FISCALIZATION_NOT_ENABLED"),
    ],
)
def test_rejects_wrong_or_inactive_shop(change: dict[str, Any], error: str) -> None:
    with pytest.raises(setup.CheckFailed, match=error):
        setup.validate_account({**ACCOUNT, **change}, SHOP_ID)


def test_verifies_get_only_and_stages_secrets_privately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    existing = {
        "shop_id": SHOP_ID,
        "secret_key": KEY,
        "public_base_url": "https://merchant.example.org",
        "receipt_mode": "self_employed",
        "provider": "manual",
    }
    docker = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(existing)))
    monkeypatch.setattr(setup.subprocess, "run", docker)
    prompt = Mock(side_effect=AssertionError("existing credentials must be reused"))
    monkeypatch.setattr(setup, "hidden_input", prompt)
    response = Mock(status=200)
    response.read.return_value = json.dumps(ACCOUNT).encode()
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = context
    monkeypatch.setattr(setup.urllib.request, "build_opener", lambda *_: opener)

    setup.run(tmp_path)

    request = opener.open.call_args.args[0]
    assert request.full_url == "https://api.yookassa.ru/v3/me"
    assert request.get_method() == "GET"
    assert request.data is None
    expected = base64.b64encode(f"{SHOP_ID}:{KEY}".encode()).decode()
    assert request.get_header("Authorization") == "Basic " + expected
    docker.assert_called_once()
    assert docker.call_args.args[0][:4] == [
        "docker",
        "exec",
        "node-rental-bot-1",
        "python",
    ]
    output = capsys.readouterr().out
    assert "YOOKASSA_CREDENTIALS_VERIFIED" in output
    assert KEY not in output
    assert SHOP_ID not in output
    assert expected not in output
    (directory,) = tmp_path.iterdir()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert (directory / "secret_key.txt").read_text().strip() == KEY
    for file in directory.iterdir():
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
    report = json.loads((directory / "verification.json").read_text())
    assert report["bank_card_available"]
    assert report["sbp_available"]
    assert report["fiscalization_enabled"]
    assert report["provider"] == "manual"


def test_private_write_never_overwrites_existing_key(tmp_path: Path) -> None:
    setup.write_private(tmp_path, "key", KEY)
    with pytest.raises(FileExistsError):
        setup.write_private(tmp_path, "key", "replacement")
    assert (tmp_path / "key").read_text() == KEY


def test_rejects_redirect_instead_of_forwarding_credentials() -> None:
    with pytest.raises(setup.CheckFailed, match="REDIRECT_REJECTED"):
        setup.NoRedirect().redirect_request(None, None, 302, "", None, "https://other/")


def test_hidden_prompt_refuses_echo_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def fallback(_: str) -> str:
        warnings.warn("Cannot control echo", getpass.GetPassWarning, stacklevel=2)
        raise AssertionError("must stop before an unhidden prompt")

    monkeypatch.setattr(setup.getpass, "getpass", fallback)
    with pytest.raises(getpass.GetPassWarning):
        setup.hidden_input("Key: ")


@pytest.mark.parametrize(
    "url",
    [
        "http://merchant.example.org",
        "https://user:password@merchant.example.org",
        "https://node.example.com",
        "https://merchant.example.org?token=private",
        "https://merchant.example.org:wrong",
    ],
)
def test_unsafe_or_placeholder_webhook_not_reported(url: str) -> None:
    assert setup.candidate_webhook(url) is None


def test_bad_credentials_fail_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = Mock(side_effect=AssertionError("network must not be attempted"))
    monkeypatch.setattr(setup.urllib.request, "build_opener", opener)
    with pytest.raises(setup.CheckFailed, match="INVALID_SHOP_ID_FORMAT"):
        setup.verify_credentials("user:password", KEY)
    with pytest.raises(setup.CheckFailed, match="INVALID_KEY_FORMAT"):
        setup.verify_credentials(SHOP_ID, "key\ninjected-header")
    opener.assert_not_called()
