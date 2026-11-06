"""The server report distinguishes live/test stores without exposing credentials."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

import config
from scripts.diagnose_payment_path import BOT_CONFIG

KEY = "test_private_diagnostic_value"
DEFAULT_SECRET = SecretStr(KEY)
SHOP = "987654321"


def run_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    account: dict[str, Any] | None = None,
    status: int = 200,
    error: Exception | None = None,
    secret: SecretStr | None = DEFAULT_SECRET,
) -> list[httpx.Request]:
    # Simulate the older live Settings schema without egress attributes.
    settings = SimpleNamespace(
        sbp_provider="manual",
        yookassa_shop_id=SHOP,
        yookassa_secret_key=secret,
        db_key=None,
    )
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if error:
            raise error
        return httpx.Response(status, json=account)

    client = httpx.AsyncClient

    def create(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs.pop("proxy") == "http://10.203.0.1:3128"
        return client(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", create)
    exec(compile(BOT_CONFIG, "<server-payment-diagnostic>", "exec"), {})  # noqa: S102
    return requests


@pytest.mark.parametrize("test", [True, False])
def test_reports_merchant_mode_and_methods_without_secret_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], test: bool
) -> None:
    requests = run_probe(
        monkeypatch,
        account={
            "account_id": SHOP,
            "test": test,
            "status": "enabled",
            "fiscalization_enabled": False,
            "payment_methods": ["bank_card"],
            "description": KEY,
        },
    )
    output = capsys.readouterr().out
    assert KEY not in output
    assert SHOP not in output
    report = json.loads(output)
    assert report["account_matches_configured_shop"]
    assert report["test_store"] is test
    assert report["bank_card_available"] is True
    assert report["sbp_available"] is False
    assert report["configured_egress"] == "unsupported"
    assert report["configured_ru_proxy"] is False
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.yookassa.ru/v3/me"
    assert requests[0].method == "GET"


def test_missing_rest_key_is_reported_without_attempting_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests = run_probe(monkeypatch, secret=None)
    assert not requests
    report = json.loads(capsys.readouterr().out)
    assert report["account_check"] == "REST_CREDENTIALS_MISSING"
    assert report["yookassa_secret_set"] is False


def test_account_failure_does_not_print_the_provider_response(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_probe(monkeypatch, status=401, account={"description": KEY})
    output = capsys.readouterr().out
    assert KEY not in output
    assert json.loads(output)["account_http"] == 401


def test_proxy_error_is_safe_and_does_not_fall_back_to_direct(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests = run_probe(monkeypatch, error=httpx.ProxyError(KEY))
    output = capsys.readouterr().out
    assert KEY not in output
    assert json.loads(output)["account_error_type"] == "ProxyError"
    assert len(requests) == 1
