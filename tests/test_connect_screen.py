"""The connect screen (PWA and Mini App): the same apps as the bot, official links only.

Each screen offers the apps the owner chose for the visitor's device, copies the
link when an app is chosen and shows a short list of steps. The install links
are the developers' own pages; they are checked here against a closed list of
hosts and against the bot's (services/connection_guide.py), so that a typo in one
of the three places cannot send a buyer of a security product to a look-alike.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.connection_guide import (
    DIRECT_CHECK,
    DIRECT_INTRO,
    GUIDES,
    KARING_URL,
    NEKOBOX_URL,
    ONEXRAY_URL,
    V2RAYN_URL,
    V2RAYNG_URL,
)

ROOT = Path(__file__).resolve().parents[1]
APPS = ["pwa", "tma"]

# A wrong install link in a security product could send a buyer to a look-alike,
# so the set of hosts is closed and checked: the apps ship on GitHub and in the
# app stores.
OFFICIAL_INSTALL_HOSTS = {"apps.apple.com", "play.google.com", "github.com"}
OFFICIAL_INSTALL_URLS = {
    V2RAYNG_URL,
    NEKOBOX_URL,
    V2RAYN_URL,
    KARING_URL,
    ONEXRAY_URL,
}
# Apps the owner once chose and that were taken out, with the reason. A removed
# app must not come back by accident; bringing one back means checking it again
# (docs/CONNECT_APPS.md).
REMOVED_APPS = {
    "nekoray": "its repository was archived in March 2025",
    "foxray": "gone from the App Store; a search finds an unrelated look-alike",
    "v2box": "removed from the Russian App Store in March 2026",
    "hiddify": "not in the Russian App Store",
    "streisand": "removed from the Russian App Store in March 2026",
    "happ": "removed from the Russian App Store in March 2026",
    "v2raytun": "removed from the Russian App Store in March 2026",
}

# "key", "name" and "install", in this order, are what both device modules share.
ENTRY = re.compile(r'key: "(\w+)",\s*name: "([^"]+)",\s*install: "([^"]+)"', re.DOTALL)


def _device(app: str) -> str:
    return (ROOT / app / "src" / "device.ts").read_text(encoding="utf-8")


def _page(app: str) -> str:
    path = ROOT / app / "src" / "pages" / "ConnectPage.tsx"
    return path.read_text(encoding="utf-8")


def _apps_by_platform(app: str) -> dict[str, list[tuple[str, str, str]]]:
    """{platform: [(key, name, install)]} read from the APPS table of device.ts."""
    block = _device(app).split("export const APPS", 1)[1]
    block = block.split("\n};", 1)[0]
    parts = re.split(r"^  (android|ios|windows|macos|linux): \[", block, flags=re.M)
    # re.split keeps the platform names: [before, name, body, name, body, ...]
    return {parts[i]: ENTRY.findall(parts[i + 1]) for i in range(1, len(parts) - 1, 2)}


@pytest.mark.parametrize("app", APPS)
def test_install_links_point_only_to_official_pages(app: str) -> None:
    urls = re.findall(r'"(https?://[^"]+)"', _device(app))
    assert urls, f"{app}/src/device.ts carries no install links"
    for url in urls:
        assert url.startswith("https://"), url
        host = re.sub(r"^https://([^/]+).*$", r"\1", url)
        assert host in OFFICIAL_INSTALL_HOSTS, url
    assert set(urls) == OFFICIAL_INSTALL_URLS


@pytest.mark.parametrize("app", APPS)
def test_the_apps_for_each_device_are_the_bots(app: str) -> None:
    found = _apps_by_platform(app)
    expected = {
        device: [(a.key, a.name, a.download_url) for a in guide.apps]
        for device, guide in GUIDES.items()
    }
    assert found == expected


def test_the_apps_offered_for_each_device() -> None:
    offered = {
        "android": ["v2rayNG", "NekoBox"],
        "ios": ["Karing", "OneXray"],
        "windows": ["v2rayN"],
        "macos": ["OneXray", "v2rayN"],
        "linux": ["v2rayN"],
    }
    assert {d: [a.name for a in g.apps] for d, g in GUIDES.items()} == offered


@pytest.mark.parametrize("app", ["bot", *APPS])
def test_removed_apps_do_not_come_back(app: str) -> None:
    if app == "bot":
        keys = {a.key for g in GUIDES.values() for a in g.apps}
        names = {a.name.lower() for g in GUIDES.values() for a in g.apps}
    else:
        entries = [e for found in _apps_by_platform(app).values() for e in found]
        keys = {key for key, _, _ in entries}
        names = {name.lower() for _, name, _ in entries}
    assert not keys & set(REMOVED_APPS)
    assert not names & set(REMOVED_APPS)


def test_app_store_links_name_the_app_by_its_id() -> None:
    # A search by name finds look-alikes: the link must be the app's own page.
    for url in OFFICIAL_INSTALL_URLS:
        if "apps.apple.com" in url:
            assert re.fullmatch(r"https://apps\.apple\.com/app/id\d+", url), url


@pytest.mark.parametrize("app", APPS)
def test_linux_is_detected_after_android_and_never_for_chromebooks(app: str) -> None:
    device = _device(app)
    # An Android user agent also says "Linux": Android must be asked first.
    assert device.index('"android"; //') < device.index("Linux|X11") or (
        device.index("/Android/i") < device.index("/Linux|X11/i")
    )
    assert "CrOS" in device


def test_the_platforms_are_in_the_same_order_everywhere() -> None:
    for app in APPS:
        order = re.search(r"PLATFORMS: Platform\[\] = \[([^\]]+)\]", _device(app))
        assert order
        assert re.findall(r'"(\w+)"', order[1]) == list(GUIDES)


def test_the_mini_app_has_steps_for_every_app_it_offers() -> None:
    strings = (ROOT / "tma" / "src" / "i18n" / "ru.ts").read_text(encoding="utf-8")
    block = strings.split("const guides", 1)[1].split("\n};\n", 1)[0]
    found: dict[str, list[str]] = {}
    platform = ""
    for line in block.splitlines():
        if match := re.match(r"^  (\w+): \{$", line):
            platform = match[1]
            found[platform] = []
        elif match := re.match(r"^    (\w+): \{$", line):
            found[platform].append(match[1])
    assert found == {d: [a.key for a in g.apps] for d, g in GUIDES.items()}


def test_the_website_keeps_the_steps_with_the_apps() -> None:
    device = _device("pwa")
    for guide in GUIDES.values():
        for app in guide.apps:
            # Each app brings its own steps; the first one installs that very app.
            assert f'key: "{app.key}"' in device
    assert device.count("steps: [") == sum(len(g.apps) for g in GUIDES.values())


@pytest.mark.parametrize("app", APPS)
def test_the_screen_asks_for_nothing_but_the_link_and_the_platform(app: str) -> None:
    page = _page(app)
    # The link is copied inside the tap that chooses the app.
    assert "navigator.clipboard.writeText(link)" in page
    assert "function choose(" in page
    assert "chosen.install" in page
    # No scheme link opens an app: none of these apps has one that is checked.
    for text in (page, _device(app)):
        assert not re.search(r"\b(?!https\b)[a-z][a-z0-9+.-]*://", text), text[:80]
    assert "connectUrl" not in page


def test_the_one_tap_bouncer_is_gone() -> None:
    for gone in (
        "pwa/public/connect.html",
        "pwa/public/connect.js",
        "pwa/src/connect.ts",
        "tma/src/connect.ts",
    ):
        assert not (ROOT / gone).exists(), gone
    for folder in ("pwa/src", "tma/src", "keyboards", "handlers"):
        for path in (ROOT / folder).rglob("*"):
            if path.suffix in {".ts", ".tsx", ".py"}:
                text = path.read_text(encoding="utf-8")
                assert "connect.html" not in text, path
                # (docs/CONNECT_APPS.md is the list of offered apps, not the old table)
                assert not re.search(r"\bCONNECT_APPS\b(?!\.md)", text), path
                assert "connect_app_kb" not in text, path


# ------------------------------------------------- Russian sites, directly
def _ts_array(text: str, name: str) -> list[str]:
    """The string items of `const NAME = [ ... ];` in a TypeScript module."""
    body = text.split(f"const {name} = [", 1)[1].split("\n];", 1)[0]
    return re.findall(r'^\s*"([^"]+)",$', body, flags=re.M)


def _ts_string(text: str, name: str) -> str:
    """A string constant or property split over lines: `NAME =\n  "..."`."""
    match = re.search(rf'{name}[:=]?\s*=?\s*\n?\s*"([^"]+)"', text)
    assert match, name
    return match[1]


def _bot_steps(direct: str) -> list[str]:
    return [re.sub(r"^\d+\. ", "", line) for line in direct.split("\n")]


def test_the_russian_sites_steps_are_the_same_in_all_three_places() -> None:
    pwa = _device("pwa")
    tma = (ROOT / "tma" / "src" / "i18n" / "ru.ts").read_text(encoding="utf-8")
    seen: set[str] = set()
    for guide in GUIDES.values():
        for app in guide.apps:
            if app.key in seen:
                continue
            seen.add(app.key)
            steps = _bot_steps(app.direct)
            assert _ts_array(pwa, f"{app.key.upper()}_DIRECT") == steps, app.key
            assert _ts_array(tma, f"{app.key}Direct") == steps, app.key
    assert _ts_string(pwa, "export const DIRECT_INTRO") == DIRECT_INTRO
    assert _ts_string(pwa, "export const DIRECT_CHECK") == DIRECT_CHECK
    assert _ts_string(tma, "directIntro:") == DIRECT_INTRO
    assert _ts_string(tma, "directCheck:") == DIRECT_CHECK


def test_every_app_on_the_screens_carries_its_russian_sites_steps() -> None:
    apps = sum(len(g.apps) for g in GUIDES.values())
    assert _device("pwa").count("      direct: ") == apps
    tma = (ROOT / "tma" / "src" / "i18n" / "ru.ts").read_text(encoding="utf-8")
    assert tma.count("      direct: ") == apps
    assert "chosen.direct.map" in _page("pwa")
    assert "guide.direct.map" in _page("tma")
    for app in APPS:
        # Folded away under its title: connecting comes first.
        assert "<details" in _page(app)
