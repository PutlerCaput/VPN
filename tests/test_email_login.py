"""Login to the site by a code sent to an email address (Telegram unreachable)."""

from __future__ import annotations

import re
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from services.email_login import EmailLogin, mask_email, normalize_email
from services.mailer import Mailer, MailError
from services.notifier import Notifier
from services.security import pseudonymous_node_username
from tests.conftest import make_init_data
from webhook_server import create_webhook_app

API = "/api/v1"


class Outbox:
    """Stands in for the SMTP server: keeps what would have been mailed."""

    enabled = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.broken = False

    async def send(self, to: str, subject: str, text: str) -> None:
        if self.broken:
            raise MailError("send failed")
        self.sent.append((to, subject, text))

    def code(self) -> str:
        found = re.search(r"\b(\d{6})\b", self.sent[-1][2])
        assert found
        return found.group(1)


@pytest.fixture
def outbox(services: Any) -> Outbox:
    box = Outbox()
    services.email_login = EmailLogin(
        services.db,
        box,  # type: ignore[arg-type]
        services.settings.jwt_secret.get_secret_value(),
        600,
    )
    return box


def app_of(services: Any) -> Any:
    return create_webhook_app(services.bot, None, services)


def wrong_for(code: str) -> str:
    return "000000" if code != "000000" else "111111"


async def sign_in(client: Any, outbox: Outbox, email: str) -> dict[str, str]:
    asked = await client.post(f"{API}/auth/email/request", json={"email": email})
    assert asked.status == 202, await asked.text()
    done = await client.post(
        f"{API}/auth/email/verify",
        json={"email": email, "code": outbox.code()},
    )
    assert done.status == 200, await done.text()
    tokens = await done.json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def telegram_headers(client: Any, services: Any, user: int) -> dict[str, str]:
    token = services.settings.bot_token.get_secret_value()
    response = await client.post(
        f"{API}/auth/telegram", json={"initData": make_init_data(token, user)}
    )
    return {"Authorization": f"Bearer {(await response.json())['access_token']}"}


async def test_a_new_address_gets_a_site_account_and_the_same_one_next_time(
    services: Any, outbox: Outbox
) -> None:
    async with TestClient(TestServer(app_of(services))) as client:
        headers = await sign_in(client, outbox, " Person@Example.com ")
        to, subject, text = outbox.sent[-1]
        assert to == "person@example.com"
        assert outbox.code() in subject
        assert "один раз" in text
        me = await (await client.get(f"{API}/me", headers=headers)).json()
        assert me["telegram_id"] < 0
        assert me["account"] == "site"
        assert me["email"] == "p***@example.com"
        assert me["email_login"] is True
        assert me["referral_link"] == ""
        # The whole product works for it: offer, trial, configuration.
        accept = await (
            await client.post(f"{API}/legal/accept", headers=headers)
        ).json()
        assert accept["trial"] == "provisioned"
        config = await (await client.get(f"{API}/config", headers=headers)).json()
        assert config["config_link"].startswith("vless://")
        # No Telegram chat: the menu cannot be sent there, and nothing is tried.
        services.bot.send_message.reset_mock()
        in_bot = await client.post(f"{API}/payments/in-bot", json={}, headers=headers)
        assert in_bot.status == 409
        services.bot.send_message.assert_not_called()
        # Later the same address signs in to the same account.
        await services.db.drop_email_code("person@example.com")
        again = await sign_in(client, outbox, "person@example.com")
        me_again = await (await client.get(f"{API}/me", headers=again)).json()
        assert me_again["telegram_id"] == me["telegram_id"]
        # Another address is another account.
        other = await sign_in(client, outbox, "other@example.com")
        me_other = await (await client.get(f"{API}/me", headers=other)).json()
        assert me_other["telegram_id"] == me["telegram_id"] - 1


async def test_a_code_works_once_and_wrong_codes_use_it_up(
    services: Any, outbox: Outbox
) -> None:
    async with TestClient(TestServer(app_of(services))) as client:
        email = "a@example.com"
        await client.post(f"{API}/auth/email/request", json={"email": email})
        code = outbox.code()
        for _ in range(4):
            bad = await client.post(
                f"{API}/auth/email/verify",
                json={"email": email, "code": wrong_for(code)},
            )
            assert bad.status == 401
        good = await client.post(
            f"{API}/auth/email/verify", json={"email": email, "code": code}
        )
        assert good.status == 200
        # Used: the same code never works again.
        replay = await client.post(
            f"{API}/auth/email/verify", json={"email": email, "code": code}
        )
        assert replay.status == 401
        # Five wrong answers and the code is gone, the right one included. (Asked of
        # the service itself: the API also limits how often one sender may try.)
        await client.post(f"{API}/auth/email/request", json={"email": email})
        code = outbox.code()
        login = services.email_login
        for _ in range(5):
            outcome = await login.check(email, wrong_for(code), "login")
            assert outcome["result"] == "wrong"
        assert (await login.check(email, code, "login"))["result"] == "none"
        # ... and one sender cannot try without end.
        statuses = set()
        for _ in range(12):
            tried = await client.post(
                f"{API}/auth/email/verify", json={"email": email, "code": "123456"}
            )
            statuses.add(tried.status)
        assert 429 in statuses


async def test_an_expired_code_and_a_code_of_another_address_do_not_work(
    services: Any, outbox: Outbox
) -> None:
    async with TestClient(TestServer(app_of(services))) as client:
        await client.post(f"{API}/auth/email/request", json={"email": "a@example.com"})
        code = outbox.code()
        other = await client.post(
            f"{API}/auth/email/verify",
            json={"email": "b@example.com", "code": code},
        )
        assert other.status == 401
        async with services.db.transaction() as conn:
            await conn.execute("UPDATE email_codes SET expires_at = 1")
        expired = await client.post(
            f"{API}/auth/email/verify",
            json={"email": "a@example.com", "code": code},
        )
        assert expired.status == 401


async def test_codes_are_not_mailed_in_bursts(services: Any, outbox: Outbox) -> None:
    async with TestClient(TestServer(app_of(services))) as client:
        email = "a@example.com"
        for _ in range(5):
            asked = await client.post(
                f"{API}/auth/email/request", json={"email": email}
            )
            assert asked.status == 202
        # One letter: the others came within the minute, and the first code holds.
        assert len(outbox.sent) == 1
        limited = await client.post(f"{API}/auth/email/request", json={"email": email})
        assert limited.status == 429
        assert len(outbox.sent) == 1


async def test_bad_input_and_a_mail_server_that_is_down(
    services: Any, outbox: Outbox
) -> None:
    async with TestClient(TestServer(app_of(services))) as client:
        for body in ({"email": "nope"}, {"email": "a@b"}, {}, {"email": 5}):
            bad = await client.post(f"{API}/auth/email/request", json=body)
            assert bad.status == 400
        assert outbox.sent == []
        outbox.broken = True
        down = await client.post(
            f"{API}/auth/email/request", json={"email": "a@example.com"}
        )
        assert down.status == 502
        # Nothing was delivered, so the next try is not made to wait a minute.
        outbox.broken = False
        retry = await client.post(
            f"{API}/auth/email/request", json={"email": "a@example.com"}
        )
        assert retry.status == 202
        assert len(outbox.sent) == 1


async def test_without_smtp_the_login_is_off_and_says_so(services: Any) -> None:
    async with TestClient(TestServer(app_of(services))) as client:
        off = await client.post(
            f"{API}/auth/email/request", json={"email": "a@example.com"}
        )
        assert off.status == 503
        verify = await client.post(
            f"{API}/auth/email/verify",
            json={"email": "a@example.com", "code": "111111"},
        )
        assert verify.status == 503
        headers = await telegram_headers(client, services, 770070)
        me = await (await client.get(f"{API}/me", headers=headers)).json()
        assert me["email_login"] is False


async def test_a_telegram_user_ties_an_address_and_signs_in_by_it(
    services: Any, outbox: Outbox
) -> None:
    async with TestClient(TestServer(app_of(services))) as client:
        headers = await telegram_headers(client, services, 770077)
        me = await (await client.get(f"{API}/me", headers=headers)).json()
        assert (me["account"], me["email"]) == ("telegram", None)
        asked = await client.post(
            f"{API}/me/email/request",
            json={"email": "me@example.com"},
            headers=headers,
        )
        assert asked.status == 202
        code = outbox.code()
        # A link code is not a login code.
        as_login = await client.post(
            f"{API}/auth/email/verify",
            json={"email": "me@example.com", "code": code},
        )
        assert as_login.status == 401
        tied = await client.post(
            f"{API}/me/email/verify",
            json={"email": "me@example.com", "code": code},
            headers=headers,
        )
        assert tied.status == 200, await tied.text()
        assert (await tied.json())["email"] == "m***@example.com"
        # Now the address opens the Telegram account, not a new one.
        by_mail = await sign_in(client, outbox, "me@example.com")
        me_by_mail = await (await client.get(f"{API}/me", headers=by_mail)).json()
        assert me_by_mail["telegram_id"] == 770077
        assert me_by_mail["account"] == "telegram"
        # One address per account.
        second = await client.post(
            f"{API}/me/email/request",
            json={"email": "two@example.com"},
            headers=headers,
        )
        assert second.status == 409


async def test_a_link_code_of_one_account_does_not_tie_the_address_to_another(
    services: Any, outbox: Outbox
) -> None:
    async with TestClient(TestServer(app_of(services))) as client:
        first = await telegram_headers(client, services, 770001)
        second = await telegram_headers(client, services, 770002)
        await client.post(
            f"{API}/me/email/request",
            json={"email": "x@example.com"},
            headers=first,
        )
        stolen = await client.post(
            f"{API}/me/email/verify",
            json={"email": "x@example.com", "code": outbox.code()},
            headers=second,
        )
        assert stolen.status == 400
        assert await services.db.email_of(770002) is None


async def test_an_address_of_another_account_cannot_be_tied_and_gets_no_letter(
    services: Any, outbox: Outbox
) -> None:
    async with TestClient(TestServer(app_of(services))) as client:
        await sign_in(client, outbox, "taken@example.com")
        letters = len(outbox.sent)
        headers = await telegram_headers(client, services, 770078)
        asked = await client.post(
            f"{API}/me/email/request",
            json={"email": "taken@example.com"},
            headers=headers,
        )
        # The same answer as for a free address: it does not tell who is registered.
        assert asked.status == 202
        assert len(outbox.sent) == letters
        assert await services.db.email_of(770078) is None


async def test_a_site_account_is_never_written_to_in_telegram(services: Any) -> None:
    services.bot.send_message.reset_mock()
    notifier = Notifier(services.bot, services.push)
    assert await notifier.telegram(-7, "text") is False
    services.bot.send_message.assert_not_called()


def test_small_parts() -> None:
    assert normalize_email("  A@B.Example ") == "a@b.example"
    assert normalize_email("a b@c.d") is None
    assert mask_email("person@example.com") == "p***@example.com"
    # A site account gets a panel name of its own, as stable as any other.
    name = pseudonymous_node_username("s" * 64, -1)
    assert name == pseudonymous_node_username("s" * 64, -1)
    assert name != pseudonymous_node_username("s" * 64, 1)
    with pytest.raises(ValueError, match="zero"):
        pseudonymous_node_username("s" * 64, 0)


def test_the_mailer_is_off_without_a_host(services: Any) -> None:
    settings = services.settings
    assert Mailer(settings).enabled is False
    configured = settings.model_copy(
        update={"smtp_host": "smtp.example.com", "smtp_username": "a@example.com"}
    )
    assert Mailer(configured).enabled is True
