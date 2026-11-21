"""Exercise the released Android apps through their normal subscription screens."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import pytest
from aiohttp import web

from tests.interop.profiles import reality_links, xray
from tests.test_entry_sharding import (
    NODE,
    RU_A,
    RU_B,
    Upstream,
    _client,
    _sharded_settings,
    _user,
)

V2RAYNG = "com.v2ray.ang"
NEKOBOX = "moe.nb4a"
LINK_AGENT = "v2rayNG/1.9.0"
Node = dict[str, str]


def adb(*args: str) -> str:
    tool = shutil.which("adb")
    assert tool
    return subprocess.run(  # noqa: S603 - fixed emulator; no shell
        [tool, "-s", "emulator-5554", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout


def screen() -> list[Node]:
    path = "/data/local/tmp/client-interop.xml"
    if "dumped" not in adb("shell", "uiautomator", "dump", path):
        return []
    xml = adb("exec-out", "cat", path)
    return [n.attrib for n in ET.fromstring(xml).iter("node")]  # noqa: S314


def wait_ui(match: Callable[[list[Node]], bool], label: str) -> None:
    deadline = time.monotonic() + 35
    nodes: list[Node] = []
    while time.monotonic() < deadline:
        nodes = screen()
        if match(nodes):
            return
        time.sleep(0.3)
    labels = [n["text"] or n["content-desc"] for n in nodes]
    pytest.fail(f"Did not see {label}. Emulator screen: {list(filter(None, labels))}")


def tap(attribute: str, value: str) -> None:
    def find_button(nodes: list[Node]) -> bool:
        for node in nodes:
            if node.get(attribute, "").casefold() == value.casefold():
                x1, y1, x2, y2 = map(int, re.findall(r"\d+", node["bounds"]))
                adb("shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
                return True
        return False

    wait_ui(find_button, value)


def wait_count(app: str, count: int) -> None:
    def displayed(nodes: list[Node]) -> bool:
        if app == NEKOBOX:
            return any(
                n["text"].startswith(f"{count} Proxies | Updated on") for n in nodes
            )
        # V2rayNG displays an absent VLESS fragment as "none" in each server row.
        return sum("none" in n["text"].lower().splitlines() for n in nodes) == count

    wait_ui(displayed, f"{app} with {count} saved profiles")


def open_subscription(app: str, url: str) -> None:
    scheme = "sn://subscription" if app == NEKOBOX else "v2rayng://install-sub"
    uri = f"{scheme}?url={quote(url, safe='')}"
    adb(
        "shell", "am", "start", "-W", "-a", "android.intent.action.VIEW", "-d", uri, app
    )
    if app == NEKOBOX:
        tap("text", "Yes")  # Import the group.
        tap("text", "Yes")  # Allow this loopback HTTP refresh while disconnected.
        tap("text", "OK")  # NekoBox displays the list of added profiles.


def refresh(app: str) -> None:
    if app == NEKOBOX:
        tap("resource-id", f"{app}:id/group_update")
        tap("text", "Yes")
    else:
        tap("content-desc", "More options")
        tap("text", "Update subscription")


@pytest.mark.parametrize("app", [V2RAYNG, NEKOBOX])
async def test_installed_app_refresh(services: Any, app: str) -> None:
    emulator = await asyncio.to_thread(adb, "shell", "getprop", "ro.kernel.qemu")
    assert emulator.strip() == "1"
    services.settings = _sharded_settings()
    await _user(services.db, 4213)
    await services.db.set_entry_segment(4213, "ru-a", "admin")
    upstream = Upstream(
        {
            "*": json.dumps(xray()),
            LINK_AGENT: base64.b64encode(reality_links().encode()).decode(),
        }
    )
    services.hiddify.fetch_subscription = upstream
    statuses: list[int] = []

    async def record(request: web.Request, response: web.StreamResponse) -> None:
        if request.path == f"/sub/{NODE}/":
            statuses.append(response.status)

    client = await _client(services)
    client.server.app.on_response_prepare.append(record)
    async with client:
        url = client.make_url(f"/sub/{NODE}/")
        port = f"tcp:{url.port}"
        await asyncio.to_thread(adb, "reverse", port, port)
        try:
            await asyncio.to_thread(open_subscription, app, str(url))
            await asyncio.to_thread(wait_count, app, 2)
            assert 200 in statuses
            default_agent = "NekoBox/Android/" if app == NEKOBOX else "v2rayNG/"
            assert upstream.agents[0].startswith(default_agent)
            assert LINK_AGENT in upstream.agents
            # A final, successful one-route refresh proves the displayed count
            # tracks this subscription and can change after the failed requests.
            for payload, status, count in (
                (json.dumps(xray()), 502, 2),
                (reality_links((RU_B,)), 503, 2),
                (reality_links((RU_A,)), 200, 1),
            ):
                statuses.clear()
                upstream.bodies[LINK_AGENT] = payload
                await asyncio.to_thread(refresh, app)
                async with asyncio.timeout(45):
                    while status not in statuses:
                        await asyncio.sleep(0.2)
                if app == NEKOBOX and status == 200:
                    await asyncio.to_thread(tap, "text", "OK")
                await asyncio.to_thread(wait_count, app, count)
        finally:
            await asyncio.to_thread(adb, "reverse", "--remove", port)
