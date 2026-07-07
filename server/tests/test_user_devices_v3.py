"""End-to-end tests for the /v3/devices endpoints."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from server.auth.models import AuthSession, DevicePushToken, UserDevice
from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory

pytestmark = pytest.mark.asyncio(loop_scope="session")


def login_body(student_id: str = "B11015000", device: str = "iphone-abc") -> dict:
    return {
        "student_id": student_id,
        "password": "pw",
        "moodle_token": "tok",
        "device_info": {"client_device_id": device, "platform": "ios"},
    }


PUSH_TOKEN = {
    "provider": "apns",
    "token_kind": "standard",
    "token_value": "a" * 64,
    "bundle_id": "org.ntust.app.TigerDuck",
    "environment": "production",
}


async def do_login(client, **kwargs) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="whatever")
    )
    response = await client.post("/v3/auth/login", json=login_body(**kwargs))
    assert response.status_code == 200
    return response.json()


def bearer(login: dict) -> dict:
    return {"Authorization": f"Bearer {login['access_token']}"}


async def test_register_device_with_push_token(client) -> None:
    login = await do_login(client)
    response = await client.post(
        "/v3/devices/register",
        headers=bearer(login),
        json={
            "client_device_id": "iphone-abc",
            "platform": "ios",
            "device_name": "iPhone 17",
            "push_token": PUSH_TOKEN,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["device_id"] == login["device_id"]
    assert body["push_token_id"] is not None

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        token = (
            await session.execute(select(DevicePushToken))
        ).scalar_one()
        assert token.token_hash == hashlib.sha256(b"a" * 64).hexdigest()
        assert token.status == "active"
        assert token.scope_key == ""


async def test_reregister_updates_token_not_duplicates(client) -> None:
    login = await do_login(client)
    payload = {
        "client_device_id": "iphone-abc",
        "platform": "ios",
        "push_token": PUSH_TOKEN,
    }
    first = await client.post(
        "/v3/devices/register", headers=bearer(login), json=payload
    )
    second = await client.post(
        "/v3/devices/register", headers=bearer(login), json=payload
    )
    assert second.status_code == 200
    assert first.json()["push_token_id"] == second.json()["push_token_id"]

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        tokens = (await session.execute(select(DevicePushToken))).scalars().all()
        assert len(tokens) == 1


async def test_token_moves_between_devices(client) -> None:
    # Same physical push token re-registered from a different device row
    # (e.g. app reinstall produced a new client_device_id): the active
    # token must move, not violate ux_push_token_active.
    login = await do_login(client)
    await client.post(
        "/v3/devices/register",
        headers=bearer(login),
        json={
            "client_device_id": "iphone-abc",
            "platform": "ios",
            "push_token": PUSH_TOKEN,
        },
    )
    relogin = await do_login(client, device="iphone-new")
    response = await client.post(
        "/v3/devices/register",
        headers=bearer(relogin),
        json={
            "client_device_id": "iphone-new",
            "platform": "ios",
            "push_token": PUSH_TOKEN,
        },
    )
    assert response.status_code == 200

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        token = (await session.execute(select(DevicePushToken))).scalar_one()
        device = (
            await session.execute(
                select(UserDevice).where(UserDevice.id == token.device_id)
            )
        ).scalar_one()
        assert device.client_device_id == "iphone-new"


async def test_list_devices_excludes_deleted(client) -> None:
    login = await do_login(client)
    await do_login(client, device="mac-xyz")

    # Both logins used the same user; list shows both devices.
    response = await client.get("/v3/devices", headers=bearer(login))
    assert response.status_code == 200
    ids = {d["client_device_id"] for d in response.json()["items"]}
    assert ids == {"iphone-abc", "mac-xyz"}


async def test_delete_device_revokes_sessions_and_tokens(client) -> None:
    login_phone = await do_login(client)
    login_mac = await do_login(client, device="mac-xyz")
    await client.post(
        "/v3/devices/register",
        headers=bearer(login_phone),
        json={
            "client_device_id": "iphone-abc",
            "platform": "ios",
            "push_token": PUSH_TOKEN,
        },
    )

    # Delete the phone from the mac. The path takes the client-owned
    # client_device_id, not the server row PK.
    response = await client.delete(
        "/v3/devices/iphone-abc", headers=bearer(login_mac)
    )
    assert response.status_code == 204

    # Phone's refresh token is dead; mac's still works.
    dead = await client.post(
        "/v3/auth/refresh", json={"refresh_token": login_phone["refresh_token"]}
    )
    assert dead.status_code == 401

    # Phone's access JWT is cut immediately as well.
    phone_access = await client.get("/v3/devices", headers=bearer(login_phone))
    assert phone_access.status_code == 401
    alive = await client.post(
        "/v3/auth/refresh", json={"refresh_token": login_mac["refresh_token"]}
    )
    assert alive.status_code == 200
    # Rotation revoked the mac's login session, so its original access JWT
    # is dead too — subsequent calls must use the refreshed access token.
    mac_headers = {"Authorization": f"Bearer {alive.json()['access_token']}"}

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        token = (await session.execute(select(DevicePushToken))).scalar_one()
        assert token.status == "invalidated"
        device = (
            await session.execute(
                select(UserDevice).where(
                    UserDevice.client_device_id == "iphone-abc"
                )
            )
        ).scalar_one()
        assert device.deleted_at is not None

    # And the deleted device no longer shows in the list.
    listing = await client.get("/v3/devices", headers=mac_headers)
    assert listing.status_code == 200
    ids = {d["client_device_id"] for d in listing.json()["items"]}
    assert ids == {"mac-xyz"}


async def test_delete_other_users_device_404(client) -> None:
    login_a = await do_login(client)
    login_b = await do_login(client, student_id="B11015999", device="other-dev")
    # User A's real client_device_id, called with B's auth: must 404
    # (device lookup is user-scoped).
    response = await client.delete(
        "/v3/devices/iphone-abc", headers=bearer(login_b)
    )
    assert response.status_code == 404

    # Victim's session unaffected.
    alive = await client.post(
        "/v3/auth/refresh", json={"refresh_token": login_a["refresh_token"]}
    )
    assert alive.status_code == 200


async def test_devices_require_auth(client) -> None:
    assert (await client.get("/v3/devices")).status_code == 401
    assert (
        await client.post(
            "/v3/devices/register",
            json={"client_device_id": "x", "platform": "ios"},
        )
    ).status_code == 401


async def test_session_revoked_when_own_device_deleted(client) -> None:
    # Deleting the device you're calling from logs that device out too.
    login = await do_login(client)
    response = await client.delete(
        "/v3/devices/iphone-abc", headers=bearer(login)
    )
    assert response.status_code == 204

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        rows = (await session.execute(select(AuthSession))).scalars().all()
        assert all(r.revoked_at is not None for r in rows)


# --- Phase 4c: linked-user marker on device_registrations (review 1.8) ---


async def _seed_anonymous_registration(client, device_id="iphone-abc") -> None:
    """The v2 register endpoint is retired (410); seed the pre-sunset
    anonymous row the marker logic reconciles directly instead."""
    from server.models import DeviceRegistration

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        session.add(
            DeviceRegistration(
                device_id=device_id,
                user_id="anon-1",
                pts_token_hex="a1b2c3" * 10,
                bundle_id="org.ntust.app.TigerDuck",
                attrs_type="TigerDuckActivityAttributes",
                apns_env="development",
            )
        )
        await session.commit()


async def _linked_user_id(client, device_id="iphone-abc"):
    from server.models import DeviceRegistration

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        row = await session.get(DeviceRegistration, device_id)
        return row.linked_user_id if row is not None else None


async def test_v3_register_links_anonymous_registration(client) -> None:
    await _seed_anonymous_registration(client)
    login = await do_login(client)
    response = await client.post(
        "/v3/devices/register",
        headers=bearer(login),
        json={"client_device_id": "iphone-abc", "platform": "ios"},
    )
    assert response.status_code == 200

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        device = (
            await session.execute(
                select(UserDevice).where(
                    UserDevice.client_device_id == "iphone-abc"
                )
            )
        ).scalar_one()
        assert await _linked_user_id(client) == device.user_id


async def test_v3_reregister_rederives_lost_marker(client) -> None:
    """The marker is rederivable, not one-shot: if linked_user_id is lost
    (e.g. manual DB surgery, or the anonymous row appearing after the v3
    one pre-sunset), the next v3 register restores it."""
    from server.models import DeviceRegistration
    from sqlalchemy import update as sa_update

    await _seed_anonymous_registration(client)
    login = await do_login(client)
    await client.post(
        "/v3/devices/register",
        headers=bearer(login),
        json={"client_device_id": "iphone-abc", "platform": "ios"},
    )
    assert await _linked_user_id(client) is not None

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        await session.execute(
            sa_update(DeviceRegistration)
            .where(DeviceRegistration.device_id == "iphone-abc")
            .values(linked_user_id=None)
        )
        await session.commit()

    # Re-registration (every app launch) rederives the marker.
    await client.post(
        "/v3/devices/register",
        headers=bearer(login),
        json={"client_device_id": "iphone-abc", "platform": "ios"},
    )
    assert await _linked_user_id(client) is not None


async def test_v3_device_delete_clears_marker(client) -> None:
    await _seed_anonymous_registration(client)
    login = await do_login(client)
    await client.post(
        "/v3/devices/register",
        headers=bearer(login),
        json={"client_device_id": "iphone-abc", "platform": "ios"},
    )
    assert await _linked_user_id(client) is not None

    response = await client.delete(
        "/v3/devices/iphone-abc", headers=bearer(login)
    )
    assert response.status_code == 204
    assert await _linked_user_id(client) is None


async def test_disable_sync_categories_cleans_up_and_logs_deletes(client) -> None:
    """Disabling the last device's sync categories must delete the orphaned
    server data AND record per-entity `delete` changelog entries — using only
    entity types / operations the DB check constraints allow."""
    from server.sync.models import (
        UserAssignment,
        UserChangeLog,
        UserCourse,
        UserSyncState,
    )

    login = await do_login(client)

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        device = (
            await session.execute(
                select(UserDevice).where(
                    UserDevice.client_device_id == "iphone-abc"
                )
            )
        ).scalar_one()
        user_id = device.user_id
        course = UserCourse(
            user_id=user_id,
            semester="1132",
            course_key="1132:CS101",
            course_no="CS101",
            course_name="Intro to CS",
        )
        assignment = UserAssignment(
            user_id=user_id,
            moodle_course_id=1,
            moodle_assignment_id=2,
            title="HW1",
        )
        session.add_all([course, assignment])
        await session.commit()
        course_id, assignment_id = course.id, assignment.id

    response = await client.patch(
        "/v3/devices/iphone-abc/preferences",
        headers=bearer(login),
        json={"sync_courses": False, "sync_assignments": False},
    )
    assert response.status_code == 200, response.text

    async with factory() as session:
        assert (await session.execute(select(UserCourse))).scalars().all() == []
        assert (
            await session.execute(select(UserAssignment))
        ).scalars().all() == []

        entries = (
            (
                await session.execute(
                    select(UserChangeLog).order_by(UserChangeLog.revision)
                )
            )
            .scalars()
            .all()
        )
        deletes = {(e.entity_type, e.entity_id, e.operation) for e in entries}
        assert ("course", str(course_id), "delete") in deletes
        assert ("assignment", str(assignment_id), "delete") in deletes

        # Revision watermark advanced over the cleanup entries so clients
        # polling /changes actually see them.
        state = (await session.execute(select(UserSyncState))).scalar_one()
        assert state.current_revision == max(e.revision for e in entries)
