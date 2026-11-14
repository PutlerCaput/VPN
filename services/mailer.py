"""Sends the login code by email (SMTP). Off until SMTP_HOST is set."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from config import Settings

logger = logging.getLogger(__name__)
TIMEOUT_SECONDS = 20


class MailError(RuntimeError):
    pass


class Mailer:
    def __init__(self, settings: Settings) -> None:
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.username = settings.smtp_username
        self._password = (
            settings.smtp_password.get_secret_value()
            if settings.smtp_password
            else None
        )
        self.sender = settings.smtp_from or settings.smtp_username
        self.security = settings.smtp_security
        self.sender_name = settings.smtp_from_name

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.sender)

    def _send(self, message: EmailMessage) -> None:
        if not self.host:
            raise MailError("smtp is not configured")
        context = ssl.create_default_context()
        client: smtplib.SMTP
        if self.security == "tls":
            client = smtplib.SMTP_SSL(
                self.host, self.port, timeout=TIMEOUT_SECONDS, context=context
            )
        else:
            client = smtplib.SMTP(self.host, self.port, timeout=TIMEOUT_SECONDS)
        with client:
            if self.security == "starttls":
                client.starttls(context=context)
            if self.username and self._password:
                client.login(self.username, self._password)
            client.send_message(message)

    async def send(self, to: str, subject: str, text: str) -> None:
        if not self.enabled:
            raise MailError("smtp is not configured")
        message = EmailMessage()
        message["From"] = formataddr((self.sender_name, str(self.sender)))
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        try:
            await asyncio.to_thread(self._send, message)
        except (OSError, smtplib.SMTPException) as exc:
            # The address and the code stay out of the log.
            logger.warning("mail_send_failed", extra={"error": type(exc).__name__})
            raise MailError("send failed") from exc
