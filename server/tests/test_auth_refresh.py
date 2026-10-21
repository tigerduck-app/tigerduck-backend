"""End-to-end tests for POST /v3/auth/refresh rotation semantics."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import structlog
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from server.auth import service
from server.auth.models import AuthSession
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.auth.rate_limit import SlidingWindowLimiter
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


async def _session_count(client) -> int:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(AuthSession))


@pytest.fixture
def logs(monkeypatch):
    # The module's logger may be cached with the processors of an earlier
    # app, which capture_logs does not reach; a new one binds inside it.
    monkeypatch.setattr(service, "logger", structlog.get_logger(service.__name__))
    with capture_logs() as captured:
        yield captured


def _logged(logs, event: str, **fields) -> bool:
    return any(
        entry["event"] == event and fields.items() <= entry.items() for entry in logs
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

    # B lost to C when C was rotated: it is refused, and D keeps working.
    stale = await client.post("/v3/auth/refresh", json={"refresh_token": token_b})
    assert stale.status_code == 401
    assert stale.json()["detail"] == "session_revoked"
    assert (await _refresh(client, token_d)).status_code == 200


async def test_reuse_after_the_successor_was_rotated_is_refused(client) -> None:
    login = await do_login(client)
    token_a = login["refresh_token"]

    first = await client.post("/v3/auth/refresh", json={"refresh_token": token_a})
    token_b = first.json()["refresh_token"]
    second = await client.post("/v3/auth/refresh", json={"refresh_token": token_b})
    assert second.status_code == 200

    # B was redeemed, so the client did receive the rotation reply: A is
    # refused, and the pair B was rotated into keeps working.
    reuse = await client.post("/v3/auth/refresh", json={"refresh_token": token_a})
    assert reuse.status_code == 401
    assert reuse.json()["detail"] == "session_revoked"
    assert (await _refresh(client, second.json()["refresh_token"])).status_code == 200


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

async def test_a_token_sent_again_and_again_gets_a_pair_each_time(client) -> None:
    """An app that loses every reply sends the same token a third and a
    fourth time, the last ones days later. Each send gets a pair, all stay
    live while none is used, and once the app uses the newest the others no
    longer work."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    pairs = [(await _refresh(client, token_a)).json() for _ in range(2)]
    await _rotated_ago(client, token_a, timedelta(days=3))
    for _ in range(2):
        sent = await _refresh(client, token_a)
        assert sent.status_code == 200
        pairs.append(sent.json())
    assert len({pair["refresh_token"] for pair in pairs}) == 4
    for pair in pairs:
        assert (await _row(client, pair["refresh_token"])).revoked_at is None

    used = await client.get("/v3/devices", headers=_bearer(pairs[-1]))

    assert used.status_code == 200
    for pair in pairs[:-1]:
        row = await _row(client, pair["refresh_token"])
        assert row.revoked_reason == "superseded"
        refused = await client.get("/v3/devices", headers=_bearer(pair))
        assert refused.status_code == 401
    assert await _reuse_detections(client) == 0
    assert (await _refresh(client, pairs[-1]["refresh_token"])).status_code == 200


async def test_a_token_sent_on_and_on_keeps_its_sixteen_newest_pairs(
    client,
) -> None:
    """A copy or a broken client sending a token without end still gets a
    pair each time, but only the 16 newest unused ones stay live, and the
    use of any of them still supersedes the rest."""
    client.app.state.refresh_limiter = SlidingWindowLimiter(
        max_attempts=100, window_seconds=60
    )
    login = await do_login(client)
    pairs = []
    for _ in range(18):
        sent = await _refresh(client, login["refresh_token"])
        assert sent.status_code == 200
        pairs.append(sent.json())

    reasons = [
        (await _row(client, pair["refresh_token"])).revoked_reason for pair in pairs
    ]
    assert reasons == ["superseded"] * 2 + [None] * 16
    used = await client.get("/v3/devices", headers=_bearer(pairs[2]))
    assert used.status_code == 200
    reasons = [
        (await _row(client, pair["refresh_token"])).revoked_reason for pair in pairs
    ]
    assert reasons == ["superseded"] * 2 + [None] + ["superseded"] * 15


async def test_a_grace_retry_is_not_counted_as_reuse(client) -> None:
    """reuse_detected_at marks a refused reuse only; the retry rotates the
    token again instead, and the lost successor goes once the retry's pair is
    used."""
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
        timedelta(days=3),
        timedelta(days=89),
    ],
)
async def test_a_lost_reply_is_recovered_however_late(client, age) -> None:
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


async def test_reuse_after_the_successor_was_used_is_refused_and_revokes_nothing(
    client, logs
) -> None:
    """The successor's access token was accepted, so its client got the
    reply: the old token comes from a copy, or from a client that lost the
    pair it used. It is refused and logged, and the successor keeps
    working."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    rotated = await _refresh(client, token_a)
    token_b = rotated.json()["refresh_token"]
    used = await client.get("/v3/devices", headers=_bearer(rotated.json()))
    assert used.status_code == 200
    # Another session on the same device, which must stay alive.
    other = await do_login(client)
    await _rotated_ago(client, token_a, timedelta(hours=2))

    reuse = await _refresh(client, token_a)

    assert reuse.status_code == 401
    assert reuse.json()["detail"] == "session_revoked"
    assert (await _row(client, token_a)).reuse_detected_at is not None
    assert _logged(
        logs, "auth.refresh.reuse_detected", log_level="warning", cause="successor_used"
    )
    assert (await _row(client, token_b)).revoked_at is None
    again = await client.get("/v3/devices", headers=_bearer(rotated.json()))
    assert again.status_code == 200
    assert (await _refresh(client, token_b)).status_code == 200
    assert (await _refresh(client, other["refresh_token"])).status_code == 200


async def test_a_superseded_session_that_turns_up_is_refused_and_revokes_nothing(
    client, logs
) -> None:
    """The recovered pair was used, so a token of the successor it
    superseded comes from a second holder. It is refused and logged, and the
    pair that won keeps working."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    token_b = (await _refresh(client, token_a)).json()["refresh_token"]
    await _rotated_ago(client, token_a, timedelta(hours=2))
    recovered = await _refresh(client, token_a)
    used = await client.get("/v3/devices", headers=_bearer(recovered.json()))
    assert used.status_code == 200
    await _rotated_ago(client, token_b, timedelta(hours=1))

    stale = await _refresh(client, token_b)

    assert stale.status_code == 401
    assert stale.json()["detail"] == "session_revoked"
    assert _logged(
        logs, "auth.refresh.reuse_detected", log_level="warning", cause="superseded"
    )
    token_c = recovered.json()["refresh_token"]
    assert (await _row(client, token_c)).revoked_at is None
    assert (await _refresh(client, token_c)).status_code == 200


async def test_a_replaced_token_past_its_expiry_is_refused(client) -> None:
    """The token's own expiry is the one limit on rotating it again."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    token_b = (await _refresh(client, token_a)).json()["refresh_token"]
    now = datetime.now(UTC)
    await _set_row(
        client,
        token_a,
        issued_at=now - timedelta(days=91),
        expires_at=now - timedelta(days=1),
    )
    sessions = await _session_count(client)

    expired = await _refresh(client, token_a)

    assert expired.status_code == 401
    assert expired.json()["detail"] == "refresh_token_expired"
    assert await _session_count(client) == sessions
    assert (await _refresh(client, token_b)).status_code == 200


async def test_an_expired_session_among_its_rivals_does_not_hide_the_rest(
    client,
) -> None:
    """One of three pairs expires and is refused; the use of another still
    supersedes both of the others."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    pairs = [(await _refresh(client, token_a)).json() for _ in range(3)]
    now = datetime.now(UTC)
    await _set_row(
        client,
        pairs[0]["refresh_token"],
        issued_at=now - timedelta(days=91),
        expires_at=now - timedelta(days=1),
    )
    expired = await _refresh(client, pairs[0]["refresh_token"])
    assert expired.json()["detail"] == "refresh_token_expired"

    used = await client.get("/v3/devices", headers=_bearer(pairs[2]))

    assert used.status_code == 200
    reasons = [
        (await _row(client, pair["refresh_token"])).revoked_reason for pair in pairs
    ]
    assert reasons == ["superseded", "superseded", None]


async def test_a_refresh_of_one_of_three_pairs_supersedes_the_others(client) -> None:
    login = await do_login(client)
    token_a = login["refresh_token"]
    pairs = [(await _refresh(client, token_a)).json() for _ in range(3)]

    rotated = await _refresh(client, pairs[1]["refresh_token"])

    assert rotated.status_code == 200
    reasons = [
        (await _row(client, pair["refresh_token"])).revoked_reason for pair in pairs
    ]
    assert reasons == ["superseded", "rotated", "superseded"]
    for token in (token_a, pairs[0]["refresh_token"], pairs[2]["refresh_token"]):
        assert (await _refresh(client, token)).status_code == 401
    assert (await _refresh(client, rotated.json()["refresh_token"])).status_code == 200


async def _login_elsewhere(client) -> dict:
    """The same student on a second device, which can remove the first."""
    body = {
        **LOGIN_BODY,
        "device_info": {"client_device_id": "ipad-xyz", "platform": "ios"},
    }
    response = await client.post("/v3/auth/login", json=body)
    assert response.status_code == 200
    return response.json()


async def test_removing_the_device_ends_every_pair_its_token_was_rotated_into(
    client,
) -> None:
    """The device is removed from another one while three pairs of one
    token wait unused: the token and every pair are refused, and no session
    is made."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    pairs = [(await _refresh(client, token_a)).json() for _ in range(3)]
    other = await _login_elsewhere(client)
    device = LOGIN_BODY["device_info"]["client_device_id"]
    removed = await client.delete(f"/v3/devices/{device}", headers=_bearer(other))
    assert removed.status_code == 204
    sessions = await _session_count(client)

    for token in [token_a, *(pair["refresh_token"] for pair in pairs)]:
        refused = await _refresh(client, token)
        assert refused.status_code == 401
        assert refused.json()["detail"] == "session_revoked"
    for pair in pairs:
        used = await client.get("/v3/devices", headers=_bearer(pair))
        assert used.status_code == 401
    assert await _session_count(client) == sessions


async def test_after_a_logout_no_token_of_the_chain_gets_a_pair(client) -> None:
    """A logout still ends the session: the token it was rotated from and
    the pair it superseded are refused, and no session is made."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    lost = await _refresh(client, token_a)
    kept = await _refresh(client, token_a)
    logout = await client.post("/v3/auth/logout", headers=_bearer(kept.json()))
    assert logout.status_code == 204
    sessions = await _session_count(client)

    for token in (token_a, lost.json()["refresh_token"], kept.json()["refresh_token"]):
        refused = await _refresh(client, token)
        assert refused.status_code == 401
        assert refused.json()["detail"] == "session_revoked"
    assert await _session_count(client) == sessions


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
    assert response.json()["detail"] == "session_revoked"
    assert (await _row(client, token_b)).revoked_reason == "rotated"


@pytest.mark.parametrize("sends", [2, 3])
async def test_first_uses_of_the_pairs_at_once_let_one_win(client, sends) -> None:
    """Holders of the pairs a token's sends left use them at the same
    moment: one request supersedes every other session, and the others get
    a 401."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    pairs = [(await _refresh(client, token_a)).json() for _ in range(sends)]
    rows = [await _row(client, pair["refresh_token"]) for pair in pairs]

    engine = client.app.state.engine
    async with engine.connect() as holder:
        await holder.execute(
            text("SELECT 1 FROM auth_sessions WHERE id = :id FOR UPDATE"),
            {"id": min(row.id for row in rows)},
        )
        uses = [
            asyncio.create_task(client.get("/v3/devices", headers=_bearer(pair)))
            for pair in pairs
        ]
        await _until_queries_wait_for_a_lock(engine, count=sends)
        await holder.commit()
    statuses = sorted([(await use).status_code for use in uses])

    assert statuses == [200] + [401] * (sends - 1)
    rows = [await _row(client, pair["refresh_token"]) for pair in pairs]
    reasons = sorted(str(row.revoked_reason) for row in rows)
    assert reasons == ["None"] + ["superseded"] * (sends - 1)


async def test_concurrent_refreshes_with_one_token_all_get_a_pair(client) -> None:
    """Three sends of one token at once take turns: the first rotates it,
    the others rotate it again, and the app's use of any of the three pairs
    supersedes the other two."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    row_a = await _row(client, token_a)

    engine = client.app.state.engine
    async with engine.connect() as holder:
        await holder.execute(
            text("SELECT 1 FROM auth_sessions WHERE id = :id FOR UPDATE"),
            {"id": row_a.id},
        )
        sends = [asyncio.create_task(_refresh(client, token_a)) for _ in range(3)]
        await _until_queries_wait_for_a_lock(engine, count=3)
        await holder.commit()
    replies = [await send for send in sends]

    assert [reply.status_code for reply in replies] == [200] * 3
    pairs = [reply.json() for reply in replies]
    for pair in pairs:
        assert (await _row(client, pair["refresh_token"])).revoked_at is None
    used = await client.get("/v3/devices", headers=_bearer(pairs[1]))
    assert used.status_code == 200
    reasons = [
        (await _row(client, pair["refresh_token"])).revoked_reason for pair in pairs
    ]
    assert reasons == ["superseded", None, "superseded"]
    assert (await _refresh(client, pairs[1]["refresh_token"])).status_code == 200


async def test_a_rotation_in_flight_when_the_device_is_removed_is_revoked_too(
    client,
) -> None:
    """A rotation again holds its sessions' locks when the device is removed
    and commits its new session after the removal started revoking: that
    session is revoked as well."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    lost = await _refresh(client, token_a)
    other = await _login_elsewhere(client)
    row_a = await _row(client, token_a)
    row_b = await _row(client, lost.json()["refresh_token"])

    engine = client.app.state.engine
    async with engine.connect() as rotation:
        # What rotating A again writes while it holds the ring's locks.
        await rotation.execute(
            text(
                "SELECT 1 FROM auth_sessions WHERE id IN (:a, :b)"
                " ORDER BY id FOR UPDATE"
            ),
            {"a": row_a.id, "b": row_b.id},
        )
        new_id = await rotation.scalar(
            text(
                "INSERT INTO auth_sessions (user_id, device_id, refresh_token_hash,"
                " expires_at, replaced_by_session_id)"
                " VALUES (:user_id, :device_id, 'in-flight',"
                " now() + interval '90 days', :b) RETURNING id"
            ),
            {"user_id": row_a.user_id, "device_id": row_a.device_id, "b": row_b.id},
        )
        await rotation.execute(
            text(
                "UPDATE auth_sessions SET replaced_by_session_id = :new"
                " WHERE id IN (:a, :b)"
            ),
            {"new": new_id, "a": row_a.id, "b": row_b.id},
        )
        device = LOGIN_BODY["device_info"]["client_device_id"]
        removal = asyncio.create_task(
            client.delete(f"/v3/devices/{device}", headers=_bearer(other))
        )
        await _until_queries_wait_for_a_lock(engine)
        await rotation.commit()
    assert (await removal).status_code == 204

    factory = build_session_factory(engine)
    async with factory() as session:
        live = await session.scalar(
            select(func.count()).where(
                AuthSession.device_id == row_a.device_id,
                AuthSession.revoked_at.is_(None),
            )
        )
    assert live == 0


async def test_a_refresh_and_a_first_use_of_the_two_pairs_do_not_deadlock(client) -> None:
    """A refresh of the newer of the two sessions a second rotation left meets
    a first use of the older one, which locks the pair in id order. The
    refresh takes the pair in the same order, so the first use's second lock
    does not wait for it and neither request is aborted."""
    login = await do_login(client)
    token_a = login["refresh_token"]
    resend = await _refresh(client, token_a)
    late = await _refresh(client, token_a)
    row_b = await _row(client, resend.json()["refresh_token"])
    row_c = await _row(client, late.json()["refresh_token"])
    older, newer = sorted((row_b, row_c), key=lambda row: row.id)
    newer_token = (resend if newer.id == row_b.id else late).json()["refresh_token"]

    engine = client.app.state.engine
    async with engine.connect() as first_use:
        await first_use.execute(
            text("SELECT 1 FROM auth_sessions WHERE id = :id FOR UPDATE"),
            {"id": older.id},
        )
        refresh = asyncio.create_task(_refresh(client, newer_token))
        await _until_queries_wait_for_a_lock(engine)
        await first_use.execute(text("SET LOCAL lock_timeout = '3s'"))
        await first_use.execute(
            text("SELECT 1 FROM auth_sessions WHERE id = :id FOR UPDATE"),
            {"id": newer.id},
        )
        await first_use.commit()
    response = await refresh

    assert response.status_code == 200
    assert (await _row(client, newer_token)).revoked_reason == "rotated"


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
    """As releases before 3.3.6 revoked a session on a reuse of its chain."""
    login = await do_login(client)
    await _set_row(
        client,
        login["refresh_token"],
        revoked_at=datetime.now(UTC),
        revoked_reason="reuse_detected",
    )
    return login["refresh_token"]


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
