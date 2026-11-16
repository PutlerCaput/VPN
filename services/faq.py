"""Answers to the questions customers ask most often.

A question is matched by keyword stems in the customer's own words. No third-party
service sees the message, and the bot does not store it. Each answer repeats the
offer (legal/oferta.md) or a screen of the bot. Figures come from config.py, so a
change of price or limit reaches the answers without edits here. A question that
matches no topic gets the topic buttons and the support button, where a person
replies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from config import SELLABLE_TIERS, TIER_PRICES_RUB, TIERS

_SEPARATORS = re.compile(r"[^0-9a-zа-я]+")
_TIER = TIERS[SELLABLE_TIERS[0]]
_PRICES = TIER_PRICES_RUB[SELLABLE_TIERS[0]]

NOT_FOUND_TEXT = (
    "Не нашёл ответа на этот вопрос. Выберите тему ниже или нажмите "
    "«✍️ Написать человеку» — ответит человек в этом чате."
)


def normalize(text: str) -> str:
    """Lower case, ё written as е, punctuation and repeated spaces removed."""
    cleaned = _SEPARATORS.sub(" ", text.casefold().replace("ё", "е"))
    return " ".join(cleaned.split())


@dataclass(frozen=True, slots=True)
class Topic:
    slug: str  # callback data is "faq:" + slug
    label: str  # text of the topic button
    keywords: tuple[str, ...]  # stems in normalize() form
    answer: str  # HTML, sent with the bot's default parse mode
    in_menu: bool = True  # False: reachable by a question, not by a button


def _tariff_answer() -> str:
    months = sorted(_PRICES)
    prices = "\n".join(f"• {m} мес. — {_PRICES[m]} ₽" for m in months)
    return (
        f"💰 <b>Тариф «{_TIER.title}»</b>\n\n"
        f"• до {_TIER.devices} устройств по одной подписке\n"
        f"• {_TIER.traffic_gb} ГБ трафика на каждые 30 дней, "
        "общий для всех устройств\n"
        f"• срок от {months[0]} до {months[-1]} месяцев\n\n"
        f"<b>Цена за весь срок:</b>\n{prices}\n\n"
        "Выбрать и оплатить: «💳 Оплата»."
    )


def _devices_answer() -> str:
    return (
        "📲 <b>Устройства</b>\n\n"
        f"По одной подписке можно подключить до {_TIER.devices} устройств, "
        "например телефон и ноутбук.\n\n"
        "Чтобы заменить устройство, нажмите «🔄 Сбросить ссылку» в разделе "
        "«🔐 Моя конфигурация»: старые копии ссылки перестанут работать, "
        "а устройства подключаются заново в пределах лимита."
    )


def _traffic_answer() -> str:
    return (
        "📊 <b>Трафик</b>\n\n"
        f"В тарифе «{_TIER.title}» — {_TIER.traffic_gb} ГБ на каждые 30 дней. "
        "Лимит общий для всех устройств подписки. Бот предупредит, когда "
        "израсходовано 80%. Текущий расход — в «👤 Профиль».\n\n"
        "Когда лимит исчерпан, доступ приостанавливается до обнуления счётчика. "
        "Оплата нового периода счётчик не обнуляет."
    )


TRIAL_ANSWER = (
    "🎁 <b>Пробный период</b>\n\n"
    "Один раз на один аккаунт Telegram: 24 часа бесплатного доступа по составу "
    f"тарифа «{_TIER.title}». Он открывается после принятия оферты (/start). Лимит "
    "трафика пробного периода показан в «👤 Профиль»."
)

PAYMENT_ANSWER = (
    "💳 <b>Оплата</b>\n\n"
    "Способы оплаты показываются в «💳 Оплата» в момент покупки. Сейчас это "
    "Telegram Stars и СБП.\n\n"
    "Счёт действует 1 час и затем отменяется сам. Одновременно может быть только "
    "один неоплаченный счёт: его можно отменить в боте и выбрать другой срок."
)

PAYMENT_MISSING_ANSWER = (
    "🧾 <b>Оплатили, а доступа нет</b>\n\n"
    "Напишите /paysupport и укажите номер заказа (он есть в счёте), способ оплаты "
    "и примерное время. Для Telegram Stars можно переслать сообщение об успешной "
    "оплате.\n\n"
    "Не присылайте данные карты, пароли и коды из SMS."
)

CONNECT_ANSWER = (
    "📱 <b>Подключение</b>\n\n"
    "Откройте «📱 Подключить устройство» (/connect): выберите систему, установите "
    "приложение и импортируйте подписку по ссылке. Ссылку и QR-код никому не "
    "передавайте.\n\n"
    "Если не подключается, проверьте в «👤 Профиль», что подписка активна. Если это "
    "не помогло, напишите в «💬 Поддержка» модель устройства, приложение и текст "
    "ошибки."
)

DIRECT_ANSWER = (
    "🇷🇺 <b>Российские сайты напрямую</b>\n\n"
    "Можно сделать так, чтобы банки, Госуслуги, маркетплейсы и другие российские "
    "сайты открывались напрямую, через вашего обычного провайдера, а остальное "
    "шло через сервер. Это делает само приложение на устройстве: откройте "
    "«📱 Подключить устройство» (/connect) → устройство → приложение. В "
    "инструкции сказано, работает ли это в нём само или какие пункты включить, "
    "и как вернуть всё через сервер.\n\n"
    "Шифрование сервиса на прямые соединения не действует."
)

CONFIG_ANSWER = (
    "🔐 <b>Моя конфигурация</b>\n\n"
    "Ссылка подключения и QR-код — в разделе «🔐 Моя конфигурация». Не публикуйте "
    "их. Если ссылка попала к посторонним, нажмите «🔄 Сбросить ссылку»: старые "
    "копии перестанут работать."
)

AUTORENEW_ANSWER = (
    "🔁 <b>Автопродление</b>\n\n"
    "Автопродление есть только для оплаты Telegram Stars: «💳 Оплата» → "
    "«⭐ Telegram Stars» → тариф → «🔁 Каждый месяц с автопродлением». Списание "
    "идёт каждые 30 дней.\n\n"
    "Статус и отключение — команда /autorenew. Отключить можно кнопкой "
    "«Отключить автопродление»; оплаченный период действует до конца."
)

REFUND_ANSWER = (
    "↩️ <b>Возврат и отказ от услуги</b>\n\n"
    "Отказаться от услуги можно в любое время: напишите /paysupport и укажите, что "
    "хотите отказаться от подписки. Возвращается стоимость неиспользованных дней за "
    "вычетом фактически понесённых расходов, в течение 10 дней со дня получения "
    "заявления. Деньги возвращаются тем же способом, которым была оплата; оплата в "
    "Telegram Stars возвращается звёздами через Telegram.\n\n"
    "Пробный период не возвращается. Если услуга не работала по вине сервиса дольше "
    "72 часов подряд, на выбор возвращается стоимость неоказанной части или период "
    "продлевается (оферта, п. 6.2)."
)

PRIVACY_ANSWER = (
    "🔒 <b>Персональные данные</b>\n\n"
    "Политика обработки — команда /privacy. Запрос о своих данных или об их "
    "удалении отправьте через бот: /privacy или «💬 Поддержка». Ответ — в течение "
    "10 рабочих дней.\n\n"
    "Данные, которые закон обязывает хранить (например, сведения о платежах для "
    "налогового учёта), удаляются по истечении установленного срока, остальные — "
    "в течение 30 дней после запроса."
)

ABOUT_ANSWER = (
    "ℹ️ <b>Что это за сервис</b>\n\n"
    "Cyberdefency шифрует трафик между вашим устройством и нашим сервером в "
    "Финляндии (Хельсинки). Это защищает данные в публичных сетях: в кафе, "
    "отелях, аэропортах и транспорте.\n\n"
    "• Протоколы на выбор: VLESS + Reality или AmneziaWG\n"
    "• При подключении через AmneziaWG работает фильтр вредоносных, "
    "фишинговых сайтов и 18+\n"
    "• Пошаговая инструкция для вашего устройства — прямо в боте\n\n"
    "Полную анонимность и работу в любой сети сервис не гарантирует. "
    "Условия — «📄 Оферта»."
)

OFFER_ANSWER = (
    "📄 <b>Оферта</b>\n\n"
    "Полный текст оферты и политика обработки персональных данных — в разделе "
    "«📄 Оферта». Там условия тарифов, оплаты, возврата и ответственности сторон."
)

SUPPORT_ANSWER = (
    "💬 <b>Поддержка</b>\n\n"
    "Выберите тему ниже. Если ответа нет, нажмите «✍️ Написать человеку»: ответит "
    "живой человек в этом чате. По оплате укажите номер заказа и пишите /paysupport."
)

TOPICS: tuple[Topic, ...] = (
    Topic(
        slug="payment_missing",
        label="🧾 Оплатил, а доступа нет",
        keywords=(
            "доступа нет",
            "доступ не",
            "не зачисл",
            "не прише",
            "не пришл",
            "не активир",
            "не появил",
            "списал",
            "деньги ушли",
            "не продлил",
        ),
        answer=PAYMENT_MISSING_ANSWER,
    ),
    Topic(
        slug="trial",
        label="🎁 Пробный период",
        keywords=("пробн", "бесплатн", "триал", "trial", "попробов"),
        answer=TRIAL_ANSWER,
    ),
    Topic(
        slug="tariff",
        label="💰 Тарифы и цены",
        keywords=("тариф", "цен", "стоимост", "стоит", "сколько", "прайс", "дорог"),
        answer=_tariff_answer(),
    ),
    Topic(
        slug="payment",
        label="💳 Оплата",
        keywords=(
            "оплат",
            "оплач",
            "способ",
            "карт",
            "сбп",
            "звезд",
            "stars",
            "счет",
            "купит",
            "заплат",
            "платеж",
            "продлит",
        ),
        answer=PAYMENT_ANSWER,
    ),
    Topic(
        slug="connect",
        label="📱 Подключение",
        keywords=(
            "подключ",
            "настро",
            "установ",
            "импорт",
            "приложени",
            "инструкц",
            "v2ray",
            "nekobox",
            "nekoray",
            "foxray",
            "v2box",
            "karing",
            "onexray",
            "впн не работ",
            "vpn не работ",
        ),
        answer=CONNECT_ANSWER,
    ),
    Topic(
        slug="direct",
        label="🇷🇺 Российские сайты",
        keywords=(
            "российские сайт",
            "российских сайт",
            "русские сайт",
            "русских сайт",
            "российские сервис",
            "российских сервис",
            "госуслуг",
            "сбербанк",
            "сбер онлайн",
            "тинькофф",
            "вайлдберриз",
            "wildberries",
            "ozon",
            "озон",
            "раздельн",
            "туннелир",
            "split tunnel",
            "напрямую",
            "мимо сервер",
            "без впн",
            "без vpn",
            "split",
        ),
        answer=DIRECT_ANSWER,
    ),
    Topic(
        slug="devices",
        label="📲 Устройства",
        keywords=(
            "устройств",
            "сколько устрой",
            "телефон",
            "ноутбук",
            "компьютер",
            "планшет",
            "смартфон",
        ),
        answer=_devices_answer(),
    ),
    Topic(
        slug="config",
        label="🔐 Моя ссылка",
        keywords=(
            "конфигурац",
            "ссылк",
            "qr",
            "куар",
            "сброс",
            "утек",
            "украл",
            "посторонн",
        ),
        answer=CONFIG_ANSWER,
    ),
    Topic(
        slug="traffic",
        label="📊 Трафик",
        keywords=(
            "трафик",
            "гб",
            "гигабайт",
            "мегабайт",
            "лимит",
            "безлимит",
            "счетчик",
            "сколько трафик",
            "сколько гб",
            "сколько гигабайт",
        ),
        answer=_traffic_answer(),
    ),
    Topic(
        slug="autorenew",
        label="🔁 Автопродление",
        keywords=(
            "автопродл",
            "автоматическ",
            "каждый месяц",
            "ежемесячн",
            "продлени",
            "отключить продл",
            "отменить продл",
            "списыва",
        ),
        answer=AUTORENEW_ANSWER,
    ),
    Topic(
        slug="refund",
        label="↩️ Возврат",
        keywords=(
            "возврат",
            "вернут",
            "верните",
            "вернете",
            "деньги назад",
            "отменить подписк",
            "удалить подписк",
            "отказаться",
            "отказ от",
        ),
        answer=REFUND_ANSWER,
    ),
    Topic(
        slug="privacy",
        label="🔒 Мои данные",
        keywords=(
            "персональн",
            "удалить данные",
            "удаление данных",
            "удалить аккаунт",
            "удалить учетн",
            "конфиденц",
            "журнал",
            "хранени",
            "хранит",
            "слеж",
            "следит",
            "политик",
        ),
        answer=PRIVACY_ANSWER,
    ),
    Topic(
        slug="about",
        label="ℹ️ Что это за сервис",
        keywords=(
            "что за сервис",
            "что это за сервис",
            "как работает сервис",
            "для чего",
            "зачем",
            "безопасн",
            "анонимн",
            "шифр",
            "публичн",
        ),
        answer=ABOUT_ANSWER,
    ),
    Topic(
        slug="offer",
        label="📄 Оферта",
        keywords=("оферт", "договор", "соглашен"),
        answer=OFFER_ANSWER,
    ),
    Topic(
        slug="support",
        label="💬 Поддержка",
        keywords=(
            "поддержк",
            "оператор",
            "живой",
            "живого",
            "человек",
            "связаться",
            "менеджер",
            "помогите",
            "помощь",
        ),
        answer=SUPPORT_ANSWER,
        in_menu=False,
    ),
)

TOPICS_BY_SLUG: dict[str, Topic] = {topic.slug: topic for topic in TOPICS}


def match_topic(text: str) -> Topic | None:
    """Return the topic whose keywords cover most of the text.

    Longer keywords are more specific and weigh more. On a tie the topic listed
    first in TOPICS wins, so the more specific topics come first there.
    """
    normalized = normalize(text)
    best: Topic | None = None
    best_score = 0
    for topic in TOPICS:
        score = sum(len(word) for word in topic.keywords if word in normalized)
        if score > best_score:
            best, best_score = topic, score
    return best
