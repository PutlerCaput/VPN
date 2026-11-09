"""The PWA connect screen: device-aware app picks, official install links only.

The deep links that hand a subscription to a VPN app (happ://, v2raytun://,
hiddify://) are built in one place, pwa/public/connect.js, which first checks the
link is this service's own subscription (tests/test_connect.py). The screen and
the device module must go through it and must never point an install button at
anything but the developers' own pages.
"""

from __future__ import annotations

import re
from pathlib import Path

from keyboards.main_menu import CONNECT_APPS

ROOT = Path(__file__).resolve().parents[1]
DEVICE = (ROOT / "pwa" / "src" / "device.ts").read_text(encoding="utf-8")
PAGE = (ROOT / "pwa" / "src" / "pages" / "ConnectPage.tsx").read_text(encoding="utf-8")

# A wrong install link in a security product could send a buyer to a look-alike,
# so the set of hosts is closed and checked: happ.su routes to the stores itself,
# Hiddify ships on GitHub, v2rayTUN on the two app stores.
OFFICIAL_INSTALL_HOSTS = {"happ.su", "apps.apple.com", "play.google.com", "github.com"}


def test_install_links_point_only_to_official_pages() -> None:
    urls = re.findall(r'"(https?://[^"]+)"', DEVICE)
    assert urls, "device.ts carries no install links"
    for url in urls:
        assert url.startswith("https://"), url
        host = re.sub(r"^https://([^/]+).*$", r"\1", url)
        assert host in OFFICIAL_INSTALL_HOSTS, url


def test_device_module_covers_exactly_the_wired_apps() -> None:
    apps = set(re.findall(r"^  (\w+): \{$", DEVICE, re.M))
    assert apps == {app for app, _ in CONNECT_APPS}


def test_only_the_checked_bouncer_builds_scheme_links() -> None:
    for scheme in ("happ://", "v2raytun://", "hiddify://"):
        assert scheme not in DEVICE
        assert scheme not in PAGE
    # The screen hands links to the bouncer through connectUrl, nowhere else.
    assert "connectUrl(" in PAGE
