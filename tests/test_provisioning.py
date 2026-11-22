from __future__ import annotations

import asyncio
import time
import uuid as uuidlib
from typing import Any

import pytest

from db.database import Database
from services.hiddify import HiddifyError
from services.provisioning import AUTOBAN_TEXT
from tests.conftest import FakeHiddify, FakeNotifier


async def _register(
    db: Database, telegram_id: int, referred_by: int | None = None
) -> None:
    await db.create_user(telegram_id, "user", referred_by)


async def test_trial_requires_user_and_is_single_use(
    db: Database,
    provisioning: Any,
    hiddify: FakeHiddify,
) -> None:
    result = await provisioning.accept_offer_and_start_trial(100001)
    assert not result.ok
    await _register(db, 100001)
    result = await provisioning.accept_offer_and_start_trial(100001)
    assert result.ok
    assert result.reason == "provisioned"
    assert result.config_link.startswith("vless://")
    user = await db.get_user(100001)
    assert user["status"] == "active"
    name = user["node_username"]
    assert uuidlib.UUID(name).version == 4
    assert "100001" not in name
    assert 86000 < user["expires_at"] - time.time() <= 86400
    assert await db.has_accepted_offer(100001)
    again = await provisioning.accept_offer_and_start_trial(100001)
    assert again.reason == "already_used"
    assert [c for c in hiddify.calls if c[0] == "create"] == [("create", name)]


async def test_payment_is_idempotent_and_extends(
    db: Database, provisioning: Any
) -> None:
    await _register(db, 200002)
    await provisioning.accept_offer_and_start_trial(200002)
    before = (await db.get_user(200002))["expires_at"]
    await db.create_payment(
        payment_id="a" * 32,
        telegram_id=200002,
        amount=30000,
        currency="RUB",
        months=1,
        method="sbp",
    )
    assert await provisioning.process_paid_payment("a" * 32)
    assert not await provisioning.process_paid_payment("a" * 32)
    after = (await db.get_user(200002))["expires_at"]
    assert after - before == 30 * 86400


async def test_paid_payment_retries_without_extending_twice(
    db: Database, provisioning: Any, hiddify: FakeHiddify
) -> None:
    await _register(db, 210002)
    await provisioning.accept_offer_and_start_trial(210002)
    before = int((await db.get_user(210002))["expires_at"])
    payment_id = "e" * 32
    await db.create_payment(
        payment_id=payment_id,
        telegram_id=210002,
        amount=30000,
        currency="RUB",
        months=1,
        method="sbp",
    )

    node = (await db.get_user(210002))["node_username"]
    hiddify.fail_modify_once[node] = HiddifyError("temporary panel outage")
    assert await provisioning.process_paid_payment(payment_id)
    activation = await db.get_payment_activation(payment_id)
    assert activation["status"] == "pending"
    fixed_target = activation["target_expires_at"]
    assert fixed_target == before + 30 * 86400
    assert int((await db.get_user(210002))["expires_at"]) == before

    assert await provisioning.process_paid_payment(payment_id)
    assert (await db.get_payment_activation(payment_id))["status"] == "completed"
    after = int((await db.get_user(210002))["expires_at"])
    assert after == fixed_target

    assert not await provisioning.process_paid_payment(payment_id)
    assert int((await db.get_user(210002))["expires_at"]) == fixed_target


async def test_paid_payment_marked_before_crash_is_picked_up_by_retry(
    db: Database, provisioning: Any
) -> None:
    await _register(db, 220002)
    await provisioning.accept_offer_and_start_trial(220002)
    before = int((await db.get_user(220002))["expires_at"])
    payment_id = "g" * 32
    await db.create_payment(
        payment_id=payment_id,
        telegram_id=220002,
        amount=30000,
        currency="RUB",
        months=1,
        method="sbp",
    )

    # Simulate a process crash after the paid status commit but before a job
    # row or Hiddify request was created.
    assert await db.mark_payment_paid(payment_id)
    pending = await db.payments_pending_activation()
    assert [row["payment_id"] for row in pending] == [payment_id]

    await provisioning.retry_paid_activations()
    assert (await db.get_payment_activation(payment_id))["status"] == "completed"
    assert int((await db.get_user(220002))["expires_at"]) == before + 30 * 86400


async def test_referral_bonus_only_on_first_payment(
    db: Database,
    provisioning: Any,
    notifier: FakeNotifier,
) -> None:
    await _register(db, 300003)
    await provisioning.accept_offer_and_start_trial(300003)
    await _register(db, 300004, referred_by=300003)
    await provisioning.accept_offer_and_start_trial(300004)
    inviter_before = (await db.get_user(300003))["expires_at"]
    invitee_before = (await db.get_user(300004))["expires_at"]
    for pid in ("b" * 32, "c" * 32):
        await db.create_payment(
            payment_id=pid,
            telegram_id=300004,
            amount=350,
            currency="USDT",
            months=1,
            method="usdt",
        )
        await provisioning.process_paid_payment(pid)
    inviter_after = (await db.get_user(300003))["expires_at"]
    invitee_after = (await db.get_user(300004))["expires_at"]
    assert inviter_after - inviter_before == 15 * 86400
    assert invitee_after - invitee_before == (60 + 3) * 86400
    assert await db.referral_bonus_days(300003) == 15
    assert any("+15" in text for tid, text in notifier.messages if tid == 300003)


async def test_referral_bonus_retries_each_side_without_duplicate_days(
    db: Database, provisioning: Any, hiddify: FakeHiddify
) -> None:
    await _register(db, 310003)
    await provisioning.accept_offer_and_start_trial(310003)
    await _register(db, 310004, referred_by=310003)
    await provisioning.accept_offer_and_start_trial(310004)
    inviter_before = int((await db.get_user(310003))["expires_at"])
    invitee_before = int((await db.get_user(310004))["expires_at"])
    await db.create_payment(
        payment_id="f" * 32,
        telegram_id=310004,
        amount=350,
        currency="USDT",
        months=1,
        method="usdt",
    )

    # The payment itself provisions the invitee; fail only the inviter's
    # separate reward so the retry ledger must resume that side alone.
    inviter_node = (await db.get_user(310003))["node_username"]
    hiddify.fail_modify_once[inviter_node] = HiddifyError("temporary panel outage")
    assert await provisioning.process_paid_payment("f" * 32)
    job = await db.get_referral_bonus_job(310004)
    assert job["status"] == "pending"
    assert job["inviter_target_expires_at"] == inviter_before + 15 * 86400
    assert job["inviter_applied_at"] is None
    assert job["invitee_applied_at"] is not None
    assert int((await db.get_user(310004))["expires_at"]) == invitee_before + 33 * 86400

    await provisioning.retry_pending_referral_bonuses()
    job = await db.get_referral_bonus_job(310004)
    assert job["status"] == "completed"
    assert int((await db.get_user(310003))["expires_at"]) == inviter_before + 15 * 86400
    invitee_after = int((await db.get_user(310004))["expires_at"])
    assert invitee_after == invitee_before + 33 * 86400

    await provisioning.retry_pending_referral_bonuses()
    assert int((await db.get_user(310003))["expires_at"]) == inviter_before + 15 * 86400
    assert int((await db.get_user(310004))["expires_at"]) == invitee_after


async def test_referral_job_is_recovered_after_payment_activation_crash(
    db: Database, provisioning: Any
) -> None:
    await _register(db, 320003)
    await provisioning.accept_offer_and_start_trial(320003)
    await _register(db, 320004, referred_by=320003)
    await provisioning.accept_offer_and_start_trial(320004)
    inviter_before = int((await db.get_user(320003))["expires_at"])
    invitee_before = int((await db.get_user(320004))["expires_at"])
    await db.create_payment(
        payment_id="h" * 32,
        telegram_id=320004,
        amount=30000,
        currency="RUB",
        months=1,
        method="sbp",
    )

    original = provisioning.apply_referral_bonus

    async def simulate_crash(_: int) -> None:
        raise RuntimeError("simulated crash after payment activation")

    provisioning.apply_referral_bonus = simulate_crash
    with pytest.raises(RuntimeError, match="simulated crash"):
        await provisioning.process_paid_payment("h" * 32)
    provisioning.apply_referral_bonus = original

    # The periodic retry must discover the completed first payment even
    # though no referral_bonuses row had been written before the crash.
    await provisioning.retry_pending_referral_bonuses()
    job = await db.get_referral_bonus_job(320004)
    assert job["status"] == "completed"
    assert int((await db.get_user(320003))["expires_at"]) == inviter_before + 15 * 86400
    assert int((await db.get_user(320004))["expires_at"]) == invitee_before + 33 * 86400


async def test_expiration_target_keeps_suspended_user_disabled(
    db: Database, provisioning: Any, hiddify: FakeHiddify
) -> None:
    await _register(db, 330003)
    await provisioning.accept_offer_and_start_trial(330003)
    await db.set_user_status(330003, "suspended")
    node = (await db.get_user(330003))["node_username"]
    hiddify.users[node]["status"] = "disabled"

    async with provisioning._lock(330003):
        await provisioning._apply_expiration_target(
            330003, int(time.time()) + 60 * 86400
        )

    assert hiddify.users[node]["status"] == "disabled"
    assert (await db.get_user(330003))["status"] == "suspended"


async def test_autoban_from_flags(
    services: Any, db: Database, hiddify: FakeHiddify, notifier: FakeNotifier
) -> None:
    await _register(db, 400004)
    await services.provisioning.accept_offer_and_start_trial(400004)
    node = (await db.get_user(400004))["node_username"]
    hiddify.panel_usage = [
        {
            "username": node,
            "status": "active",
            "used_traffic": 10,
            "note": "ids: PORT_SCANNING MALICIOUS_PROBE",
        },
    ]
    await services.monitor.poll()
    user = await db.get_user(400004)
    assert user["status"] == "banned"
    assert hiddify.users[node]["status"] == "disabled"
    anomalies = await db.list_anomalies(400004)
    assert anomalies[0]["kind"] == "AUTOBAN"
    assert (400004, AUTOBAN_TEXT) in notifier.messages


async def test_autoban_remains_pending_until_hiddify_confirms_disable(
    db: Database, provisioning: Any, hiddify: FakeHiddify
) -> None:
    await _register(db, 410004)
    await provisioning.accept_offer_and_start_trial(410004)
    node = (await db.get_user(410004))["node_username"]
    hiddify.fail_disable_once = HiddifyError("temporary panel outage")

    assert not await provisioning.autoban(
        410004, node, "admin", {"flags": ["CONFIRMED_ABUSE"]}
    )
    user = await db.get_user(410004)
    assert user["status"] == "active"
    pending = await db.get_pending_ban(410004)
    assert pending is not None
    assert pending["attempts"] == 1
    assert hiddify.users[node]["status"] == "active"

    await provisioning.retry_pending_bans()
    user = await db.get_user(410004)
    assert user["status"] == "banned"
    assert await db.get_pending_ban(410004) is None
    assert hiddify.users[node]["status"] == "disabled"


async def test_autoban_on_traffic_and_soft_suspend(
    services: Any, db: Database, hiddify: FakeHiddify
) -> None:
    gb = 1024**3
    names: dict[int, str] = {}
    for tid in (500005, 500006):
        await _register(db, tid)
        await services.provisioning.accept_offer_and_start_trial(tid)
        names[tid] = (await db.get_user(tid))["node_username"]
    hiddify.panel_usage = [
        {"username": names[500005], "status": "active", "used_traffic": 0},
        {"username": names[500006], "status": "active", "used_traffic": 0},
    ]
    await services.monitor.poll()  # establishes today's baseline
    hiddify.panel_usage = [
        {"username": names[500005], "status": "active", "used_traffic": 2100 * gb},
        {"username": names[500006], "status": "active", "used_traffic": 900 * gb},
    ]
    await services.monitor.poll()
    assert (await db.get_user(500005))["status"] == "banned"
    assert (await db.get_user(500006))["status"] == "suspended"
    assert await services.provisioning.unsuspend(500006)
    assert (await db.get_user(500006))["status"] == "active"


async def test_expire_user(
    db: Database, provisioning: Any, hiddify: FakeHiddify
) -> None:
    await _register(db, 600006)
    await provisioning.accept_offer_and_start_trial(600006)
    node = (await db.get_user(600006))["node_username"]
    await db.set_subscription(
        600006, status="active", expires_at=1, node_username=node, config_link=None
    )
    for user in await db.users_to_expire(int(time.time())):
        await provisioning.expire_user(user)
    assert (await db.get_user(600006))["status"] == "expired"
    assert hiddify.users[node]["status"] == "disabled"


async def test_banned_user_is_not_reactivated_by_payment(
    db: Database, provisioning: Any
) -> None:
    await _register(db, 700007)
    await provisioning.accept_offer_and_start_trial(700007)
    await provisioning.autoban(700007, None, "admin", {"flags": ["CYBERCRIME"]})
    await db.create_payment(
        payment_id="d" * 32,
        telegram_id=700007,
        amount=30000,
        currency="RUB",
        months=1,
        method="sbp",
    )
    await provisioning.process_paid_payment("d" * 32)
    assert (await db.get_user(700007))["status"] == "banned"


class _ContendedLock(asyncio.Lock):
    """Signals when a second caller starts waiting for the lock."""

    def __init__(self) -> None:
        super().__init__()
        self.contended = asyncio.Event()

    async def acquire(self) -> bool:
        if self.locked():
            self.contended.set()
        return await super().acquire()


async def test_refund_recorded_while_activation_waits_wins(
    db: Database, provisioning: Any
) -> None:
    await _register(db, 710007)
    await provisioning.accept_offer_and_start_trial(710007)
    before = int((await db.get_user(710007))["expires_at"])
    payment_id = "f" * 32
    await db.create_payment(
        payment_id=payment_id,
        telegram_id=710007,
        amount=30000,
        currency="RUB",
        months=1,
        method="sbp",
    )
    lock = _ContendedLock()
    provisioning._locks[710007] = lock
    await lock.acquire()
    task = asyncio.create_task(provisioning.process_paid_payment(payment_id))
    await asyncio.wait_for(lock.contended.wait(), timeout=2)
    # The refund lands while the activation still waits for the lock.
    assert (await db.get_payment(payment_id))["status"] == "paid"
    assert await db.mark_payment_refunded(payment_id)
    lock.release()
    assert await task is False
    assert int((await db.get_user(710007))["expires_at"]) == before
    activation = await db.get_payment_activation(payment_id)
    assert activation is None or activation["status"] != "completed"
    assert await db.payments_pending_activation() == []


async def test_create_user_stores_uuid4_in_node_username(
    db: Database, provisioning: Any, hiddify: FakeHiddify
) -> None:
    await _register(db, 100101)
    first = await provisioning.accept_offer_and_start_trial(100101)
    assert first.ok
    stored = (await db.get_user(100101))["node_username"]
    assert uuidlib.UUID(stored).version == 4
    assert stored in hiddify.users
    assert ("create", stored) in hiddify.calls


async def test_revoke_subscription_rotates_uuid(
    db: Database, provisioning: Any, hiddify: FakeHiddify
) -> None:
    await _register(db, 100102)
    await provisioning.accept_offer_and_start_trial(100102)
    before = await db.get_user(100102)
    old = before["node_username"]
    old_link = before["config_link"]
    new_link = await provisioning.revoke_link(100102)
    after = await db.get_user(100102)
    new = after["node_username"]
    assert uuidlib.UUID(new).version == 4
    assert new != old
    assert new_link != old_link
    assert after["config_link"] == new_link
    assert old not in hiddify.users
    assert new in hiddify.users
    assert ("revoke", old) in hiddify.calls
    assert after["expires_at"] == before["expires_at"]


async def test_revoke_keeps_confirmed_uuid_when_config_lookup_fails(
    db: Database,
    provisioning: Any,
    hiddify: FakeHiddify,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _register(db, 100103)
    await provisioning.accept_offer_and_start_trial(100103)
    before = await db.get_user(100103)
    old = before["node_username"]
    rotate = hiddify.revoke_subscription
    get_user = hiddify.get_user

    async def rotate_without_link(username: str) -> dict[str, Any]:
        data = await rotate(username)
        return {"uuid": data["uuid"]}

    async def unavailable(username: str) -> dict[str, Any]:
        raise HiddifyError("unavailable")

    monkeypatch.setattr(hiddify, "revoke_subscription", rotate_without_link)
    monkeypatch.setattr(hiddify, "get_user", unavailable)
    with pytest.raises(HiddifyError):
        await provisioning.revoke_link(100103)

    after = await db.get_user(100103)
    new = after["node_username"]
    assert new != old and new in hiddify.users
    assert after["config_link"] == ""
    assert await db.get_user_by_node(old) is None
    monkeypatch.setattr(hiddify, "get_user", get_user)
    assert new in await provisioning.fresh_config(100103)


async def test_payment_replaces_a_marzban_era_node_username(
    db: Database, provisioning: Any
) -> None:
    """A legacy Marzban username is not a Hiddify UUID: payment still provisions."""
    await _register(db, 200099)
    await provisioning.accept_offer_and_start_trial(200099)
    await db.set_node_username(200099, "tg_200099")
    await db.create_payment(
        payment_id="c" * 32,
        telegram_id=200099,
        amount=29900,
        currency="RUB",
        months=1,
        method="sbp",
    )
    assert await provisioning.process_paid_payment("c" * 32)
    node = (await db.get_user(200099))["node_username"]
    assert node != "tg_200099"
    assert str(uuidlib.UUID(node)) == node
