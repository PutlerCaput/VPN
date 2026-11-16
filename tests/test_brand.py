"""Brand: icons in the sizes the manifests promise, fonts with their licence,
the logo before the bot's first greeting, and the brand animation."""

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

import services.brand_animation as brand
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
    message = Message(
        message_id=10,
        date=datetime.now(UTC),
        chat=Chat(id=owner, type="private"),
        from_user=User(id=owner, is_bot=False, first_name="Owner"),
    )
    for name in ("answer", "answer_photo", "answer_animation"):
        object.__setattr__(message, name, AsyncMock())
    return message


async def test_new_user_sees_the_animation_with_the_offer(
    services: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(brand, "_file_ids", {})
    uploaded = SimpleNamespace(animation=SimpleNamespace(file_id="anim-1"))
    first = _message(995001)
    first.answer_animation.return_value = uploaded

    await cmd_start(first, CommandObject(command="start"), services)

    first.answer_animation.assert_awaited_once()
    caption = first.answer_animation.await_args.kwargs["caption"]
    assert "Добро пожаловать" in caption
    assert "24 часа бесплатно" in caption
    first.answer.assert_not_awaited()
    assert brand.cached_animation_file_id() == "anim-1"

    second = _message(995002)
    second.answer_animation.return_value = SimpleNamespace(
        animation=SimpleNamespace(file_id="ignored")
    )
    await cmd_start(second, CommandObject(command="start"), services)
    # Uploaded once; later greetings send Telegram's copy.
    assert second.answer_animation.await_args.args[0] == "anim-1"


async def test_animation_failure_does_not_block_the_offer(
    services: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(brand, "_file_ids", {})
    message = _message(995003)
    message.answer_animation.side_effect = TelegramNetworkError(
        method=MagicMock(), message="down"
    )
    message.answer_photo.side_effect = TelegramNetworkError(
        method=MagicMock(), message="down"
    )

    await cmd_start(message, CommandObject(command="start"), services)

    message.answer.assert_awaited_once()
    assert "24 часа бесплатно" in message.answer.await_args.args[0]


async def test_returning_user_sees_the_animation_on_start(services: Any) -> None:
    await services.db.create_user(995004, "owner", None)
    await services.provisioning.accept_offer_and_start_trial(995004)
    message = _message(995004)
    message.answer_animation.return_value = SimpleNamespace(
        animation=SimpleNamespace(file_id="anim-return")
    )

    await cmd_start(message, CommandObject(command="start"), services)

    message.answer_animation.assert_awaited_once()
    caption = message.answer_animation.await_args.kwargs["caption"]
    assert "Рады снова видеть" in caption
    # The Mini App hint stays a plain follow-up, not a second animation.
    assert message.answer.await_count == 1
    assert "приложении" in message.answer.await_args.args[0]


def test_pictures_have_the_sizes_botfather_and_the_page_declare() -> None:
    wanted = {
        "branding/telegram/avatar-640.jpg": (640, 640),
        "branding/telegram/description-640x360.jpg": (640, 360),
        "branding/telegram/welcome-1280x720.jpg": (1280, 720),
        "pwa/public/brand/og-image.jpg": (1200, 630),
    }
    for name, size in wanted.items():
        with Image.open(ROOT / name) as image:
            assert (image.format, image.size) == ("JPEG", size), name
    assert (ROOT / "branding/telegram/brand-animation.mp4").is_file()
    assert (ROOT / "branding/telegram/brand-animation.gif").is_file()
    page = (ROOT / "pwa" / "index.html").read_text(encoding="utf-8")
    assert 'og:image:width" content="1200"' in page
    assert 'og:image:height" content="630"' in page


def test_bot_image_copies_the_animation_into_the_container() -> None:
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "branding/telegram/brand-animation.mp4" in docker


def test_the_banner_is_drawn_the_same_every_time() -> None:
    from scripts.build_brand_assets import banner

    first, second = banner(320, 180), banner(320, 180)
    assert first.size == (320, 180)
    assert first.tobytes() == second.tobytes()
    # Light behind the logo, a darker blue towards the corners.
    assert sum(first.getpixel((40, 90))) > sum(first.getpixel((2, 2)))
