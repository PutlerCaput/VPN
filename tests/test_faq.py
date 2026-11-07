"""Customer FAQ: matching, text rules that keep answers inside the offer, routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

from config import TIER_PRICES_RUB, TIERS
from handlers import build_root_router
from handlers.faq import faq_question, faq_topic
from keyboards.faq import faq_topics_kb
from services.faq import NOT_FOUND_TEXT, TOPICS, TOPICS_BY_KEY, match_topic, normalize


@pytest.mark.parametrize(
    ("question", "key"),
    [
        ("сколько стоит подписка?", "tariff"),
        ("Есть ли пробный период", "trial"),
        ("как оплатить СБП", "payment"),
        ("Оплатил, но доступа нет", "payment_missing"),
        ("не могу подключить телефон", "connect"),
        ("сколько устройств можно подключить", "devices"),
        ("как сбросить ссылку", "config"),
        ("сколько трафика", "traffic"),
        ("как отключить автопродление", "autorenew"),
        ("хочу вернуть деньги", "refund"),
        ("как удалить мои персональные данные", "privacy"),
        ("что за сервис", "about"),
        ("где оферта", "offer"),
        ("хочу поговорить с оператором", "support"),
    ],
)
def test_common_questions_reach_their_topic(question: str, key: str) -> None:
    topic = match_topic(question)
    assert topic is not None
    assert topic.key == key


@pytest.mark.parametrize("text", ["", "привет", "ок"])
def test_unrelated_text_matches_no_topic(text: str) -> None:
    assert match_topic(text) is None


def test_normalize_folds_case_yo_and_punctuation() -> None:
    assert normalize("  Ёлка, VPN!  ") == "елка vpn"


def test_topic_keys_and_callbacks_are_unique_and_fit_telegram() -> None:
    keys = [topic.key for topic in TOPICS]
    assert len(keys) == len(set(keys))
    for topic in TOPICS:
        assert len(f"faq:{topic.key}".encode()) <= 64


def test_every_answer_fits_one_message() -> None:
    for topic in TOPICS:
        assert len(topic.answer) <= 4096


FORBIDDEN_PROMISES = (
    "обойти блокировк",
    "обход блокировк",
    "обходит блокировк",
    "обходить блокировк",
    "неубиваем",
    "тспу",
    "dpi",
    "ркн",
)


def test_answers_do_not_promise_bypassing_blocks() -> None:
    for topic in TOPICS:
        lowered = topic.answer.casefold()
        for phrase in FORBIDDEN_PROMISES:
            assert phrase not in lowered, (topic.key, phrase)


def test_tariff_answer_quotes_the_configured_terms() -> None:
    standard = TIERS["standard"]
    answer = TOPICS_BY_KEY["tariff"].answer
    assert f"до {standard.devices} устройств" in answer
    assert f"{standard.traffic_gb} ГБ" in answer
    assert f"{TIER_PRICES_RUB['standard'][1]} ₽" in answer


def test_topic_buttons_offer_every_menu_topic_and_a_person() -> None:
    buttons = [button for row in faq_topics_kb().inline_keyboard for button in row]
    callbacks = [button.callback_data for button in buttons]
    for topic in TOPICS:
        if topic.in_menu:
            assert f"faq:{topic.key}" in callbacks
    assert "support:write" in callbacks
    assert "faq:support" not in callbacks


async def test_question_gets_the_matching_answer_with_topic_buttons() -> None:
    message = MagicMock(spec=Message)
    message.text = "сколько стоит подписка"
    message.answer = AsyncMock()
    await faq_question(message)
    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == TOPICS_BY_KEY["tariff"].answer
    assert "reply_markup" in message.answer.await_args.kwargs


async def test_unmatched_question_gets_the_topic_menu_not_a_guess() -> None:
    message = MagicMock(spec=Message)
    message.text = "привет"
    message.answer = AsyncMock()
    await faq_question(message)
    assert message.answer.await_args.args[0] == NOT_FOUND_TEXT


async def test_topic_button_opens_its_answer() -> None:
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.data = "faq:refund"
    callback.message = message
    callback.answer = AsyncMock()
    await faq_topic(callback)
    assert message.answer.await_args.args[0] == TOPICS_BY_KEY["refund"].answer


async def test_unknown_topic_button_asks_to_start_again() -> None:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = "faq:missing"
    callback.message = None
    callback.answer = AsyncMock()
    await faq_topic(callback)
    callback.answer.assert_awaited_once_with("Выберите тему заново.")


def test_faq_comes_after_the_support_flow() -> None:
    root = build_root_router()
    try:
        names = [child.name for child in root.sub_routers]
    finally:
        for child in list(root.sub_routers):
            root.sub_routers.remove(child)
            child._parent_router = None
    assert names.index("faq") > names.index("support")
