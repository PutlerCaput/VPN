"""One-tap connect links: only this service's subscription links, only known apps."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from keyboards.main_menu import CONNECT_APPS, connect_app_kb

ROOT = Path(__file__).resolve().parents[1]
SITE = "https://app.test"
SUB = "https://sub.test:8443"
LINK = f"{SUB}/sub/abc123/"


def _urls(markup: object) -> list[str]:
    rows = markup.inline_keyboard  # type: ignore[attr-defined]
    return [row[0].url for row in rows]


def test_each_app_gets_a_button_to_the_connect_page() -> None:
    markup = connect_app_kb(SITE, SUB, LINK)
    assert markup is not None
    urls = _urls(markup)
    assert len(urls) == len(CONNECT_APPS) == 3
    for (app, _), url in zip(CONNECT_APPS, urls, strict=True):
        assert url == f"{SITE}/connect.html#app={app}&sub={quote(LINK, safe='')}"


def test_no_buttons_for_other_hosts_or_plain_links() -> None:
    assert connect_app_kb(SITE, SUB, "https://evil.test/sub/abc/") is None
    assert connect_app_kb(SITE, SUB, "vless://uuid@host:443") is None
    assert connect_app_kb("http://app.test", SUB, LINK) is None


def test_connect_page_has_no_inline_script() -> None:
    # The site's CSP allows same-origin scripts only: an inline one never runs.
    page = (ROOT / "pwa" / "public" / "connect.html").read_text(encoding="utf-8")
    assert re.search(r"<script(?![^>]*\ssrc=)[^>]*>", page) is None
    assert '<script src="/connect.js" defer></script>' in page


def test_page_and_bot_agree_on_the_subscription_host_and_apps() -> None:
    page = (ROOT / "pwa" / "public" / "connect.js").read_text(encoding="utf-8")
    origin = re.search(r'SUB_ORIGIN = "([^"]+)"', page)
    assert origin is not None
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    sub_base = re.search(r"^SUBSCRIPTION_BASE_URL=(\S+)$", env, re.M)
    assert sub_base is not None
    assert origin.group(1) == sub_base.group(1)
    apps = set(re.findall(r'^\s+(\w+): "[a-z0-9]+://', page, re.M))
    assert apps == {app for app, _ in CONNECT_APPS}
