"""The Moodle token dropped at the last sign-out, when a sign-out races
another sign-out or a sign-in of the same student, each in a transaction
of its own (tigerduck-backend#85)."""

from __future__ import annotations

import asyncio
import base64

import pytest
from sqlalchemy import func, select, update

from server.auth import service
from server.auth.crypto import CredentialCipher
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

pytestmark = pytest.mark.asyncio(loop_scope="session")

_STUDENT = "b11203058"
# Long enough for a statement that is not blocked to finish first.
_BLOCKED_FOR = 0.5


async def _student(session, devices: list[str]):
    user = User(student_id=_STUDENT)
    session.add(user)
    await session.flush()
    account = ExternalAccount(
        user_id=user.id, provider="ntust_sso", external_user_id=_STUDENT
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
    cipher = CredentialCipher(
        keys={"v1": base64.b64encode(b"0" * 32).decode()}, active_key_id="v1"
    )

    async with factory() as signing_in, factory() as signing_out:
        await service.login(
            signing_in,
            test_settings,
            StaticMoodleVerifier(MoodleVerifyResult(ok=True, username=_STUDENT)),
            SlidingWindowLimiter(max_attempts=10, window_seconds=60),
            cipher,
            student_id=_STUDENT,
            moodle_token="tok-2",
            moodle_private_token=None,
            device_info=DeviceInfo(client_device_id="ipad-1", platform="ipados"),
            client_ip="127.0.0.1",
        )
        await _soft_delete(signing_out, "iphone-1")
        await _forget_while_the_other_is_open(signing_in, signing_out, user_id)

    assert await _stored(db_session) == 1
