"""Regression for the exact empty InlineKeyboardButton reported in production."""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pydantic import ValidationError

from scripts.fix_empty_payment_button import remove_empty_row

BROKEN = """# keep surrounding module content
BEFORE = "untouched"

def channel_kb(months=None, tier=None):
    suffix = f":{tier}:{months}" if tier and months else (f":{tier}" if tier else "")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🇷🇺 Рубли: СБП или карта (я в России)",
                    callback_data=f"chan:sbp{suffix}",
                )
            ],
            [
                InlineKeyboardButton(
                )
            ],
        ],
    )

AFTER = "untouched too"
"""


def compile_keyboard(source: str) -> Any:
    scope: dict[str, Any] = {
        "InlineKeyboardButton": InlineKeyboardButton,
        "InlineKeyboardMarkup": InlineKeyboardMarkup,
    }
    exec(compile(source, "<payment button regression>", "exec"), scope)  # noqa: S102
    return scope["channel_kb"]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_reported_menu_crash_is_repaired_without_changing_other_code(
    newline: str,
) -> None:
    original = BROKEN.replace("\n", newline)
    with pytest.raises(ValidationError, match="text"):
        compile_keyboard(original)()
    fixed = remove_empty_row(original)
    keyboard = compile_keyboard(fixed)
    for months, tier, expected in (
        (None, None, "chan:sbp"),
        (3, "basic", "chan:sbp:basic:3"),
        (None, "prime", "chan:sbp:prime"),
    ):
        rows = keyboard(months, tier).inline_keyboard
        assert len(rows) == 1
        assert len(rows[0]) == 1
        assert rows[0][0].callback_data == expected
    empty_row = """            [
                InlineKeyboardButton(
                )
            ],
""".replace("\n", newline)
    assert fixed == original.replace(empty_row, "", 1)


def test_hotfix_refuses_a_different_or_already_fixed_keyboard() -> None:
    fixed = remove_empty_row(BROKEN)
    with pytest.raises(RuntimeError, match="EXPECTED_ONE_EMPTY_BUTTON_ROW"):
        remove_empty_row(fixed)
