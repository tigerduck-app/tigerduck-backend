"""End-to-end tests for POST /v3/auth/refresh rotation semantics."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from server.auth.models import AuthSession
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.auth.tokens import hash_refresh_token
from server.db import build_session_factory

pytestmark = pytest.mark.asyncio(loop_scope="session")

LOGIN_BODY = {
    "student_id": "B11015000",
    "password": "pw",
    "moodle_token": "tok",
    "device_info": {"client_device_id": "iphone-abc", "platform": "ios"},
}


async def do_login(client) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="b11015000")
    )
    response = await client.post("/v3/auth/login", json=LOGIN_BODY)
    assert response.status_code == 200
    return response.json()


async def _refresh(client, refresh_token: str):
    return await client.post("/v3/auth/refresh", json={"refresh_token": refresh_token})


def _bearer(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _token_hash(client, refresh_token: str) -> str:
    return hash_refresh_token(
        client.app.state.settings.auth_refresh_hmac_key, refresh_token
    )


async def _row(client, refresh_token: str) -> AuthSession:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        return (
            await session.execute(
                select(AuthSession).where(
                    AuthSession.refresh_token_hash == _token_hash(client, refresh_token)
                )
            )
        ).scalar_one()


async def _set_row(client, refresh_token: str, **values) -> None:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.refresh_token_hash == _token_hash(client, refresh_token))
            .values(**values)
        )
        await session.commit()


async def _rotated_ago(client, refresh_token: str, age: timedelta) -> None:
    """As if the client came back with the rotated token `age` after its
    rotation."""
    await _set_row(client, refresh_token, revoked_at=datetime.now(UTC) - age)


async def _reuse_detections(client) -> int:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        return await session.scalar(
            select(func.count()).where(AuthSession.reuse_detected_at.is_not(None))
        )


async def test_normal_rotation_issues_new_tokens(client) -> None:
    login = await do_login(client)
    response = await client.post(
        "/v3/auth/refresh", json={"refresh_token": login["refresh_token"]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"] != login["refresh_token"]
    assert body["expires_in"] == 900

    # The new refresh token must itself rotate fine.
    second = await client.post(
        "/v3/auth/refresh", json={"refresh_token": body["refresh_token"]}
    )
    assert second.status_code == 200


async def test_unknown_refresh_token_401(client) -> None:
    response = await client.post(
        "/v3/auth/refresh", json={"refresh_token": "garbage"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_refresh_token"


async def test_retry_within_grace_returns_fresh_tokens(client) -> None:
    login = await do_login(client)
    token_a = login["refresh_token"]

    first = await client.post("/v3/auth/refresh", json={"refresh_token": token_a})
    assert first.status_code == 200
    token_b = first.json()["refresh_token"]

    # Simulated lost response: client retries with A within the grace
    # window — gets fresh tokens, no logout.
    retry = await client.post("/v3/auth/refresh", json={"refresh_token": token_a})
    assert retry.status_code == 200
    token_c = retry.json()["refresh_token"]
    assert token_c != token_b

    # C (the retry's token) works.
    ok = await client.post("/v3/auth/refresh", json={"refresh_token": token_c})
    assert ok.status_code == 200
    token_d = ok.json()["refresh_token"]

    # If B surfaces later, the "lost" response was actually intercepted —
    # that's theft: B is rejected AND the device's sessions are revoked.
    stale = await client.post("/v3/auth/refresh", json={"refresh_token": token_b})
    assert stale.status_code == 401

    dead = await client.post("/v3/auth/refresh", json={"refresh_token": token_d})
    assert dead.status_code == 401


async def test_reuse_after_the_recovery_window_revokes_family(client) -> None:
    login = await do_login(client)
    token_a = login["refresh_token"]

    first = await _refresh(client, token_a)
    assert first.status_code == 200
    token_b = first.json()["refresh_token"]
    await _rotated_ago(client, token_a, timedelta(hours=25))

    # B was never used, but a lost reply is not recovered this late: theft,
    # 401, and the whole family dies.
    reuse = await _refresh(client, token_a)
    assert reuse.status_code == 401
    assert reuse.json()["detail"] == "refresh_reuse_detected"

    dead = await _refresh(client, token_b)
    assert dead.status_code == 401

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        rows = (await session.execute(select(AuthSession))).scalars().all()
        assert all(r.revoked_at is not None for r in rows)
        assert any(r.reuse_detected_at is not None for r in rows)


async def test_grace_does_not_apply_if_successor_already_used(client) -> None:
    login = await do_login(client)
    token_a = login["refresh_token"]

    first = await client.post("/v3/auth/refresh", json={"refresh_token": token_a})
    token_b = first.json()["refresh_token"]
    second = await client.post("/v3/auth/refresh", json={"refresh_token": token_b})
    assert second.status_code == 200

    # B was redeemed, so the client DID receive the rotation response —
    # a later replay of A is theft even inside the grace window.
    reuse = await client.post("/v3/auth/refresh", json={"refresh_token": token_a})
    assert reuse.status_code == 401
    assert reuse.json()["detail"] == "refresh_reuse_detected"


async def test_expired_session_401(client) -> None:
    login = await do_login(client)
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        row = (
            await session.execute(
                select(AuthSession).where(AuthSession.revoked_at.is_(None))
            )
        ).scalar_one()
        row.issued_at = datetime.now(UTC) - timedelta(days=91)
        row.expires_at = datetime.now(UTC) - timedelta(days=1)
        await session.commit()

    response = await client.post(
        "/v3/auth/refresh", json={"refresh_token": login["refresh_token"]}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "refresh_token_expired"

async def test_grace_retry_is_one_shot(client) -> None:
    """Replaying token A a SECOND time within the grace window must be
    treated as theft, not another benign retry — otherwise an attacker
    holding A can mint unbounded live sessions for 60s."""
    login = await do_login(client)
    token_a = login["refresh_token"]

    first = await client.post("/v3/auth/refresh", json={"refresh_token": token_a})
    assert first.status_code == 200

    retry = await client.post("/v3/auth/refresh", json={"refresh_token": token_a})
    assert retry.status_code == 200  # the one benign grace retry
    token_c = retry.json()["refresh_token"]

    second_retry = await client.post(
        "/v3/auth/refresh", json={"refresh_token": token_a}
    )
    assert second_retry.status_code == 401
    assert second_retry.json()["detail"] == "refresh_reuse_detected"

    # Family revoked: the session minted by the first grace retry dies too.
    dead = await client.post("/v3/auth/refresh", json={"refresh_token": token_c})
    assert dead.status_code == 401


async def test_a_grace_retry_is_not_counted_as_reuse(client) -> None:
    """reuse_detected_at counts theft only; the retry spends the token's one
    second rotation instead, and the lost successor goes once the retry's
    pair is used."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    token_b = (await _refresh(client, token_a)).json()["refresh_token"]

    retry = await _refresh(client, token_a)

    assert retry.status_code == 200
    row_a = await _row(client, token_a)
    assert row_a.reuse_detected_at is None
    assert row_a.rerotated_at is not None
    used = await client.get("/v3/devices", headers=_bearer(retry.json()))
    assert used.status_code == 200
    assert (await _row(client, token_b)).revoked_reason == "superseded"


@pytest.mark.parametrize(
    "age",
    [
        # Android after a 502 or 504 that hid the rotation: it keeps the old
        # token and sends it again after a 60 s pause.
        timedelta(minutes=2),
        timedelta(hours=23),
    ],
)
async def test_a_lost_reply_is_recovered_within_the_window(client, age) -> None:
    """The client lost the rotation reply and came back with the old token
    later: it gets a fresh pair, and once it uses that pair only the
    successor it never received is revoked."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    lost = await _refresh(client, token_a)
    assert lost.status_code == 200
    token_b = lost.json()["refresh_token"]
    # Another session on the same device, which must stay alive.
    other = await do_login(client)
    await _rotated_ago(client, token_a, age)

    recovered = await _refresh(client, token_a)

    assert recovered.status_code == 200
    token_c = recovered.json()["refresh_token"]
    assert token_c not in (token_a, token_b)
    row_a = await _row(client, token_a)
    row_b = await _row(client, token_b)
    row_c = await _row(client, token_c)
    # Until one of the two is used, the client may hold either.
    assert row_b.revoked_at is None
    assert row_c.revoked_at is None
    assert row_b.replaced_by_session_id == row_c.id
    assert row_c.replaced_by_session_id == row_b.id
    assert row_a.replaced_by_session_id == row_c.id
    assert row_a.rerotated_at is not None
    assert row_a.reuse_detected_at is None

    used = await client.get("/v3/devices", headers=_bearer(recovered.json()))

    assert used.status_code == 200
    assert (await _row(client, token_b)).revoked_reason == "superseded"
    assert (await _row(client, token_c)).revoked_at is None
    assert (await _row(client, other["refresh_token"])).revoked_at is None
    assert await _reuse_detections(client) == 0
    assert (await _refresh(client, token_c)).status_code == 200


async def test_a_zero_recovery_window_turns_the_recovery_off(client) -> None:
    """MIGRATE.md offers this to switch the recovery off without a
    rollback; the grace retry stays."""
    client.app.state.settings.auth_refresh_recovery_window_seconds = 0
    try:
        login = await do_login(client)
        token_a = login["refresh_token"]
        await _refresh(client, token_a)
        assert (await _refresh(client, token_a)).status_code == 200

        second = await do_login(client)
        await _refresh(client, second["refresh_token"])
        await _rotated_ago(client, second["refresh_token"], timedelta(hours=2))
        reuse = await _refresh(client, second["refresh_token"])
    finally:
        client.app.state.settings.auth_refresh_recovery_window_seconds = 24 * 60 * 60

    assert reuse.status_code == 401
    assert reuse.json()["detail"] == "refresh_reuse_detected"


async def test_a_second_recovery_of_the_same_token_is_theft(client) -> None:
    login = await do_login(client)
    token_a = login["refresh_token"]
    await _refresh(client, token_a)
    await _rotated_ago(client, token_a, timedelta(hours=2))
    recovered = await _refresh(client, token_a)
    assert recovered.status_code == 200
    token_c = recovered.json()["refresh_token"]
    await _rotated_ago(client, token_a, timedelta(hours=1))

    again = await _refresh(client, token_a)

    assert again.status_code == 401
    assert again.json()["detail"] == "refresh_reuse_detected"
    assert (await _row(client, token_a)).reuse_detected_at is not None
    assert (await _row(client, token_c)).revoked_reason == "reuse_detected"


async def test_reuse_after_the_successor_was_used_is_theft(client) -> None:
    """The successor's access token was accepted, so its client got the
    reply: the old token comes from a copy."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    rotated = await _refresh(client, token_a)
    token_b = rotated.json()["refresh_token"]
    used = await client.get("/v3/devices", headers=_bearer(rotated.json()))
    assert used.status_code == 200
    await _rotated_ago(client, token_a, timedelta(hours=2))

    reuse = await _refresh(client, token_a)

    assert reuse.status_code == 401
    assert reuse.json()["detail"] == "refresh_reuse_detected"
    assert (await _row(client, token_b)).revoked_reason == "reuse_detected"


async def test_a_superseded_successor_that_turns_up_is_theft(client) -> None:
    """The recovered pair was used, so a token of the successor it
    superseded comes from a second holder. The sessions here have no
    device, so only the chain walk from that token reaches the session
    that won."""
    login = await do_login(client)
    await _set_row(client, login["refresh_token"], device_id=None)
    token_a = login["refresh_token"]
    token_b = (await _refresh(client, token_a)).json()["refresh_token"]
    await _rotated_ago(client, token_a, timedelta(hours=2))
    recovered = await _refresh(client, token_a)
    used = await client.get("/v3/devices", headers=_bearer(recovered.json()))
    assert used.status_code == 200

    stale = await _refresh(client, token_b)

    assert stale.status_code == 401
    assert stale.json()["detail"] == "refresh_reuse_detected"
    token_c = recovered.json()["refresh_token"]
    assert (await _row(client, token_c)).revoked_reason == "reuse_detected"


async def test_a_late_duplicate_after_the_successor_is_used_is_refused(
    client,
) -> None:
    """Android sends a refresh once more at once when the first send fails.
    A first send delayed in the network can arrive after the resend's
    reply, which the app saved and used: the late send must leave that
    pair alone."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    resend = await _refresh(client, token_a)
    token_b = resend.json()["refresh_token"]
    used = await client.get("/v3/devices", headers=_bearer(resend.json()))
    assert used.status_code == 200

    late = await _refresh(client, token_a)

    assert late.status_code == 401
    assert late.json()["detail"] == "session_revoked"
    assert (await _row(client, token_a)).rerotated_at is None
    assert await _reuse_detections(client) == 0
    again = await client.get("/v3/devices", headers=_bearer(resend.json()))
    assert again.status_code == 200
    assert (await _refresh(client, token_b)).status_code == 200


async def test_a_late_duplicate_before_the_successor_is_used_loses_to_it(
    client,
) -> None:
    """The same late send, arriving before the app used the resend's pair,
    gets a pair the app never reads; the app's first use of its own pair
    supersedes that one."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    resend = await _refresh(client, token_a)
    token_b = resend.json()["refresh_token"]
    late = await _refresh(client, token_a)
    assert late.status_code == 200

    used = await client.get("/v3/devices", headers=_bearer(resend.json()))

    assert used.status_code == 200
    unread = late.json()["refresh_token"]
    assert (await _row(client, unread)).revoked_reason == "superseded"
    assert (await _row(client, token_b)).revoked_at is None
    assert await _reuse_detections(client) == 0
    assert (await _refresh(client, token_b)).status_code == 200


async def test_the_pair_that_lost_turns_up_as_theft(client) -> None:
    """Once one of the two pairs is used, a token of the other comes from a
    second holder. Without a device, the chain walk from it has to reach
    the session that won."""
    login = await do_login(client)
    await _set_row(client, login["refresh_token"], device_id=None)
    token_a = login["refresh_token"]
    resend = await _refresh(client, token_a)
    late = await _refresh(client, token_a)
    used = await client.get("/v3/devices", headers=_bearer(resend.json()))
    assert used.status_code == 200

    stale = await _refresh(client, late.json()["refresh_token"])

    assert stale.status_code == 401
    assert stale.json()["detail"] == "refresh_reuse_detected"
    token_b = resend.json()["refresh_token"]
    assert (await _row(client, token_b)).revoked_reason == "reuse_detected"


async def test_a_token_the_previous_release_retried_is_not_rotated_again(
    client,
) -> None:
    """Before rerotated_at, a grace retry marked the token in
    reuse_detected_at; such a token has had its second rotation."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    await _refresh(client, token_a)
    await _set_row(client, token_a, reuse_detected_at=datetime.now(UTC))
    await _rotated_ago(client, token_a, timedelta(hours=2))

    reuse = await _refresh(client, token_a)

    assert reuse.status_code == 401
    assert reuse.json()["detail"] == "refresh_reuse_detected"


async def _until_queries_wait_for_a_lock(engine: AsyncEngine, count: int = 1) -> None:
    for _ in range(200):
        # A transaction of its own each time: Postgres keeps the
        # pg_stat_activity it read first until the transaction ends.
        async with engine.connect() as conn:
            waiting = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database()"
                    " AND wait_event_type = 'Lock'"
                )
            )
        if waiting >= count:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"fewer than {count} queries waited for a lock")


async def test_a_rotation_of_the_successor_in_flight_is_waited_for(client) -> None:
    """The successor's client rotates it while the old token comes back. The
    refresh waits for that rotation and finds a used successor, rather than
    superseding a session that has just been rotated."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    token_b = (await _refresh(client, token_a)).json()["refresh_token"]
    await _rotated_ago(client, token_a, timedelta(hours=2))
    row_b = await _row(client, token_b)

    engine = client.app.state.engine
    async with engine.connect() as rotation:
        await rotation.execute(
            update(AuthSession)
            .where(AuthSession.id == row_b.id)
            .values(
                revoked_at=func.now(),
                revoked_reason="rotated",
                first_used_at=func.now(),
            )
        )
        reuse = asyncio.create_task(_refresh(client, token_a))
        await _until_queries_wait_for_a_lock(engine)
        await rotation.commit()
    response = await reuse

    assert response.status_code == 401
    assert response.json()["detail"] == "refresh_reuse_detected"
    assert (await _row(client, token_b)).revoked_reason == "rotated"


async def test_two_first_uses_of_the_two_pairs_let_one_win(client) -> None:
    """A second holder uses the other pair at the same moment: one request
    supersedes the other session, and the other request gets a 401."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    resend = await _refresh(client, token_a)
    late = await _refresh(client, token_a)
    row_b = await _row(client, resend.json()["refresh_token"])
    row_c = await _row(client, late.json()["refresh_token"])

    engine = client.app.state.engine
    async with engine.connect() as holder:
        await holder.execute(
            text("SELECT 1 FROM auth_sessions WHERE id = :id FOR UPDATE"),
            {"id": min(row_b.id, row_c.id)},
        )
        uses = [
            asyncio.create_task(client.get("/v3/devices", headers=_bearer(pair)))
            for pair in (resend.json(), late.json())
        ]
        await _until_queries_wait_for_a_lock(engine, count=2)
        await holder.commit()
    statuses = sorted([(await use).status_code for use in uses])

    assert statuses == [200, 401]
    rows = [await _row(client, pair["refresh_token"]) for pair in (resend.json(), late.json())]
    assert sorted(str(row.revoked_reason) for row in rows) == ["None", "superseded"]


async def test_a_first_use_racing_the_second_rotation_still_wins(client) -> None:
    """The app's first request with the resend's pair reaches the row while
    a late duplicate is rotating again, and waits for it: once that commits,
    the first use still supersedes the duplicate's pair."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    resend = await _refresh(client, token_a)
    token_b = resend.json()["refresh_token"]
    row_b = await _row(client, token_b)

    engine = client.app.state.engine
    async with engine.connect() as blocker:
        await blocker.execute(
            text("SELECT 1 FROM auth_sessions WHERE id = :id FOR UPDATE"),
            {"id": row_b.id},
        )
        late = asyncio.create_task(_refresh(client, token_a))
        await _until_queries_wait_for_a_lock(engine)
        use = asyncio.create_task(
            client.get("/v3/devices", headers=_bearer(resend.json()))
        )
        await _until_queries_wait_for_a_lock(engine, count=2)
        await blocker.commit()
    late_reply = await late
    used = await use

    assert late_reply.status_code == 200
    assert used.status_code == 200
    unread = late_reply.json()["refresh_token"]
    assert (await _row(client, unread)).revoked_reason == "superseded"
    row_b = await _row(client, token_b)
    assert row_b.revoked_at is None
    assert row_b.replaced_by_session_id is None
    assert (await _refresh(client, token_b)).status_code == 200


async def _logged_out(client) -> str:
    login = await do_login(client)
    assert (await client.post("/v3/auth/logout", headers=_bearer(login))).status_code == 204
    return login["refresh_token"]


async def _removed_with_its_device(client) -> str:
    login = await do_login(client)
    device = LOGIN_BODY["device_info"]["client_device_id"]
    removed = await client.delete(f"/v3/devices/{device}", headers=_bearer(login))
    assert removed.status_code == 204
    return login["refresh_token"]


async def _expired(client) -> str:
    login = await do_login(client)
    now = datetime.now(UTC)
    await _set_row(
        client,
        login["refresh_token"],
        issued_at=now - timedelta(days=91),
        expires_at=now - timedelta(days=1),
    )
    expired = await _refresh(client, login["refresh_token"])
    assert expired.json()["detail"] == "refresh_token_expired"
    return login["refresh_token"]


async def _revoked_with_its_family(client) -> str:
    login = await do_login(client)
    token_a = login["refresh_token"]
    rotated = await _refresh(client, token_a)
    await client.get("/v3/devices", headers=_bearer(rotated.json()))
    await _rotated_ago(client, token_a, timedelta(hours=2))
    assert (await _refresh(client, token_a)).json()["detail"] == "refresh_reuse_detected"
    return rotated.json()["refresh_token"]


@pytest.mark.parametrize(
    "revoke",
    [_logged_out, _removed_with_its_device, _expired, _revoked_with_its_family],
)
async def test_a_token_revoked_for_another_reason_is_only_refused(
    client, revoke
) -> None:
    """Nothing is left to contain, and treating the token as stolen would
    revoke every session on the device, the one its relogin made too."""
    dead_token = await revoke(client)
    relogin = await do_login(client)
    detections = await _reuse_detections(client)

    refused = await _refresh(client, dead_token)

    assert refused.status_code == 401
    assert refused.json()["detail"] == "session_revoked"
    assert await _reuse_detections(client) == detections
    assert (await _refresh(client, relogin["refresh_token"])).status_code == 200
