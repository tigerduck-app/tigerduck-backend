"""The Moodle token dropped at the last sign-out, when a sign-out races
another sign-out or a sign-in of the same student, each in a transaction
of its own (tigerduck-backend#85)."""

from __future__ import annotations

import asyncio
import base64

import pytest
from sqlalchemy import func, select, text, update

from server.auth import service
from server.auth.crypto import CredentialCipher
from server.auth.dependencies import CurrentAuth
from server.auth.models import (
    ExternalAccount,
    ExternalAccountCredential,
    User,
    UserDevice,
)
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.auth.rate_limit import SlidingWindowLimiter
from server.auth.schemas import DeviceInfo
from server.db import build_session_factory
from server.routes.user_devices import delete_device

pytestmark = pytest.mark.asyncio(loop_scope="session")

_STUDENT = "b11203058"
# Long enough for a statement that is not blocked to finish first.
_BLOCKED_FOR = 0.5


async def _student(session, devices: list[str], student_id: str = _STUDENT):
    user = User(student_id=student_id)
    session.add(user)
    await session.flush()
    account = ExternalAccount(
        user_id=user.id, provider="ntust_sso", external_user_id=student_id
    )
    session.add(account)
    session.add_all(
        UserDevice(user_id=user.id, client_device_id=d, platform="ios") for d in devices
    )
    await session.flush()
    session.add(
        ExternalAccountCredential(
            external_account_id=account.id,
            encryption_key_id="v1",
            ciphertext=b"c",
            nonce=b"n",
            aad="a",
        )
    )
    await session.commit()
    return user.id


async def _soft_delete(session, client_device_id: str) -> None:
    await session.execute(
        update(UserDevice)
        .where(UserDevice.client_device_id == client_device_id)
        .values(deleted_at=func.now())
    )


async def _forget_while_the_other_is_open(other, session, user_id) -> None:
    """Start the drop, let `other` commit, then commit the drop."""
    racing = asyncio.create_task(
        service.forget_credentials_once_signed_out(session, user_id)
    )
    await asyncio.wait({racing}, timeout=_BLOCKED_FOR)
    await other.commit()
    await racing
    await session.commit()


async def _login(session, settings, student_id: str, client_device_id: str):
    return await service.login(
        session,
        settings,
        StaticMoodleVerifier(MoodleVerifyResult(ok=True, username=student_id)),
        SlidingWindowLimiter(max_attempts=10, window_seconds=60),
        CredentialCipher(
            keys={"v1": base64.b64encode(b"0" * 32).decode()}, active_key_id="v1"
        ),
        student_id=student_id,
        moodle_token="tok-2",
        moodle_private_token=None,
        device_info=DeviceInfo(client_device_id=client_device_id, platform="ios"),
        client_ip="127.0.0.1",
    )


def _hold_logins_before_the_device(monkeypatch) -> tuple[dict, asyncio.Event]:
    """Stop each login just before it writes its device, holding its account
    locks, until the returned event is set."""
    reached: dict[str, asyncio.Event] = {}
    release = asyncio.Event()
    real = service._upsert_device

    async def held(session, *, user, info, now):
        reached.setdefault(info.client_device_id, asyncio.Event()).set()
        await release.wait()
        return await real(session, user=user, info=info, now=now)

    monkeypatch.setattr(service, "_upsert_device", held)
    return reached, release


async def _until(prepared_engine, done) -> None:
    """Wait until `done()` or another transaction waits on a lock."""
    async with prepared_engine.connect() as conn:
        for _ in range(200):
            if done():
                return
            waiting = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
            await conn.commit()
            if waiting:
                return
            await asyncio.sleep(0.05)
    raise AssertionError("nothing reached the point the test waits for")


async def _stored(session) -> int:
    return len((await session.execute(select(ExternalAccountCredential))).all())


async def test_the_last_two_devices_signing_out_together_drop_the_token(
    db_session, prepared_engine
):
    """Each saw the other's device still signed in, so neither dropped it."""
    user_id = await _student(db_session, ["iphone-1", "ipad-1"])
    factory = build_session_factory(prepared_engine)

    async with factory() as first, factory() as second:
        await _soft_delete(first, "iphone-1")
        await service.forget_credentials_once_signed_out(first, user_id)
        await _soft_delete(second, "ipad-1")
        await _forget_while_the_other_is_open(first, second, user_id)

    assert await _stored(db_session) == 0


async def test_a_sign_in_during_the_last_sign_out_keeps_its_token(
    db_session, prepared_engine, test_settings
):
    """The sign-out waited on the token row the sign-in wrote, then deleted
    it, judging by a snapshot without the new device."""
    user_id = await _student(db_session, ["iphone-1"])
    factory = build_session_factory(prepared_engine)

    async with factory() as signing_in, factory() as signing_out:
        await _login(signing_in, test_settings, _STUDENT, "ipad-1")
        await _soft_delete(signing_out, "iphone-1")
        await _forget_while_the_other_is_open(signing_in, signing_out, user_id)

    assert await _stored(db_session) == 1


async def test_a_token_sent_while_the_last_device_signs_out_is_not_stored(client):
    """PATCH /auth/credentials waits on Moodle with no transaction open; a
    sign-out of the last device then drops the token, and the PATCH must not
    store it again for an account with no device left."""
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username=_STUDENT)
    )
    login = await client.post(
        "/v3/auth/login",
        json={
            "student_id": _STUDENT,
            "moodle_token": "tok-1",
            "device_info": {"client_device_id": "iphone-1", "platform": "ios"},
        },
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    class SignedOutWhileVerifying:
        async def verify(self, *, token, student_id):
            signed_out = await client.delete("/v3/devices/iphone-1", headers=headers)
            assert signed_out.status_code == 204
            return MoodleVerifyResult(ok=True, username=student_id)

    client.app.state.moodle_verifier = SignedOutWhileVerifying()
    response = await client.patch(
        "/v3/auth/credentials", headers=headers, json={"moodle_token": "tok-2"}
    )

    assert response.status_code == 200
    assert response.json() == {"updated": False}
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        assert await _stored(session) == 0


async def test_a_sign_out_and_a_sign_in_on_the_same_device_do_not_deadlock(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """The sign-out wrote the device row and then waited for the account,
    which the sign-in held while it waited for that device row."""
    user_id = await _student(db_session, ["iphone-1"])
    factory = build_session_factory(prepared_engine)
    reached, release = _hold_logins_before_the_device(monkeypatch)

    async with factory() as signing_in, factory() as signing_out:
        login = asyncio.create_task(_login(signing_in, test_settings, _STUDENT, "iphone-1"))
        await _until(prepared_engine, lambda: "iphone-1" in reached)
        sign_out = asyncio.create_task(
            delete_device(
                "iphone-1",
                CurrentAuth(user_id=user_id, session_id=0, device_id=None),
                signing_out,
            )
        )
        await _until(prepared_engine, sign_out.done)
        release.set()
        await login
        await signing_in.commit()
        await sign_out
        await signing_out.commit()

    assert await _stored(db_session) == 0


async def test_two_students_swapping_devices_do_not_deadlock(
    db_session, prepared_engine, test_settings, monkeypatch
):
    """Each sign-in held its own account and waited for the other's, whose
    device row it had just deleted."""
    await _student(db_session, ["phone-a"], student_id="b00000001")
    await _student(db_session, ["phone-b"], student_id="b00000002")
    factory = build_session_factory(prepared_engine)
    reached, release = _hold_logins_before_the_device(monkeypatch)

    async with factory() as first, factory() as second:
        on_b = asyncio.create_task(_login(first, test_settings, "b00000001", "phone-b"))
        await _until(prepared_engine, lambda: "phone-b" in reached)
        on_a = asyncio.create_task(_login(second, test_settings, "b00000002", "phone-a"))
        await _until(prepared_engine, lambda: "phone-a" in reached)
        release.set()
        await on_b
        await first.commit()
        await on_a
        await second.commit()

    owners = (
        await db_session.execute(
            select(UserDevice.client_device_id, User.student_id)
            .join(User, User.id == UserDevice.user_id)
            .order_by(UserDevice.client_device_id)
        )
    ).all()
    assert [tuple(o) for o in owners] == [("phone-a", "b00000002"), ("phone-b", "b00000001")]
    assert await _stored(db_session) == 2
