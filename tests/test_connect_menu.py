"""The bot's connect menu: device, then app, then the link and a few steps.

Choosing an app sends the subscription link (tap it, or the copy button, to
copy), the install button and the short steps for exactly that app. The link goes
only to its owner in a private chat, only with active access."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, User

from handlers.onboarding import (
    SETUP_TEXT,
    app_blocked_text,
    app_guide_text,
    setup_action,
    start_setup,
)
from keyboards.onboarding import (
    COPY_LIMIT,
    client_apps_kb,
    client_guide_kb,
    guide_kb,
    setup_kb,
)
from services.connection_guide import (
    GUIDES,
    NEKOBOX,
    SINGLE_LINK_STEPS,
    find_app,
)

OWNER = 123456
SUBSCRIPTION = "https://sub.test:8443/sub/abc123token"


def message_for(owner: int, *, chat_type: str = "private") -> Message:
    message = Message(
        message_id=10,
        date=datetime.now(UTC),
        chat=Chat(id=owner, type=chat_type),
        from_user=User(id=999, is_bot=True, first_name="Bot"),
    )
    for name in ("answer", "answer_photo"):
        object.__setattr__(message, name, AsyncMock())
    return message


def callback_for(data: str, message: Message, user_id: int = OWNER) -> CallbackQuery:
    callback = CallbackQuery(
        id="callback",
        chat_instance="instance",
        data=data,
        from_user=User(id=user_id, is_bot=False, first_name="Owner"),
        message=message,
    )
    object.__setattr__(callback, "answer", AsyncMock())
    return callback


def buttons(markup: InlineKeyboardMarkup | None) -> list[Any]:
    assert markup is not None
    return [button for row in markup.inline_keyboard for button in row]


async def active_owner(services: Any, link: str | None = SUBSCRIPTION) -> None:
    await services.db.create_user(OWNER, "owner", None)
    await services.provisioning.accept_offer_and_start_trial(OWNER)
    if link is not None:
        services.provisioning.fresh_config = AsyncMock(return_value=link)


# ---------------------------------------------------------------- the data
def test_each_device_offers_the_apps_that_passed_the_check() -> None:
    assert {d: [a.name for a in g.apps] for d, g in GUIDES.items()} == {
        "android": ["v2rayNG", "NekoBox"],
        "ios": ["Karing", "OneXray"],
        "windows": ["v2rayN"],
        "macos": ["OneXray", "v2rayN"],
        "linux": ["v2rayN"],
    }
    assert {g.title for g in GUIDES.values()} == {
        "Android",
        "iPhone / iPad",
        "Windows",
        "macOS",
        "Linux",
    }


def test_every_app_has_short_numbered_steps_that_start_with_its_install() -> None:
    for guide in GUIDES.values():
        for app in guide.apps:
            lines = app.steps.split("\n")
            assert 4 <= len(lines) <= 5, app.name
            for number, line in enumerate(lines, 1):
                assert line.startswith(f"{number}. "), (app.name, line)
            assert app.name in lines[0] or app.name.lower() in lines[0].lower()
            assert "Скопируйте ссылку" in lines[1]
            assert len(app.steps) < 900, app.name
            # Text goes out as HTML: nothing in it may look like a tag.
            assert not re.search(r"[<>&]", app.steps + app.note), app.name


def test_install_buttons_go_to_the_developers_own_pages() -> None:
    hosts = {"github.com", "apps.apple.com"}
    for guide in GUIDES.values():
        for app in guide.apps:
            assert app.download_url.startswith("https://")
            host = app.download_url.split("/")[2]
            assert host in hosts, app.name


def test_the_unofficial_google_play_build_of_nekobox_is_called_out() -> None:
    assert NEKOBOX.download_url.startswith("https://github.com/")
    assert "Google Play" in NEKOBOX.note
    assert "play.google.com" not in NEKOBOX.download_url


def test_find_app_rejects_stale_buttons() -> None:
    found = find_app("android", "nekobox")
    assert found is not None
    assert found[1] is NEKOBOX
    assert find_app("linux", "v2rayn") is not None
    assert find_app("android", "karing") is None  # an iPhone app on Android
    # Apps that were taken out: a button from an old message does nothing.
    for device, app in (
        ("ios", "foxray"),
        ("ios", "v2box"),
        ("macos", "v2box"),
        ("windows", "nekoray"),
        ("linux", "nekoray"),
    ):
        assert find_app(device, app) is None, (device, app)
    assert find_app("chromeos", "v2rayn") is None
    assert find_app("android", "") is None


# ----------------------------------------------------------- the keyboards
def test_the_device_menu_lists_five_devices_and_the_help_buttons() -> None:
    data = [b.callback_data for b in buttons(setup_kb())]
    assert data == [
        "setup:guide:android",
        "setup:guide:ios",
        "setup:guide:windows",
        "setup:guide:macos",
        "setup:guide:linux",
        "setup:killswitch",
        "setup:help",
    ]
    assert "setup:guide:amnezia" in [b.callback_data for b in buttons(setup_kb(True))]
    assert "Android" in SETUP_TEXT or "устройство" in SETUP_TEXT


def test_the_app_menu_has_one_button_per_app() -> None:
    for key, guide in GUIDES.items():
        data = [b.callback_data for b in buttons(client_apps_kb(key))]
        assert data[: len(guide.apps)] == [
            f"setup:app:{key}:{a.key}" for a in guide.apps
        ]
        assert data[-2:] == ["setup:choose", "setup:help"]
        assert all(len(d or "") <= 64 for d in data)
    # guide_kb keeps working for a device key too
    assert next(b.callback_data for b in buttons(guide_kb("ios"))) == (
        "setup:app:ios:karing"
    )


def test_the_guide_keyboard_copies_installs_and_offers_the_other_app() -> None:
    kb = buttons(client_guide_kb("android", NEKOBOX, SUBSCRIPTION))
    copy = kb[0]
    assert copy.copy_text is not None
    assert copy.copy_text.text == SUBSCRIPTION  # tapping copies the link itself
    install = next(b for b in kb if b.url)
    assert install.text == "Установить NekoBox"
    assert install.url == NEKOBOX.download_url
    callbacks = [b.callback_data for b in kb]
    assert "setup:config" in callbacks  # the QR code and the reset
    assert "setup:guide:android" in callbacks  # the other app
    assert "setup:choose" in callbacks
    # One app only on this device: nothing to switch to.
    single = find_app("windows", "v2rayn")
    assert single is not None
    only = [
        b.callback_data
        for b in buttons(client_guide_kb("windows", single[1], SUBSCRIPTION))
    ]
    assert "setup:guide:windows" not in only


def test_a_link_too_long_to_copy_gets_no_copy_button() -> None:
    long_link = "vless://" + "x" * COPY_LIMIT
    kb = buttons(client_guide_kb("android", NEKOBOX, long_link))
    assert all(b.copy_text is None for b in kb)
    assert any(b.url for b in kb)  # the install button stays


def test_without_a_link_there_is_only_the_install_button() -> None:
    kb = buttons(client_guide_kb("android", NEKOBOX, None))
    assert all(b.copy_text is None for b in kb)
    assert "setup:config" not in [b.callback_data for b in kb]
    assert any(b.url == NEKOBOX.download_url for b in kb)


# ------------------------------------------------------------- the messages
def test_the_guide_text_shows_the_link_then_the_steps_for_that_app() -> None:
    device = GUIDES["android"]
    text = app_guide_text(device, NEKOBOX, SUBSCRIPTION)
    assert text.startswith("📱 <b>Android · NekoBox</b>")
    assert f"<code>{SUBSCRIPTION}</code>" in text
    assert text.index(SUBSCRIPTION) < text.index("1. Установите NekoBox")
    assert NEKOBOX.note in text
    assert "никому не передавайте" in text


def test_a_single_server_link_gets_the_clipboard_steps_instead() -> None:
    link = "vless://id@host:443?security=reality&type=tcp#<name>"
    text = app_guide_text(GUIDES["android"], NEKOBOX, link)
    assert "&amp;type=tcp#&lt;name&gt;" in text  # HTML-safe
    assert SINGLE_LINK_STEPS in text
    assert "Add Subscription" not in text
    assert "Группы" not in text


def test_the_blocked_text_has_no_link_and_says_why() -> None:
    text = app_blocked_text(GUIDES["ios"], GUIDES["ios"].apps[0], "Нужна оплата.")
    assert "Нужна оплата." in text
    assert "https://" not in text
    assert "как только доступ будет активен" in text


# ------------------------------------------------------------- the handlers
async def test_choosing_a_device_with_two_apps_asks_which_app(services: Any) -> None:
    await active_owner(services)
    message = message_for(OWNER)
    callback = callback_for("setup:guide:android", message)
    await setup_action(callback, AsyncMock(), services)
    text = message.answer.call_args.args[0]
    markup = message.answer.call_args.kwargs["reply_markup"]
    assert "<b>Android</b>" in text
    assert SUBSCRIPTION not in text  # the link comes with the app
    assert [b.callback_data for b in buttons(markup)][:2] == [
        "setup:app:android:v2rayng",
        "setup:app:android:nekobox",
    ]
    services.provisioning.fresh_config.assert_not_awaited()


async def test_choosing_an_app_sends_the_link_the_steps_and_the_buttons(
    services: Any,
) -> None:
    await active_owner(services)
    message = message_for(OWNER)
    callback = callback_for("setup:app:ios:karing", message)
    state = AsyncMock()
    await setup_action(callback, state, services)
    text = message.answer.call_args.args[0]
    markup = message.answer.call_args.kwargs["reply_markup"]
    assert "iPhone / iPad · Karing" in text
    assert f"<code>{SUBSCRIPTION}</code>" in text
    assert "«Добавить профиль»" in text
    assert "OneXray" not in text  # nothing of the other apps
    assert "App Store" in text  # the note: Apple removes such apps from the store
    kb = buttons(markup)
    assert kb[0].copy_text.text == SUBSCRIPTION
    assert any(b.url == "https://apps.apple.com/app/id6472431552" for b in kb)
    services.provisioning.fresh_config.assert_awaited_once_with(OWNER)
    state.clear.assert_awaited()
    callback.answer.assert_awaited_once()


@pytest.mark.parametrize(
    ("device", "title", "expected"),
    [
        ("windows", "Windows · v2rayN", "v2rayN-windows-64.zip"),
        ("linux", "Linux · v2rayN", "sudo apt install -y ./v2rayN-linux-64.deb"),
    ],
)
async def test_a_device_with_one_app_goes_straight_to_its_guide(
    services: Any, device: str, title: str, expected: str
) -> None:
    await active_owner(services)
    message = message_for(OWNER)
    callback = callback_for(f"setup:guide:{device}", message)
    await setup_action(callback, AsyncMock(), services)
    text = message.answer.call_args.args[0]
    assert title in text
    assert f"<code>{SUBSCRIPTION}</code>" in text
    assert expected in text
    assert "setup:guide:" + device not in [
        b.callback_data
        for b in buttons(message.answer.call_args.kwargs["reply_markup"])
    ]


async def test_macos_asks_which_app_because_it_has_two(services: Any) -> None:
    await active_owner(services)
    message = message_for(OWNER)
    await setup_action(
        callback_for("setup:guide:macos", message), AsyncMock(), services
    )
    markup = message.answer.call_args.kwargs["reply_markup"]
    assert [b.callback_data for b in buttons(markup)][:2] == [
        "setup:app:macos:onexray",
        "setup:app:macos:v2rayn",
    ]


async def test_without_access_the_guide_says_so_and_sends_no_link(
    services: Any,
) -> None:
    await services.db.create_user(OWNER, "owner", None)
    await services.db.accept_offer(OWNER)
    await services.db.set_user_status(OWNER, "expired")
    services.provisioning.fresh_config = AsyncMock()
    message = message_for(OWNER)
    await setup_action(
        callback_for("setup:app:android:v2rayng", message), AsyncMock(), services
    )
    text = message.answer.call_args.args[0]
    kb = buttons(message.answer.call_args.kwargs["reply_markup"])
    assert "Конфигурация доступна только при активной аренде" in text
    assert "<code>" not in text
    assert all(b.copy_text is None for b in kb)
    assert any(b.url for b in kb)  # the app can be installed meanwhile
    services.provisioning.fresh_config.assert_not_awaited()


async def test_without_the_offer_nothing_is_sent_but_the_request_for_it(
    services: Any,
) -> None:
    await services.db.create_user(OWNER, "owner", None)
    services.provisioning.fresh_config = AsyncMock()
    message = message_for(OWNER)
    await setup_action(
        callback_for("setup:app:android:v2rayng", message), AsyncMock(), services
    )
    assert "оферту" in message.answer.call_args.args[0]
    services.provisioning.fresh_config.assert_not_awaited()


@pytest.mark.parametrize(
    "data",
    [
        "setup:app:android:karing",  # another device's app
        "setup:app:ios:foxray",  # an app that was taken out
        "setup:app:ios:v2box",
        "setup:app:windows:nekoray",
        "setup:app:chromeos:v2rayn",  # a device that is not offered
        "setup:app:android",  # truncated
        "setup:app:android:v2rayng:extra",
        "setup:guide:chromeos",
        "setup:guide:amnezia",  # no AmneziaWG server connected
    ],
)
async def test_a_stale_button_changes_nothing(services: Any, data: str) -> None:
    await active_owner(services)
    message = message_for(OWNER)
    callback = callback_for(data, message)
    await setup_action(callback, AsyncMock(), services)
    message.answer.assert_not_awaited()
    services.provisioning.fresh_config.assert_not_awaited()
    assert "/connect" in callback.answer.call_args.args[0]


async def test_the_link_is_never_sent_to_another_user_or_a_group(
    services: Any,
) -> None:
    await active_owner(services)
    private = message_for(OWNER)
    await setup_action(
        callback_for("setup:app:android:v2rayng", private, user_id=234567),
        AsyncMock(),
        services,
    )
    private.answer.assert_not_awaited()
    group = message_for(-OWNER, chat_type="group")
    await setup_action(
        callback_for("setup:app:android:v2rayng", group), AsyncMock(), services
    )
    group.answer.assert_not_awaited()
    services.provisioning.fresh_config.assert_not_awaited()


async def test_the_connect_command_shows_the_five_devices(services: Any) -> None:
    message = message_for(OWNER)
    await start_setup(message, AsyncMock(), services)
    assert message.answer.call_args.args[0] == SETUP_TEXT
    markup = message.answer.call_args.kwargs["reply_markup"]
    assert [b.text for b in buttons(markup)][:5] == [
        "Android",
        "iPhone / iPad",
        "Windows",
        "macOS",
        "Linux",
    ]
