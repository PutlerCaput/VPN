from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from handlers.payments import create_and_show
from keyboards.main_menu import channel_kb
from services.qr import render_qr_png
from tests.test_payment_creation import SBP_LINK

CHECKOUT_PAGE = "https://yoomoney.ru/checkout/payments/v2/contract/sbp?orderId=x"


def test_regular_payment_menu_contains_only_ruble_provider() -> None:
    buttons = [button for row in channel_kb().inline_keyboard for button in row]
    assert [button.callback_data for button in buttons] == ["chan:sbp"]


async def test_requested_usdt_payment_sends_invoice_qr_and_link() -> None:
    invoice_url = "https://t.me/CryptoBot?start=IV883311"
    payment = SimpleNamespace(
        payment_id="a" * 32,
        data={"pay_url": invoice_url, "amount_usdt": "3.50"},
    )
    message = SimpleNamespace(answer=AsyncMock(), answer_photo=AsyncMock())
    services = SimpleNamespace(
        billing=SimpleNamespace(create=AsyncMock(return_value=payment))
    )
    state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())

    await create_and_show(message, 12345, "usdt", 3, services, state, "prime")

    services.billing.create.assert_awaited_once_with(12345, 3, "usdt", "prime")
    message.answer.assert_awaited_once()
    assert "3.50 USDT" in message.answer.await_args.args[0]
    qr = message.answer_photo.await_args.args[0]
    assert qr.filename == "usdt-invoice.png"
    assert qr.data.startswith(b"\x89PNG\r\n\x1a\n")


async def _show_yookassa(link: str) -> SimpleNamespace:
    payment = SimpleNamespace(
        payment_id="b" * 32,
        data={
            "qr_url": link,
            "payment_url": link,
            "amount_rub": 299,
            "manual": False,
            "provider": "yookassa",
            "receipt": "self_employed",
        },
    )
    message = SimpleNamespace(answer=AsyncMock(), answer_photo=AsyncMock())
    services = SimpleNamespace(
        billing=SimpleNamespace(create=AsyncMock(return_value=payment))
    )
    state = SimpleNamespace(set_state=AsyncMock(), update_data=AsyncMock())
    await create_and_show(message, 12345, "sbp", 1, services, state, "standard")
    return message


async def test_sbp_link_is_sent_as_a_qr_code_with_a_bank_button() -> None:
    """QR confirmation: no YooMoney page, the bot shows the SBP code itself."""
    message = await _show_yookassa(SBP_LINK)
    message.answer.assert_not_awaited()
    sent = message.answer_photo.await_args
    assert sent.args[0].filename == "sbp.png"
    # The picture is the SBP link: a bank app scanning it opens this payment.
    assert sent.args[0].data == render_qr_png(SBP_LINK)
    caption = sent.kwargs["caption"]
    assert "299 ₽" in caption
    assert "b" * 32 in caption
    assert "отсканируйте QR-код" in caption
    rows = sent.kwargs["reply_markup"].inline_keyboard
    assert rows[0][0].text == "🏦 Открыть приложение банка"
    assert rows[0][0].url == SBP_LINK
    assert rows[1][0].callback_data == "paycheck:" + "b" * 32


async def test_checkout_page_link_keeps_the_page_button() -> None:
    message = await _show_yookassa(CHECKOUT_PAGE)
    message.answer_photo.assert_not_awaited()
    assert "Оплата в рублях" in message.answer.await_args.args[0]
    rows = message.answer.await_args.kwargs["reply_markup"].inline_keyboard
    assert rows[0][0].text == "💳 Оплатить через СБП"
    assert rows[0][0].url == CHECKOUT_PAGE
