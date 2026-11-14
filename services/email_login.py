"""Login to the site by a code sent to an email address.

For when Telegram cannot be reached: the site's Telegram widget then cannot sign
anyone in. An address either belongs to an account (a Telegram user tied it in the
profile) or gets a site account of its own, with a negative id and no Telegram.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from db.database import Database
from services.mailer import Mailer, MailError
from services.yookassa import is_valid_email

CODE_DIGITS = 6
MAX_ATTEMPTS = 5
MIN_INTERVAL_SECONDS = 60


def normalize_email(value: str) -> str | None:
    email = value.strip().lower()
    return email if is_valid_email(email) else None


def mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:1]}***@{domain}"


def is_site_account(telegram_id: int) -> bool:
    """An account made on the site by email: nothing of Telegram behind it."""
    return telegram_id < 0


class EmailLogin:
    def __init__(
        self, db: Database, mailer: Mailer, secret: str, ttl_seconds: int
    ) -> None:
        self.db = db
        self.mailer = mailer
        self._key = hmac.new(secret.encode(), b"email-login-code", "sha256").digest()
        self.ttl = ttl_seconds

    @property
    def enabled(self) -> bool:
        return self.mailer.enabled

    def _hash(self, email: str, code: str) -> str:
        return hmac.new(
            self._key, f"{email}\n{code}".encode(), hashlib.sha256
        ).hexdigest()

    async def send_code(
        self, email: str, purpose: str, telegram_id: int | None = None
    ) -> bool:
        """Mails a fresh code. False: one went out less than a minute ago."""
        code = f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"
        stored = await self.db.put_email_code(
            email,
            self._hash(email, code),
            purpose,
            telegram_id,
            self.ttl,
            MIN_INTERVAL_SECONDS,
        )
        if not stored:
            return False
        minutes = max(1, self.ttl // 60)
        try:
            await self.mailer.send(
                email,
                f"Код входа: {code}",
                f"Код для входа в Cyberdefency: {code}\n\n"
                f"Он действует {minutes} мин. и подходит один раз.\n"
                "Если вы не запрашивали код, ничего делать не нужно.\n",
            )
        except MailError:
            # Nothing was delivered: the next request must not wait a minute.
            await self.db.drop_email_code(email)
            raise
        return True

    async def check(self, email: str, code: str, purpose: str) -> dict[str, object]:
        if not (code.isascii() and code.isdigit() and len(code) == CODE_DIGITS):
            return {"result": "wrong", "telegram_id": None}
        return await self.db.check_email_code(
            email, self._hash(email, code), purpose, MAX_ATTEMPTS
        )
