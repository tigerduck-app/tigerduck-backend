"""A sign-in ends the older sessions of its device (tigerduck-backend#48)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import structlog
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from server.auth import service
from server.auth.models import (
    AuthSession,
    DevicePushToken,
    ExternalAccountCredential,
    User,
    UserDevice,
)
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.auth.tokens import hash_refresh_token
from server.db import build_session_factory
from server.models import DeviceRegistration

pytestmark = pytest.mark.asyncio(loop_scope="session")

PHONE = "iphone-abc"
STUDENT = "B11015000"


def _body(device: str = PHONE, student_id: str = STUDENT) -> dict:
    return {
        "student_id": student_id,
        "moodle_token": "tok",
        "device_info": {"client_device_id": device, "platform": "ios"},
    }


def _allow_moodle(client, error: str | None = None) -> None:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=error is None, username="whatever", error=error)
    )


async def _sign_in(client, device: str = PHONE, student_id: str = STUDENT) -> dict:
    _allow_moodle(client)
    response = await client.post("/v3/auth/login", json=_body(device, student_id))
    assert response.status_code == 200, response.text
    return response.json()


async def _refresh(client, refresh_token: str):
    return await client.post("/v3/auth/refresh", json={"refresh_token": refresh_token})


def _bearer(pair: dict) -> dict:
    return {"Authorization": f"Bearer {pair['access_token']}"}


def _factory(client):
    return build_session_factory(client.app.state.engine)


def _hash(client, refresh_token: str) -> str:
    return hash_refresh_token(client.app.state.settings.auth_refresh_hmac_key, refresh_token)


async def _row(client, refresh_token: str) -> AuthSession:
    async with _factory(client)() as session:
        return (
            await session.execute(
                select(AuthSession).where(
                    AuthSession.refresh_token_hash == _hash(client, refresh_token)
                )
            )
        ).scalar_one()


async def _set_row(client, refresh_token: str, **values) -> None:
    async with _factory(client)() as session:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.refresh_token_hash == _hash(client, refresh_token))
            .values(**values)
        )
        await session.commit()


async def _live(client, device: str = PHONE) -> list[int]:
    """The ids of the live sessions of `device`, under any account."""
    async with _factory(client)() as session:
        return sorted(
            (
                await session.execute(
                    select(AuthSession.id)
                    .join(UserDevice, UserDevice.id == AuthSession.device_id)
                    .where(
                        UserDevice.client_device_id == device,
                        AuthSession.revoked_at.is_(None),
                    )
                )
            ).scalars()
        )


async def _count(client, *where) -> int:
    async with _factory(client)() as session:
        return await session.scalar(select(func.count()).select_from(AuthSession).where(*where))


def _refused(response) -> bool:
    return (response.status_code, response.json().get("detail")) == (401, "session_revoked")


def _anonymous(device: str, user_id: str) -> DeviceRegistration:
    return DeviceRegistration(
        device_id=device,
        user_id=user_id,
        pts_token_hex="a1" * 32,
        bundle_id="org.ntust.app.TigerDuck",
        attrs_type="TigerDuckActivityAttributes",
        apns_env="development",
    )


async def _until_queries_wait_for_a_lock(engine: AsyncEngine, count: int = 1) -> None:
    for _ in range(200):
        async with engine.connect() as conn:
            waiting = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        if waiting >= count:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"fewer than {count} queries waited for a lock")


@pytest.fixture
def logs(monkeypatch):
    monkeypatch.setattr(service, "logger", structlog.get_logger(service.__name__))
    with capture_logs() as captured:
        yield captured


async def test_a_sign_in_ends_the_older_sessions_of_its_device(client, logs) -> None:
    """The first pair and the two a resent token left all end; only the new
    sign-in's pair works."""
    first = await _sign_in(client)
    pairs = [(await _refresh(client, first["refresh_token"])).json() for _ in range(2)]
    ended = sorted([(await _row(client, pair["refresh_token"])).id for pair in pairs])

    second = await _sign_in(client)

    for old in (first, *pairs):
        assert _refused(await client.get("/v3/devices", headers=_bearer(old)))
        assert _refused(await _refresh(client, old["refresh_token"]))
    assert await _live(client) == [(await _row(client, second["refresh_token"])).id]
    assert (await client.get("/v3/devices", headers=_bearer(second))).status_code == 200
    assert any(
        entry["event"] == "auth.login.success" and entry["ended_session_ids"] == ended
        for entry in logs
    )


async def test_an_ended_token_sent_again_marks_no_reuse_and_gets_no_pair(client, logs) -> None:
    """A refresh of an ended session takes the logged-out path however often
    it comes: no pair, no reuse mark, and the new pair keeps working."""
    first = await _sign_in(client)
    second = await _sign_in(client)
    sessions = await _count(client)

    for _ in range(3):
        assert _refused(await _refresh(client, first["refresh_token"]))

    assert await _count(client) == sessions
    assert await _count(client, AuthSession.reuse_detected_at.is_not(None)) == 0
    assert any(
        entry["event"] == "auth.refresh.revoked_token" and entry["reason"] == "logout"
        for entry in logs
    )
    assert (await client.get("/v3/devices", headers=_bearer(second))).status_code == 200
    assert (await _refresh(client, second["refresh_token"])).status_code == 200


async def test_a_token_whose_rotation_reply_was_lost_before_the_sign_in_gets_no_pair(
    client,
) -> None:
    """The app lost the reply to a refresh, then signed in: the lost pair
    ended with the sign-in, so the old token is not rotated again."""
    first = await _sign_in(client)
    assert (await _refresh(client, first["refresh_token"])).status_code == 200
    await _set_row(
        client, first["refresh_token"], revoked_at=datetime.now(UTC) - timedelta(hours=1)
    )
    second = await _sign_in(client)
    sessions = await _count(client)

    assert _refused(await _refresh(client, first["refresh_token"]))

    assert await _count(client) == sessions
    assert await _live(client) == [(await _row(client, second["refresh_token"])).id]
    assert (await _refresh(client, second["refresh_token"])).status_code == 200


async def test_a_sign_in_keeps_the_device_registered(client) -> None:
    """Ending sessions is not a deregister: the device's push and Live
    Activity tokens, its anonymous registration and the Moodle token stay."""
    first = await _sign_in(client)
    async with _factory(client)() as session:
        session.add(_anonymous(PHONE, "anon-1"))
        await session.commit()
    for kind, scope in [
        ("standard", ""),
        ("push_to_start", "TigerDuckActivityAttributes"),
        ("live_activity_update", "classPreparing::A1"),
    ]:
        token = {"provider": "apns", "token_kind": kind, "token_value": kind * 4, "scope_key": scope}
        registered = await client.post(
            "/v3/devices/register",
            headers=_bearer(first),
            json={"client_device_id": PHONE, "platform": "ios", "push_token": token},
        )
        assert registered.status_code == 200, registered.text

    second = await _sign_in(client)

    async with _factory(client)() as session:
        statuses = (await session.execute(select(DevicePushToken.status))).scalars().all()
        assert sorted(statuses) == ["active"] * 3
        device = (await session.execute(select(UserDevice))).scalar_one()
        assert device.deleted_at is None
        registration = await session.get(DeviceRegistration, PHONE)
        assert registration is not None
        assert str(registration.linked_user_id) == second["user"]["id"]
        assert await session.scalar(select(func.count()).select_from(ExternalAccountCredential)) == 1


async def test_a_sign_in_leaves_other_devices_and_deviceless_sessions_alone(client) -> None:
    """The same student's iPad, another student, an anonymous device and a
    session stored without a device keep what they have."""
    phone = await _sign_in(client)
    ipad = await _sign_in(client, device="ipad-xyz")
    other = await _sign_in(client, device="pixel-1", student_id="B11015999")
    await _set_row(client, phone["refresh_token"], device_id=None)
    async with _factory(client)() as session:
        session.add(_anonymous("anon-phone", "anon-2"))
        await session.commit()

    await _sign_in(client)

    for pair in (phone, ipad, other):
        assert (await _row(client, pair["refresh_token"])).revoked_at is None
    async with _factory(client)() as session:
        anonymous = await session.get(DeviceRegistration, "anon-phone")
        assert anonymous is not None and anonymous.linked_user_id is None


async def test_another_student_signing_in_on_the_phone_ends_none_of_the_first_ones_sessions(
    client,
) -> None:
    """Another account's sessions are that student's, even on one phone, so
    two phones that share an id cannot sign each other's students out."""
    first = await _sign_in(client)

    await _sign_in(client, student_id="B11015999")

    assert (await _row(client, first["refresh_token"])).revoked_at is None


async def _exhaust_the_login_limit(client) -> None:
    for _ in range(client.app.state.settings.auth_login_max_attempts):
        client.app.state.login_limiter.record(f"login:sid:{STUDENT.lower()}")


async def _suspend(client) -> None:
    async with _factory(client)() as session:
        await session.execute(update(User).values(status="suspended"))
        await session.commit()


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        ("invalid_token", 401),
        ("unreachable", 503),
        ("tokenless", 400),
        ("rate_limited", 429),
        ("suspended", 403),
    ],
)
async def test_a_failed_sign_in_ends_nothing(client, failure, status) -> None:
    first = await _sign_in(client)
    body = _body()
    if failure in ("invalid_token", "unreachable"):
        _allow_moodle(client, error=failure)
    elif failure == "tokenless":
        body["moodle_token"] = None
    elif failure == "rate_limited":
        await _exhaust_the_login_limit(client)
    else:
        await _suspend(client)

    response = await client.post("/v3/auth/login", json=body)

    assert response.status_code == status
    assert (await _row(client, first["refresh_token"])).revoked_at is None


async def test_a_sign_in_that_fails_after_ending_them_ends_nothing(client, monkeypatch) -> None:
    """The sessions end in the sign-in's transaction, which a later error
    rolls back."""
    first = await _sign_in(client)

    def unsigned(*args, **kwargs):
        raise RuntimeError("no access token")

    monkeypatch.setattr(service, "issue_access_token", unsigned)
    with pytest.raises(RuntimeError):
        await client.post("/v3/auth/login", json=_body())

    assert (await _row(client, first["refresh_token"])).revoked_at is None


async def test_two_sign_ins_at_once_leave_one_live_session(client) -> None:
    first = await _sign_in(client)
    engine = client.app.state.engine
    async with engine.connect() as holder:
        await holder.execute(text("SELECT 1 FROM external_accounts FOR UPDATE"))
        sign_ins = [
            asyncio.create_task(client.post("/v3/auth/login", json=_body())) for _ in range(2)
        ]
        # One waits for the account, the other for the device's sign-in lock.
        await _until_queries_wait_for_a_lock(engine, count=2)
        await holder.commit()
    replies = [await sign_in for sign_in in sign_ins]

    assert [reply.status_code for reply in replies] == [200, 200]
    assert len(await _live(client)) == 1
    statuses = [(await _refresh(client, r.json()["refresh_token"])).status_code for r in replies]
    assert sorted(statuses) == [200, 401]
    assert _refused(await _refresh(client, first["refresh_token"]))


async def test_a_refresh_in_flight_when_the_device_signs_in_is_ended_too(client) -> None:
    """A refresh holds the session's lock when the sign-in comes and commits
    its new session after the sign-in started waiting: that session ends as
    well, leaving the sign-in's alone."""
    first = await _sign_in(client)
    row = await _row(client, first["refresh_token"])
    engine = client.app.state.engine
    async with engine.connect() as rotation:
        # What a rotation of the session writes while it holds its lock.
        await rotation.execute(
            text("SELECT 1 FROM auth_sessions WHERE id = :id FOR UPDATE"), {"id": row.id}
        )
        new_id = await rotation.scalar(
            text(
                "INSERT INTO auth_sessions (user_id, device_id, refresh_token_hash, expires_at)"
                " VALUES (:user_id, :device_id, 'in-flight', now() + interval '90 days')"
                " RETURNING id"
            ),
            {"user_id": row.user_id, "device_id": row.device_id},
        )
        await rotation.execute(
            text(
                "UPDATE auth_sessions SET revoked_at = now(), revoked_reason = 'rotated',"
                " replaced_by_session_id = :new, first_used_at = now() WHERE id = :id"
            ),
            {"new": new_id, "id": row.id},
        )
        sign_in = asyncio.create_task(client.post("/v3/auth/login", json=_body()))
        await _until_queries_wait_for_a_lock(engine)
        await rotation.commit()
    second = await sign_in

    assert second.status_code == 200
    assert await _live(client) == [(await _row(client, second.json()["refresh_token"])).id]


async def test_a_refresh_of_the_older_token_during_the_sign_in_is_refused(client) -> None:
    """The refresh waits for the sign-in that holds the session, then gets
    the logged-out answer rather than a pair the sign-in would not end."""
    first = await _sign_in(client)
    user_id = (await _row(client, first["refresh_token"])).user_id
    engine = client.app.state.engine
    async with engine.connect() as holder:
        # The sign-in's last write is the user's sign-in time.
        await holder.execute(
            text("SELECT 1 FROM users WHERE id = :id FOR NO KEY UPDATE"), {"id": user_id}
        )
        sign_in = asyncio.create_task(client.post("/v3/auth/login", json=_body()))
        await _until_queries_wait_for_a_lock(engine)
        refresh = asyncio.create_task(_refresh(client, first["refresh_token"]))
        await _until_queries_wait_for_a_lock(engine, count=2)
        await holder.commit()
    second = await sign_in
    refused = await refresh

    assert second.status_code == 200
    assert _refused(refused)
    assert await _live(client) == [(await _row(client, second.json()["refresh_token"])).id]


async def test_the_sign_in_locks_the_sessions_in_id_order(client) -> None:
    """A refresh or a first use locks sessions in id order (`lock_contest`).
    A sign-in waiting for a lower one must not hold a higher one, or the two
    would deadlock."""
    first = await _sign_in(client)
    lower, higher = [(await _refresh(client, first["refresh_token"])).json() for _ in range(2)]
    row_lower = await _row(client, lower["refresh_token"])
    row_higher = await _row(client, higher["refresh_token"])
    assert row_lower.id < row_higher.id
    # A new token hash moves the lower row after the higher one in the table
    # and in every index, so a scan meets the higher one first.
    await _set_row(client, lower["refresh_token"], refresh_token_hash="moved")

    engine = client.app.state.engine
    async with engine.connect() as holder, engine.connect() as probe:
        await holder.execute(
            text("SELECT 1 FROM auth_sessions WHERE id = :id FOR UPDATE"), {"id": row_lower.id}
        )
        sign_in = asyncio.create_task(client.post("/v3/auth/login", json=_body()))
        await _until_queries_wait_for_a_lock(engine)
        # NOWAIT fails at once where the sign-in holds the row.
        await probe.execute(
            text("SELECT 1 FROM auth_sessions WHERE id = :id FOR UPDATE NOWAIT"),
            {"id": row_higher.id},
        )
        await probe.rollback()
        await holder.commit()
    second = await sign_in

    assert second.status_code == 200
    assert await _live(client) == [(await _row(client, second.json()["refresh_token"])).id]
