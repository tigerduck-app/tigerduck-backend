"""End-to-end tests for GET /v3/sync and GET /v3/sync/full."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.sync.changelog import append_change
from server.sync.models import (
    UserAssignment,
    UserAssignmentOverride,
    UserCourse,
    UserCourseOverride,
    UserSettingsDocument,
)
from server.sync.models import UserSyncState
from server.syncjobs.assignments import apply_fetched_assignments
from server.syncjobs.moodle_client import FetchedAssignment

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


def bearer(login: dict) -> dict:
    return {"Authorization": f"Bearer {login['access_token']}"}


async def seed_changes(client, user_id: str, n: int) -> list[int]:
    factory = build_session_factory(client.app.state.engine)
    revisions = []
    async with factory() as session:
        for i in range(n):
            revisions.append(
                await append_change(
                    session,
                    user_id=uuid.UUID(user_id),
                    entity_type="assignment",
                    entity_id=str(i),
                    operation="upsert",
                    payload={"fields": ["title"]},
                )
            )
        await session.commit()
    return revisions


async def test_sync_requires_auth(client) -> None:
    assert (await client.get("/v3/sync?since_revision=0")).status_code == 401


async def test_incremental_sync_envelope(client) -> None:
    login = await do_login(client)
    revisions = await seed_changes(client, login["user"]["id"], 3)

    response = await client.get(
        "/v3/sync?since_revision=0", headers=bearer(login)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["current_revision"] == revisions[-1]
    assert body["returned_until_revision"] == revisions[-1]
    assert body["has_more"] is False
    assert [c["revision"] for c in body["changes"]] == revisions
    assert body["changes"][0]["entity_type"] == "assignment"
    assert body["changes"][0]["operation"] == "upsert"
    assert body["changes"][0]["payload"] == {"fields": ["title"]}


async def test_incremental_sync_pagination(client) -> None:
    login = await do_login(client)
    revisions = await seed_changes(client, login["user"]["id"], 5)

    page1 = (
        await client.get(
            "/v3/sync?since_revision=0&limit=2", headers=bearer(login)
        )
    ).json()
    assert page1["has_more"] is True
    assert len(page1["changes"]) == 2

    page2 = (
        await client.get(
            f"/v3/sync?since_revision={page1['returned_until_revision']}&limit=10",
            headers=bearer(login),
        )
    ).json()
    assert page2["has_more"] is False
    collected = [c["revision"] for c in page1["changes"] + page2["changes"]]
    assert collected == revisions


async def test_expired_revision_410(client) -> None:
    login = await do_login(client)
    await seed_changes(client, login["user"]["id"], 2)

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        state = await session.get(
            UserSyncState, uuid.UUID(login["user"]["id"])
        )
        state.compacted_revision = state.current_revision
        await session.commit()
        compacted = state.compacted_revision

    response = await client.get(
        "/v3/sync?since_revision=0", headers=bearer(login)
    )
    assert response.status_code == 410
    body = response.json()
    assert body["error"] == "sync_revision_expired"
    assert body["min_available_revision"] == compacted
    assert body["full_sync_required"] is True


async def test_full_sync_snapshot(client) -> None:
    login = await do_login(client)
    user_id = uuid.UUID(login["user"]["id"])

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        session.add(
            UserCourse(
                user_id=user_id,
                semester="1141",
                course_key="CS3001301",
                course_no="CS3001301",
                course_name="作業系統",
            )
        )
        session.add(
            UserAssignment(
                user_id=user_id,
                moodle_course_id=101,
                moodle_assignment_id=9001,
                title="HW3",
            )
        )
        session.add(
            UserSettingsDocument(
                user_id=user_id,
                namespace="appearance",
                document={"theme": "dark"},
            )
        )
        revision = await append_change(
            session,
            user_id=user_id,
            entity_type="course",
            entity_id="1",
            operation="upsert",
        )
        await session.commit()

    response = await client.get("/v3/sync/full", headers=bearer(login))
    assert response.status_code == 200
    body = response.json()
    assert body["current_revision"] == revision
    assert len(body["courses"]) == 1
    assert body["courses"][0]["course_key"] == "CS3001301"
    assert len(body["assignments"]) == 1
    assert body["assignments"][0]["title"] == "HW3"
    assert len(body["settings_documents"]) == 1
    assert body["settings_documents"][0]["namespace"] == "appearance"
    assert body["settings_documents"][0]["document"] == {"theme": "dark"}
    # Empty sections are present (client treats full sync as authoritative).
    assert body["course_overrides"] == []
    # Bulletins are not part of TigerSync: subscriptions are per device.
    assert "bulletin_subscriptions" not in body
    assert "bulletin_states" not in body


async def test_full_sync_course_overrides_carry_the_ignore_setting(client) -> None:
    """A client keys an ignored course by its semester and course number,
    and sends the numeric Moodle id again when the server lacks it."""
    login = await do_login(client)
    user_id = uuid.UUID(login["user"]["id"])
    ignored_at = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)

    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        ignored = UserCourse(
            user_id=user_id,
            semester="1141",
            course_key="client:1141:CS3001301",
            course_no="CS3001301",
            course_name="作業系統",
            moodle_id="1141CS3001301",
        )
        coloured = UserCourse(
            user_id=user_id,
            semester="1132",
            course_key="client:1132:CS2001301",
            course_no="CS2001301",
            course_name="資料結構",
            moodle_id="1132CS2001301",
        )
        session.add_all([ignored, coloured])
        await session.flush()
        session.add_all(
            [
                UserCourseOverride(
                    user_id=user_id,
                    user_course_id=ignored.id,
                    ignore_assignments=True,
                    ignore_assignments_updated_at=ignored_at,
                    moodle_course_id=4242,
                ),
                UserCourseOverride(
                    user_id=user_id,
                    user_course_id=coloured.id,
                    color_hex="#FF8800",
                ),
            ]
        )
        await session.commit()

    response = await client.get("/v3/sync/full", headers=bearer(login))
    assert response.status_code == 200
    overrides = {o["course_no"]: o for o in response.json()["course_overrides"]}
    assert overrides["CS3001301"]["semester"] == "1141"
    assert overrides["CS3001301"]["moodle_id"] == "1141CS3001301"
    assert overrides["CS3001301"]["ignore_assignments"] is True
    assert (
        overrides["CS3001301"]["ignore_assignments_updated_at"]
        == ignored_at.isoformat()
    )
    assert overrides["CS3001301"]["moodle_course_id"] == 4242
    assert overrides["CS2001301"]["semester"] == "1132"
    assert overrides["CS2001301"]["ignore_assignments"] is False
    assert overrides["CS2001301"]["ignore_assignments_updated_at"] is None
    assert overrides["CS2001301"]["moodle_course_id"] is None


def _fetched(moodle_course_id: int, moodle_assignment_id: int) -> FetchedAssignment:
    return FetchedAssignment(
        moodle_course_id=moodle_course_id,
        moodle_assignment_id=moodle_assignment_id,
        course_name="資料結構",
        title=f"HW{moodle_assignment_id}",
        due_at=None,
        cutoff_at=None,
        allow_from_at=None,
        moodle_url=None,
        intro_html=None,
    )


async def keyed_marks(client, login) -> list[tuple[int | None, str]]:
    response = await client.get("/v3/sync/full", headers=bearer(login))
    assert response.status_code == 200
    return sorted(
        (
            (o["moodle_assignment_id"], o["local_status"])
            for o in response.json()["assignment_overrides"]
        ),
        key=lambda mark: (mark[0] is not None, mark[0] or 0, mark[1]),
    )


async def test_full_sync_keys_a_mark_on_a_row_the_fetch_soft_deleted(client) -> None:
    """The fetch leaves out a course the user is a TA in, so it soft-deletes
    that course's rows while the apps still show them. A mark without a
    Moodle id reads as cleared to both apps, which then drop it."""
    login = await do_login(client)
    user_id = uuid.UUID(login["user"]["id"])
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        student = UserAssignment(
            user_id=user_id, moodle_course_id=7001, moodle_assignment_id=1, title="HW1"
        )
        ta = UserAssignment(
            user_id=user_id, moodle_course_id=7002, moodle_assignment_id=2, title="HW2"
        )
        session.add_all([student, ta])
        await session.flush()
        session.add(
            UserAssignmentOverride(
                user_id=user_id,
                user_assignment_id=ta.id,
                local_status="ignored",
                local_status_updated_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
        )
        await session.commit()
    async with factory() as session:
        await apply_fetched_assignments(
            session,
            user_id=user_id,
            fetched=[_fetched(7001, 1)],
            now=datetime.now(UTC),
        )
        await session.commit()

    response = await client.get("/v3/sync/full", headers=bearer(login))
    assert [a["moodle_assignment_id"] for a in response.json()["assignments"]] == [1]
    assert await keyed_marks(client, login) == [(2, "ignored")]


async def test_full_sync_leaves_a_mark_unkeyed_beside_a_live_row(client) -> None:
    """A placeholder's mark stays behind when the fetch replaces the
    placeholder with the server's row. The user may have cleared the mark
    on that live row since, so the old one must not come back over it."""
    login = await do_login(client)
    user_id = uuid.UUID(login["user"]["id"])
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        placeholder = UserAssignment(
            user_id=user_id,
            moodle_course_id=0,
            moodle_assignment_id=5,
            title="HW5",
            deleted_at=datetime.now(UTC),
        )
        fetched = UserAssignment(
            user_id=user_id, moodle_course_id=7001, moodle_assignment_id=5, title="HW5"
        )
        session.add_all([placeholder, fetched])
        await session.flush()
        session.add(
            UserAssignmentOverride(
                user_id=user_id,
                user_assignment_id=placeholder.id,
                local_status="ignored",
                local_status_updated_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
        )
        await session.commit()

    assert await keyed_marks(client, login) == [(None, "ignored")]


async def test_full_sync_keys_only_the_newest_mark_of_a_soft_deleted_assignment(
    client,
) -> None:
    login = await do_login(client)
    user_id = uuid.UUID(login["user"]["id"])
    factory = build_session_factory(client.app.state.engine)
    gone = datetime.now(UTC)
    async with factory() as session:
        placeholder = UserAssignment(
            user_id=user_id,
            moodle_course_id=0,
            moodle_assignment_id=6,
            title="HW6",
            deleted_at=gone,
        )
        fetched = UserAssignment(
            user_id=user_id,
            moodle_course_id=7002,
            moodle_assignment_id=6,
            title="HW6",
            deleted_at=gone,
        )
        session.add_all([placeholder, fetched])
        await session.flush()
        session.add_all(
            [
                UserAssignmentOverride(
                    user_id=user_id,
                    user_assignment_id=fetched.id,
                    local_status="ignored",
                    local_status_updated_at=gone - timedelta(days=2),
                ),
                UserAssignmentOverride(
                    user_id=user_id,
                    user_assignment_id=placeholder.id,
                    local_status="locally_completed",
                    local_status_updated_at=gone - timedelta(days=1),
                ),
            ]
        )
        await session.commit()

    assert await keyed_marks(client, login) == [
        (None, "ignored"),
        (6, "locally_completed"),
    ]


async def upload_placeholders(client, login, *moodle_assignment_ids: int) -> None:
    response = await client.post(
        "/v3/sync/assignments/upload",
        headers=bearer(login),
        json={
            "assignments": [
                {
                    "moodle_assignment_id": moodle_assignment_id,
                    "course_no": "CS3001301",
                    "course_name": "資料結構",
                    "title": f"HW{moodle_assignment_id}",
                }
                for moodle_assignment_id in moodle_assignment_ids
            ]
        },
    )
    assert response.status_code == 200, response.text


async def mark(client, login, moodle_assignment_id: int, local_status: str) -> None:
    response = await client.patch(
        f"/v3/sync/assignments/{moodle_assignment_id}/override",
        headers=bearer(login),
        json={"local_status": local_status},
    )
    assert response.status_code == 200, response.text


async def fetch(client, login, *keys: tuple[int, int]) -> None:
    """A server fetch that returns these (course, assignment) keys and
    soft-deletes every other live row, placeholders included."""
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        await apply_fetched_assignments(
            session,
            user_id=uuid.UUID(login["user"]["id"]),
            fetched=[_fetched(*key) for key in keys],
            now=datetime.now(UTC),
        )
        await session.commit()


async def test_full_sync_keeps_a_placeholder_mark_unkeyed_once_the_server_row_outlived_it(
    client,
) -> None:
    """A mark made on a placeholder goes out unkeyed from the fetch that adds
    the server's row, so both apps drop it. A clear on the server's row used
    to leave it in place, which is HW5's state here: that old mark, and none
    on the server's row. When a later fetch soft-deletes the server's row
    too, as it does to a TA's course, that mark must not come back; a mark
    made on the server's row since, as for HW6, does."""
    login = await do_login(client)
    await upload_placeholders(client, login, 5, 6)
    await mark(client, login, 5, "ignored")
    await mark(client, login, 6, "ignored")
    await fetch(client, login, (7002, 5), (7002, 6))
    await mark(client, login, 6, "locally_completed")
    await fetch(client, login, (7001, 1))

    assert await keyed_marks(client, login) == [
        (None, "ignored"),
        (None, "ignored"),
        (6, "locally_completed"),
    ]


async def test_full_sync_keys_marks_made_after_the_server_row_went(client) -> None:
    """A guard on the rule above. Once the fetch has soft-deleted the server's
    rows of a TA's course, an app can upload a new placeholder: a mark made
    on it (HW7) is keyed once the next fetch soft-deletes it, and so is the
    server's row's own mark (HW8), which the placeholder outlived. A new
    assignment of that course never gets a server's row (HW9)."""
    login = await do_login(client)
    await fetch(client, login, (7002, 7), (7002, 8))
    await mark(client, login, 8, "locally_completed")
    await fetch(client, login, (7001, 1))
    await upload_placeholders(client, login, 7, 8, 9)
    await mark(client, login, 7, "ignored")
    await mark(client, login, 9, "ignored")
    await fetch(client, login, (7001, 1))

    assert await keyed_marks(client, login) == [
        (7, "ignored"),
        (8, "locally_completed"),
        (9, "ignored"),
    ]


async def test_full_sync_keys_a_mark_imported_after_the_server_row_went(
    client,
) -> None:
    """A guard on the rule above, which hides only a mark that went out
    unkeyed after it reached the server. A device marks the assignment
    offline after the fetch hid its placeholder, and sends the mark in its
    initial upload once the server's row went too: the upload puts it on
    the placeholder with its earlier time, and it is keyed."""
    login = await do_login(client)
    await upload_placeholders(client, login, 5)
    await fetch(client, login, (7002, 5))
    marked_offline = datetime.now(UTC)
    await fetch(client, login, (7001, 1))
    response = await client.post(
        "/v3/sync/initial-upload",
        headers=bearer(login),
        json={
            "assignment_overrides": [
                {
                    "moodle_course_id": 0,
                    "moodle_assignment_id": 5,
                    "local_status": "ignored",
                    "local_status_updated_at": marked_offline.isoformat(),
                }
            ]
        },
    )
    assert response.status_code == 200, response.text

    assert await keyed_marks(client, login) == [(5, "ignored")]


async def test_full_sync_lists_course_overrides_in_creation_order(client) -> None:
    """A course taken again has an override per term under one course
    number. The released apps keep the later row's name and Android 2.3.1
    the first row's colour, so the rows come in the order they were made,
    whatever their terms."""
    login = await do_login(client)
    user_id = uuid.UUID(login["user"]["id"])
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        courses = {
            semester: UserCourse(
                user_id=user_id,
                semester=semester,
                course_key=f"client:{semester}:CS3001301",
                course_no="CS3001301",
                course_name="作業系統",
                moodle_id=f"{semester}CS3001301",
            )
            for semester in ["1131", "1132", "1141", ""]
        }
        session.add_all(courses.values())
        await session.flush()
        for semester in ["1141", "1131", "", "1132"]:
            session.add(
                UserCourseOverride(
                    user_id=user_id,
                    user_course_id=courses[semester].id,
                    color_hex="#FF8800",
                )
            )
            await session.flush()
        await session.commit()

    response = await client.get("/v3/sync/full", headers=bearer(login))
    assert response.status_code == 200
    assert [o["semester"] for o in response.json()["course_overrides"]] == [
        "1141",
        "1131",
        "",
        "1132",
    ]
