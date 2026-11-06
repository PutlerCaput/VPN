from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from handlers.payments import create_and_show
from keyboards.main_menu import channel_kb


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
