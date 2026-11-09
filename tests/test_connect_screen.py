"""The connect screen (PWA and Mini App): device-aware picks, official links only.

The deep links that hand a subscription to a VPN app (happ://, v2raytun://,
hiddify://) are built in one place per app, public/connect.js, which first checks
the link is this service's own subscription (tests/test_connect.py). Each screen
and its device module must go through it and must never point an install button
at anything but the developers' own pages.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from keyboards.main_menu import CONNECT_APPS

ROOT = Path(__file__).resolve().parents[1]
APPS = ["pwa", "tma"]

# A wrong install link in a security product could send a buyer to a look-alike,
# so the set of hosts is closed and checked: happ.su routes to the stores itself,
# Hiddify ships on GitHub, v2rayTUN on the two app stores.
OFFICIAL_INSTALL_HOSTS = {"happ.su", "apps.apple.com", "play.google.com", "github.com"}


def _device(app: str) -> str:
    return (ROOT / app / "src" / "device.ts").read_text(encoding="utf-8")


def _page(app: str) -> str:
    path = ROOT / app / "src" / "pages" / "ConnectPage.tsx"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("app", APPS)
def test_install_links_point_only_to_official_pages(app: str) -> None:
    urls = re.findall(r'"(https?://[^"]+)"', _device(app))
    assert urls, f"{app}/src/device.ts carries no install links"
    for url in urls:
        assert url.startswith("https://"), url
        host = re.sub(r"^https://([^/]+).*$", r"\1", url)
        assert host in OFFICIAL_INSTALL_HOSTS, url


@pytest.mark.parametrize("app", APPS)
def test_device_module_covers_exactly_the_wired_apps(app: str) -> None:
    apps = set(re.findall(r"^  (\w+): \{$", _device(app), re.M))
    assert apps == {a for a, _ in CONNECT_APPS}


@pytest.mark.parametrize("app", APPS)
def test_only_the_checked_bouncer_builds_scheme_links(app: str) -> None:
    for scheme in ("happ://", "v2raytun://", "hiddify://"):
        assert scheme not in _device(app)
        assert scheme not in _page(app)
    # The screen hands links to the bouncer through connectUrl, nowhere else.
    assert "connectUrl(" in _page(app)
