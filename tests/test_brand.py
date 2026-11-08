"""Brand: icons in the sizes the manifests promise, fonts with their licence,
and the logo before the bot's first greeting."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import CommandObject
from aiogram.types import Chat, Message, User
from PIL import Image

import handlers.start as start
from handlers.start import cmd_start

ROOT = Path(__file__).resolve().parents[1]
APPS = (("pwa", "/"), ("tma", "/tma/"))


@pytest.mark.parametrize(("app", "prefix"), APPS)
def test_manifest_icons_exist_in_their_declared_sizes(app: str, prefix: str) -> None:
    public = ROOT / app / "public"
    manifest = json.loads((public / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["short_name"] == "Cyberdefency"
    for icon in manifest["icons"]:
        width, height = (int(value) for value in icon["sizes"].split("x"))
        with Image.open(public / icon["src"].removeprefix(prefix)) as image:
            assert image.size == (width, height)


@pytest.mark.parametrize(("app", "prefix"), APPS)
def test_page_template_references_files_that_exist(app: str, prefix: str) -> None:
    public = ROOT / app / "public"
    page = (ROOT / app / "index.html").read_text(encoding="utf-8")
    refs = re.findall(
        r'(?:href|content)="(?:__PUBLIC_ORIGIN__)?(/[^"]+\.(?:png|jpg|webp|woff2))"',
        page,
    )
    assert refs
    for ref in refs:
        assert (public / ref.removeprefix(prefix)).is_file(), ref


def test_link_preview_image_gets_the_sites_absolute_address() -> None:
    # Open Graph needs an absolute og:image. The build puts the bot's
    # PUBLIC_BASE_URL in place of the marker; update_web.sh passes it.
    page = (ROOT / "pwa" / "index.html").read_text(encoding="utf-8")
    assert 'property="og:image" content="__PUBLIC_ORIGIN__/brand/og-image.jpg"' in page
    vite = (ROOT / "pwa" / "vite.config.ts").read_text(encoding="utf-8")
    assert "/__PUBLIC_ORIGIN__/g" in vite
    assert "ORIGIN_RE.test(requested)" in vite
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pwa_stage = docker.split("AS pwa-builder", 1)[1].split("FROM ", 1)[0]
    assert "ARG PUBLIC_ORIGIN=https://app.cyberdefency.com" in pwa_stage
    update = (ROOT / "scripts" / "update_web.sh").read_text(encoding="utf-8")
    assert 'container_env "$BOT_CONTAINER" PUBLIC_BASE_URL' in update
    assert '--build-arg "PUBLIC_ORIGIN=$site"' in update


@pytest.mark.parametrize(("app", "prefix"), APPS)
def test_fonts_ship_with_their_licence(app: str, prefix: str) -> None:
    fonts = ROOT / app / "public" / "fonts"
    assert "SIL Open Font License" in (fonts / "OFL.txt").read_text(encoding="utf-8")
    for subset in ("cyrillic", "latin", "latin-ext"):
        assert (fonts / f"manrope-{subset}.woff2").read_bytes()[:4] == b"wOF2"


def _message(owner: int) -> Message:
    return Message(
        message_id=10,
        date=datetime.now(UTC),
        chat=Chat(id=owner, type="private"),
        from_user=User(id=owner, is_bot=False, first_name="Owner"),
    )


async def test_new_user_sees_the_logo_before_the_offer(
    services: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(start, "_welcome_file", {})
    order: list[str] = []
    uploaded = SimpleNamespace(
        photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")]
    )
    first = _message(995001)
    object.__setattr__(
        first,
        "answer_photo",
        AsyncMock(side_effect=lambda *_a, **_k: order.append("photo") or uploaded),
    )
    object.__setattr__(
        first, "answer", AsyncMock(side_effect=lambda *_a, **_k: order.append("offer"))
    )

    await cmd_start(first, CommandObject(command="start"), services)

    assert order == ["photo", "offer"]
    assert start._welcome_file == {"file_id": "large"}

    second = _message(995002)
    for name in ("answer", "answer_photo"):
        object.__setattr__(second, name, AsyncMock())
    await cmd_start(second, CommandObject(command="start"), services)
    # Uploaded once; later greetings send Telegram's copy.
    assert second.answer_photo.call_args.args[0] == "large"


async def test_logo_failure_does_not_block_the_offer(
    services: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(start, "_welcome_file", {})
    message = _message(995003)
    object.__setattr__(
        message,
        "answer_photo",
        AsyncMock(side_effect=TelegramNetworkError(method=MagicMock(), message="down")),
    )
    object.__setattr__(message, "answer", AsyncMock())

    await cmd_start(message, CommandObject(command="start"), services)

    message.answer.assert_awaited_once()


async def test_returning_user_gets_no_logo(services: Any) -> None:
    await services.db.create_user(995004, "owner", None)
    await services.provisioning.accept_offer_and_start_trial(995004)
    message = _message(995004)
    for name in ("answer", "answer_photo"):
        object.__setattr__(message, name, AsyncMock())

    await cmd_start(message, CommandObject(command="start"), services)

    message.answer_photo.assert_not_awaited()
