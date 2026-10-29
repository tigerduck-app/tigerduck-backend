"""The portal's Status numbers, user list and user page, run against the
server's tables. In the server suite for the reason
test_portal_sync_job_retry.py gives."""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.db import _asyncpg_dsn  # noqa: E402
from portal.app.routes import users as users_route  # noqa: E402
from portal.app.routes.users import get_user, list_users, user_stats  # noqa: E402
from server.academic_calendar.models import SemesterTerm  # noqa: E402
from server.auth.models import (  # noqa: E402
    AuthSession,
    DevicePushToken,
    ExternalAccount,
    PushDelivery,
    PushJob,
    User,
    UserDevice,
)
from server.bulletins.models import Bulletin, BulletinDispatch  # noqa: E402
from server.models import DeviceRegistration  # noqa: E402
from server.sync.models import (  # noqa: E402
    UserChangeLog,
    UserCourse,
    UserCourseSkippedDate,
    UserSettingsDocument,
)
from server.syncjobs.models import SyncJob  # noqa: E402

pytestmark = pytest.mark.asyncio(loop_scope="session")

_REQUEST = SimpleNamespace(
    state=SimpleNamespace(actor="ops@example.com"),
    url=SimpleNamespace(path="/api/users"),
)
_TAIPEI = ZoneInfo("Asia/Taipei")


@pytest_asyncio.fixture(loop_scope="session")
async def portal_pool(test_settings, db_session):
    pool = await asyncpg.create_pool(
        _asyncpg_dsn(test_settings.database_url), min_size=1, max_size=2
    )
    yield pool
    await pool.close()


def _today() -> date:
    return datetime.now(_TAIPEI).date()


async def _user(db_session, student_id: str, *, account: str | None = "active", **fields) -> User:
    user = User(student_id=student_id, **fields)
    db_session.add(user)
    await db_session.flush()
    if account is not None:
        db_session.add(
            ExternalAccount(
                user_id=user.id,
                provider="ntust_sso",
                external_user_id=student_id,
                credential_status=account,
            )
        )
    return user


def _session(user: User, issued_at: datetime) -> AuthSession:
    return AuthSession(
        user_id=user.id,
        refresh_token_hash=f"{user.id}-{issued_at.isoformat()}",
        issued_at=issued_at,
        expires_at=issued_at + timedelta(days=30),
    )


async def _sync_job(db_session, user: User, status: str) -> None:
    account_id = await db_session.scalar(
        select(ExternalAccount.id).where(ExternalAccount.user_id == user.id)
    )
    db_session.add(
        SyncJob(user_id=user.id, external_account_id=account_id, job_type="moodle_assignments", status=status)
    )


def _job(user: User, channel: str, *, status: str = "sent", **fields) -> PushJob:
    now = datetime.now(UTC)
    return PushJob(
        user_id=user.id,
        dedupe_key=f"{channel}-{now.timestamp()}-{status}-{fields.get('payload')}",
        channel=channel,
        scenario=channel,
        fire_at=now,
        payload=fields.pop("payload", {}),
        status=status,
        **fields,
    )


def _delivery(job: PushJob, status: str, *, device: UserDevice | None = None) -> PushDelivery:
    return PushDelivery(
        push_job_id=job.id,
        user_id=job.user_id,
        device_id=device.id if device else None,
        provider="apns",
        token_kind="standard",
        token_hash=f"{job.id}-{status}",
        status=status,
        sent_at=datetime.now(UTC) if status == "sent" else None,
    )


async def test_daily_active_users_count_session_renewals_by_taipei_day(
    db_session, portal_pool
):
    now = datetime.now(UTC)
    active = await _user(db_session, "a1")
    earlier = await _user(db_session, "a2")
    gone = await _user(db_session, "a3", deleted_at=now)
    db_session.add_all(
        [
            _session(active, now),
            _session(active, now - timedelta(minutes=5)),
            _session(earlier, now - timedelta(days=10)),
            _session(gone, now),
        ]
    )
    await db_session.commit()

    stats = await user_stats(pool=portal_pool)

    daily = stats["active"]["daily"]
    assert len(daily) == 90
    assert daily[-1] == {"day": _today(), "users": 1}
    ten_days_ago = (now - timedelta(days=10)).astimezone(_TAIPEI).date()
    assert {"day": ten_days_ago, "users": 1} in daily
    assert stats["active"]["last_7d"] == 1
    assert stats["users"]["total"] == 2
    assert stats["users"]["new_7d"] == 2
    weekly = stats["users"]["weekly"]
    assert len(weekly) == 12
    assert weekly[-1]["users"] == 2
    assert weekly[-1]["week"].isoweekday() == 1


async def test_school_accounts_split_signed_in_needs_sign_in_and_failing(
    db_session, portal_pool
):
    await _user(db_session, "s1")
    await _user(db_session, "s2", account="invalid")
    failing = await _user(db_session, "s3")
    await _sync_job(db_session, failing, "failed")
    await _user(db_session, "s4", account=None)
    await db_session.commit()

    stats = await user_stats(pool=portal_pool)

    assert stats["accounts"] == {"signed_in": 1, "needs_sign_in": 1, "failing": 1}


async def test_devices_by_version_feature_share_and_daily_count(db_session, portal_pool):
    user = await _user(db_session, "d1")
    off = await _user(db_session, "d2")
    now = datetime.now(UTC)
    db_session.add_all(
        [
            UserDevice(user_id=user.id, client_device_id="p1", platform="ios", app_version="2.8.2"),
            UserDevice(
                user_id=off.id,
                client_device_id="p2",
                platform="android",
                app_version="2.4.0",
                bulletin_push_enabled=False,
                cloud_sync_enabled=False,
            ),
            UserDevice(
                user_id=user.id,
                client_device_id="p3",
                platform="ios",
                app_version="2.8.1",
                created_at=now - timedelta(days=5),
                deleted_at=now - timedelta(days=2),
            ),
            UserSettingsDocument(
                user_id=off.id, namespace="notification", document={"courses": {"enabled": False}}
            ),
            DeviceRegistration(
                device_id="anon-1",
                user_id="anon-1",
                pts_token_hex="",
                bundle_id="",
                attrs_type="",
                apns_env="",
            ),
        ]
    )
    await db_session.commit()

    stats = await user_stats(pool=portal_pool)

    devices = stats["devices"]
    assert devices["versions"] == [
        {"platform": "android", "app_version": "2.4.0", "devices": 1},
        {"platform": "ios", "app_version": "2.8.2", "devices": 1},
    ]
    assert devices["features"] == {
        "class_reminders": 1,
        "bulletin_push": 1,
        "live_activity": 2,
        "assignment_reminders": 2,
        "cloud_sync": 1,
        "course_name_sync": 2,
    }
    daily = devices["daily"]
    assert len(daily) == 20
    assert [d["devices"] for d in daily[-7:]] == [0, 1, 1, 1, 0, 0, 2]
    assert stats["anonymous_devices"] == 1


async def test_pushes_count_sent_and_failed_by_channel(db_session, portal_pool):
    user = await _user(db_session, "p1")
    course = _job(user, "course")
    bulletin = _job(user, "bulletin")
    old = _job(user, "course")
    db_session.add_all([course, bulletin, old])
    await db_session.flush()
    db_session.add_all(
        [
            _delivery(course, "sent"),
            _delivery(course, "failed"),
            _delivery(bulletin, "sent"),
            _delivery(bulletin, "skipped"),
        ]
    )
    stale = _delivery(old, "sent")
    stale.sent_at = datetime.now(UTC) - timedelta(days=2)
    db_session.add(stale)
    registration = DeviceRegistration(
        device_id="anon-2", user_id="anon-2", pts_token_hex="", bundle_id="", attrs_type="", apns_env=""
    )
    post = Bulletin(external_id="x1", source_url="https://example.com/x1", title="t")
    db_session.add_all([registration, post])
    await db_session.flush()
    db_session.add(
        BulletinDispatch(
            bulletin_id=post.id, device_id="anon-2", status="sent", sent_at=datetime.now(UTC)
        )
    )
    await db_session.commit()

    stats = await user_stats(pool=portal_pool)

    channels = {c["channel"]: (c["sent"], c["failed"]) for c in stats["pushes"]["channels"]}
    assert channels == {"course": (1, 1), "bulletin": (2, 0)}
    daily = stats["pushes"]["daily"]
    assert len(daily) == 7
    assert daily[-1] == {"day": _today(), "sent": 3, "failed": 1}
    assert sum(d["sent"] for d in daily) == 4


async def test_user_list_has_devices_activity_sync_and_account(db_session, portal_pool):
    now = datetime.now(UTC)
    user = await _user(db_session, "l1", display_name="測試")
    await _user(db_session, "l2", account="invalid")
    await _user(db_session, "l3", deleted_at=now)
    db_session.add_all(
        [
            UserDevice(
                user_id=user.id,
                client_device_id="a",
                platform="ios",
                last_seen_at=now - timedelta(hours=1),
                cloud_sync_enabled=False,
            ),
            UserDevice(
                user_id=user.id,
                client_device_id="b",
                platform="android",
                last_seen_at=now - timedelta(minutes=5),
            ),
            UserDevice(
                user_id=user.id,
                client_device_id="c",
                platform="macos",
                last_seen_at=now,
                deleted_at=now,
            ),
        ]
    )
    await db_session.commit()

    result = await list_users(_REQUEST, pool=portal_pool)

    rows = {r["student_id"]: r for r in result["items"]}
    assert set(rows) == {"l1", "l2"}
    first = rows["l1"]
    assert first["display_name"] == "測試"
    assert sorted(first["platforms"]) == ["android", "ios"]
    assert abs((first["last_active_at"] - (now - timedelta(minutes=5))).total_seconds()) < 1
    assert first["cloud_sync"] is True
    assert first["account"] == "signed_in"
    assert rows["l2"] == {**rows["l2"], "platforms": [], "last_active_at": None, "cloud_sync": None}
    assert rows["l2"]["account"] == "needs_sign_in"
    assert result["items"][0]["student_id"] == "l1"
    audits = await portal_pool.fetch("SELECT action, detail FROM admin_audit_log")
    assert [a["action"] for a in audits] == ["user.list"]


async def test_user_page_shows_devices_week_pushes_and_last_upload(db_session, portal_pool):
    now = datetime.now(UTC)
    today = _today()
    monday = today - timedelta(days=today.isoweekday() - 1)
    user = await _user(db_session, "u1", display_name="測試", locale="en")
    phone = UserDevice(
        user_id=user.id,
        client_device_id="phone",
        platform="ios",
        device_name="Phone",
        device_model="iPhone17,3",
        os_version="26.1",
        app_version="2.8.2",
        last_seen_at=now,
        bulletin_push_enabled=False,
    )
    mac = UserDevice(user_id=user.id, client_device_id="mac", platform="macos")
    db_session.add_all(
        [
            phone,
            mac,
            SemesterTerm(code="1151", start_date=today - timedelta(days=60), end_date=today + timedelta(days=60)),
            SemesterTerm(code="1142", start_date=today - timedelta(days=200), end_date=today - timedelta(days=90)),
            UserSettingsDocument(
                user_id=user.id, namespace="notification", document={"courses": {"enabled": False}}
            ),
        ]
    )
    await db_session.flush()
    course = UserCourse(
        user_id=user.id,
        semester="1151",
        course_key="k1",
        course_no="CS1",
        course_name="資料結構",
        classroom="TR-412",
        schedule_json=[{"day": 2, "periods": ["3", "4"]}],
    )
    db_session.add_all(
        [
            course,
            UserCourse(user_id=user.id, semester="1142", course_key="k0", course_no="CS0", course_name="舊課"),
            UserCourse(
                user_id=user.id,
                semester="1151",
                course_key="k2",
                course_no="CS2",
                course_name="退選",
                enrollment_status="dropped",
            ),
            DevicePushToken(
                device_id=phone.id,
                provider="apns",
                token_kind="standard",
                token_hash="h",
                token_value="v",
                environment="production",
            ),
            UserChangeLog(
                user_id=user.id,
                entity_type="course",
                entity_id="k1",
                operation="upsert",
                device_id=phone.id,
            ),
        ]
    )
    sent = _job(user, "course", payload={"title": "Class soon"})
    cancelled = _job(user, "schedule", status="cancelled")
    waiting = _job(user, "assignment", status="pending")
    db_session.add_all([sent, cancelled, waiting])
    await db_session.flush()
    db_session.add_all(
        [
            UserCourseSkippedDate(
                user_id=user.id, user_course_id=course.id, skipped_on=monday + timedelta(days=1)
            ),
            UserCourseSkippedDate(
                user_id=user.id, user_course_id=course.id, skipped_on=monday - timedelta(days=6)
            ),
            _delivery(sent, "sent", device=phone),
            _delivery(sent, "failed", device=mac),
        ]
    )
    await db_session.commit()

    page = await get_user(_REQUEST, user.id, pool=portal_pool)

    assert page["student_id"] == "u1"
    assert page["locale"] == "en"
    assert page["account"] == "signed_in"
    assert page["class_reminders"] is False
    devices = {d["id"]: d for d in page["devices"]}
    assert devices[phone.id]["push_token"] == {"provider": "apns", "environment": "production"}
    assert devices[phone.id]["bulletin_push_enabled"] is False
    assert devices[mac.id]["push_token"] is None
    assert page["week_start"] == monday
    assert page["term"] == "1151"
    assert page["courses"] == [
        {
            "id": course.id,
            "name": "資料結構",
            "classroom": "TR-412",
            "schedule": [{"day": 2, "periods": ["3", "4"]}],
            "skipped": [monday + timedelta(days=1)],
        }
    ]
    pushes = sorted((p["status"], p["channel"], p["device_id"]) for p in page["pushes"])
    assert pushes == [
        ("cancelled", "schedule", None),
        ("failed", "course", mac.id),
        ("sent", "course", phone.id),
    ]
    assert {p["title"] for p in page["pushes"]} == {"Class soon", ""}
    assert page["last_upload"]["device_id"] == phone.id
    audits = await portal_pool.fetch("SELECT action, target_user_id FROM admin_audit_log")
    assert [(a["action"], a["target_user_id"]) for a in audits] == [("user.view", user.id)]


async def test_user_page_for_an_unknown_user_is_not_found(db_session, portal_pool):
    with pytest.raises(HTTPException) as caught:
        await get_user(_REQUEST, uuid.uuid4(), pool=portal_pool)
    assert caught.value.status_code == 404


async def test_user_page_counts_every_push_while_listing_a_capped_number(
    db_session, portal_pool, monkeypatch
):
    user = await _user(db_session, "c1")
    job = _job(user, "course")
    db_session.add(job)
    await db_session.flush()
    db_session.add_all([_delivery(job, "sent"), _delivery(job, "failed")])
    cancelled = _job(user, "schedule", status="cancelled")
    db_session.add(cancelled)
    await db_session.commit()
    monkeypatch.setattr(users_route, "_PUSH_ROWS", 1)

    page = await get_user(_REQUEST, user.id, pool=portal_pool)

    assert len(page["pushes"]) == 1
    assert page["push_counts"] == {"sent": 1, "failed": 1, "cancelled": 1}


async def test_class_reminders_count_users_not_their_devices(db_session, portal_pool):
    on = await _user(db_session, "r1")
    off = await _user(db_session, "r2")
    db_session.add_all(
        [
            UserDevice(user_id=on.id, client_device_id="r1a", platform="ios"),
            UserDevice(user_id=on.id, client_device_id="r1b", platform="ipados"),
            UserDevice(user_id=off.id, client_device_id="r2a", platform="ios"),
            UserSettingsDocument(
                user_id=off.id, namespace="notification", document={"courses": {"enabled": False}}
            ),
        ]
    )
    await db_session.commit()

    stats = await user_stats(pool=portal_pool)

    assert stats["devices"]["features"]["class_reminders"] == 1
    assert stats["devices"]["users"] == 2


async def test_user_page_drops_schedule_entries_that_are_not_objects(db_session, portal_pool):
    today = _today()
    user = await _user(db_session, "s1")
    db_session.add(
        SemesterTerm(code="1151", start_date=today - timedelta(days=60), end_date=today + timedelta(days=60))
    )
    db_session.add(
        UserCourse(
            user_id=user.id,
            semester="1151",
            course_key="k1",
            course_no="CS1",
            course_name="c",
            schedule_json=[None, 3, {"day": 2, "periods": ["3"]}],
        )
    )
    await db_session.commit()

    page = await get_user(_REQUEST, user.id, pool=portal_pool)

    assert page["courses"][0]["schedule"] == [{"day": 2, "periods": ["3"]}]


async def test_user_page_lists_pushes_by_when_they_went_out(db_session, portal_pool):
    now = datetime.now(UTC)
    user = await _user(db_session, "w1")
    filed_early = _job(user, "assignment", created_at=now - timedelta(days=10))
    went_out_long_ago = _job(user, "course", created_at=now - timedelta(days=10))
    db_session.add_all([filed_early, went_out_long_ago])
    await db_session.flush()
    old = _delivery(went_out_long_ago, "sent")
    old.sent_at = now - timedelta(days=8)
    db_session.add_all([_delivery(filed_early, "sent"), old])
    await db_session.commit()

    page = await get_user(_REQUEST, user.id, pool=portal_pool)

    assert [p["channel"] for p in page["pushes"]] == ["assignment"]
    assert page["push_counts"]["sent"] == 1


async def test_user_page_falls_back_to_a_free_form_semester_while_no_term_is_published(
    db_session, portal_pool
):
    meets = [{"day": 2, "periods": ["3"]}]
    only_free = await _user(db_session, "t1")
    mixed = await _user(db_session, "t2")
    db_session.add_all(
        [
            UserCourse(
                user_id=only_free.id, semester="fall", course_key="a", course_name="a",
                source="user_added", schedule_json=meets,
            ),
            UserCourse(
                user_id=mixed.id, semester="zzz", course_key="b", course_name="b",
                source="user_added", schedule_json=meets,
            ),
            UserCourse(
                user_id=mixed.id, semester="1142", course_key="c", course_name="c",
                source="user_added", schedule_json=meets,
            ),
        ]
    )
    await db_session.commit()

    free_page = await get_user(_REQUEST, only_free.id, pool=portal_pool)
    mixed_page = await get_user(_REQUEST, mixed.id, pool=portal_pool)

    assert (free_page["term"], len(free_page["courses"])) == ("fall", 1)
    assert mixed_page["term"] == "1142"


async def test_user_page_titles_a_class_reminder_with_its_course(db_session, portal_pool):
    user = await _user(db_session, "n1")
    job = _job(user, "course", payload={"kind": "course_reminder", "course_name": "資料結構", "body": "TR-412"})
    db_session.add(job)
    await db_session.flush()
    db_session.add(_delivery(job, "sent"))
    await db_session.commit()

    page = await get_user(_REQUEST, user.id, pool=portal_pool)

    assert [p["title"] for p in page["pushes"]] == ["資料結構"]


async def test_pushes_count_a_job_that_failed_before_any_delivery_once(db_session, portal_pool):
    user = await _user(db_session, "f1")
    no_device = _job(user, "assignment", status="failed")
    every_device = _job(user, "course", status="failed")
    db_session.add_all([no_device, every_device])
    await db_session.flush()
    db_session.add(_delivery(every_device, "failed"))
    await db_session.commit()

    stats = await user_stats(pool=portal_pool)

    channels = {c["channel"]: (c["sent"], c["failed"]) for c in stats["pushes"]["channels"]}
    assert channels == {"assignment": (0, 1), "course": (0, 1)}
    assert stats["pushes"]["daily"][-1]["failed"] == 2
