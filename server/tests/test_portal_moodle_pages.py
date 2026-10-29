"""The data the portal's Moodle pages read beyond the sync-job queue: the
student search and timetable of Data inspection, what its panels show of a
student, and the counts the Sync page draws. In the server suite for the
reason test_portal_sync_job_retry.py gives."""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.db import _asyncpg_dsn  # noqa: E402
from portal.app.routes.moodle.inspection import (  # noqa: E402
    search_students,
    student_timetable,
    sync_events,
)
from portal.app.routes.moodle.status import moodle_stats, moodle_status  # noqa: E402
from server.academic_calendar.models import AcademicHoliday, CourseChange, SemesterTerm  # noqa: E402
from server.auth.models import (  # noqa: E402
    DevicePushToken,
    ExternalAccount,
    PushDelivery,
    PushJob,
    User,
    UserDevice,
)
from server.sync.models import UserCourse  # noqa: E402
from server.syncjobs.models import SyncJob, SyncPolicy, SyncRun  # noqa: E402

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _request(path: str = "/api/moodle") -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(actor="ops@example.com"), url=SimpleNamespace(path=path)
    )


@pytest_asyncio.fixture(loop_scope="session")
async def pool(test_settings, db_session):
    p = await asyncpg.create_pool(
        _asyncpg_dsn(test_settings.database_url), min_size=1, max_size=2
    )
    yield p
    await p.close()


async def _user(db_session, student_id: str, name: str | None = None, **fields) -> User:
    user = User(student_id=student_id, display_name=name, **fields)
    db_session.add(user)
    await db_session.flush()
    return user


async def _device(db_session, user: User, platform: str = "ios", **fields) -> UserDevice:
    device = UserDevice(
        user_id=user.id,
        client_device_id=f"{user.student_id}-{platform}",
        platform=platform,
        **fields,
    )
    db_session.add(device)
    await db_session.flush()
    return device


async def _job(db_session, user: User, **fields) -> SyncJob:
    account = ExternalAccount(
        user_id=user.id, provider="ntust_sso", external_user_id=user.student_id
    )
    db_session.add(account)
    await db_session.flush()
    job = SyncJob(
        user_id=user.id,
        external_account_id=account.id,
        job_type="moodle_assignments",
        **fields,
    )
    db_session.add(job)
    await db_session.flush()
    return job


async def test_search_matches_part_of_a_student_id_or_a_name(db_session, pool):
    """Data inspection finds a student from a fragment of either; a name
    two students share lists both, so the operator picks one."""
    seen = datetime.now(UTC) - timedelta(minutes=5)
    first = await _user(db_session, "B99915023", "王小明")
    await _device(db_session, first, last_seen_at=seen)
    await _device(db_session, first, "ipados")
    await _device(db_session, await _user(db_session, "B99916050", "王小明"))
    await _user(db_session, "M99911208", "黃志明")
    await _user(db_session, "B99910001", "王小明", deleted_at=datetime.now(UTC))
    await db_session.commit()

    by_id = await search_students(_request(), q="15023", pool=pool)
    by_name = await search_students(_request(), q="小明", pool=pool)
    any_case = await search_students(_request(), q="m9991", pool=pool)
    wildcard = await search_students(_request(), q="%", pool=pool)
    blank = await search_students(_request(), q="  ", pool=pool)

    assert by_id["students"] == [
        {
            "student_id": "B99915023",
            "display_name": "王小明",
            "devices": 2,
            "last_seen_at": seen,
        }
    ]
    assert sorted(s["student_id"] for s in by_name["students"]) == ["B99915023", "B99916050"]
    assert [s["student_id"] for s in any_case["students"]] == ["M99911208"]
    assert wildcard["students"] == [] and blank["students"] == []
    audit = await pool.fetch("SELECT action, detail FROM admin_audit_log")
    assert [(a["action"], json.loads(a["detail"])["count"]) for a in audit] == [("user.list", 1)]


async def _timetable_fixture(db_session) -> User:
    today = date.today()
    db_session.add_all([
        SemesterTerm(code="1151", start_date=today - timedelta(days=30), end_date=today + timedelta(days=90)),
        SemesterTerm(code="1142", start_date=today - timedelta(days=200), end_date=today - timedelta(days=60)),
    ])
    user = await _user(db_session, "B99915023", "王小明")
    db_session.add_all([
        UserCourse(
            user_id=user.id, semester="1151", course_key="ntust:1151:CS2001301",
            course_no="CS2001301", classroom="TR-412",
            course_name="115.1【資工系】CS2001301 資料結構 Data Structures",
            schedule_json=[{"day": 2, "periods": ["3", "4"]}, {"day": 4, "periods": [2, 3]}],
        ),
        UserCourse(
            user_id=user.id, semester="1151", course_key="ntust:1151:MA1001703",
            course_no="MA1001703", course_name="微積分", classroom="T2-302",
            schedule_json=[{"day": 1, "periods": ["1", "2"]}],
        ),
        UserCourse(
            user_id=user.id, semester="1142", course_key="ntust:1142:CS1002301",
            course_no="CS1002301", course_name="程式設計",
            schedule_json=[{"day": 3, "periods": ["3"]}],
        ),
    ])
    await db_session.flush()

    def change(course_no: str, start: date, **fields) -> CourseChange:
        return CourseChange(
            term_code="1151", course_no=course_no, mode="day", start_date=start,
            end_date=start, created_by="ops@example.com", updated_by="ops@example.com",
            **fields,
        )

    db_session.add_all([
        change("CS2001301", today + timedelta(days=3), kind="room", room="IB-302"),
        change("CS2001301", today - timedelta(days=3), kind="cancelled"),
        change("EE2005701", today + timedelta(days=3), kind="cancelled"),
    ])
    await db_session.commit()
    return user


async def test_timetable_opens_on_the_current_term_with_its_upcoming_changes(
    db_session, pool
):
    """The term toggle lists every term the student has courses in, the
    published current one first; the grid takes each course's meetings, its
    plain name and its colour, and the changes still to come for its course."""
    await _timetable_fixture(db_session)

    current = await student_timetable(_request(), student_id="B99915023", term=None, pool=pool)

    assert (current["term"], current["current_term"], current["terms"]) == (
        "1151", "1151", ["1151", "1142"],
    )
    courses = {c["course_no"]: c for c in current["courses"]}
    assert set(courses) == {"CS2001301", "MA1001703"}
    assert courses["CS2001301"]["name"] == "資料結構 Data Structures"
    assert courses["CS2001301"]["classroom"] == "TR-412"
    assert courses["CS2001301"]["schedule"] == [
        {"day": 2, "periods": ["3", "4"]}, {"day": 4, "periods": [2, 3]},
    ]
    assert courses["MA1001703"]["color"].startswith("#")
    assert [(c["course_no"], c["kind"], c["room"]) for c in current["changes"]] == [
        ("CS2001301", "room", "IB-302"),
    ]


async def test_timetable_takes_the_earliest_term_before_any_has_begun(db_session, pool):
    """The apps' rule (server/academic_calendar/terms.py current_term_code):
    while every published term starts later, the earliest one is current."""
    today = date.today()
    db_session.add_all([
        SemesterTerm(code="1152", start_date=today + timedelta(days=120), end_date=today + timedelta(days=240)),
        SemesterTerm(code="1151", start_date=today + timedelta(days=10), end_date=today + timedelta(days=100)),
    ])
    await _user(db_session, "B99915023")
    await db_session.commit()

    current = await student_timetable(_request(), student_id="B99915023", term=None, pool=pool)

    assert (current["term"], current["current_term"], current["terms"]) == ("1151", "1151", ["1151"])


async def test_timetable_drops_schedule_entries_that_are_not_meetings(db_session, pool):
    """The course upload takes any list, so a null or a bare value may sit
    among the meetings; the grid gets only the objects."""
    user = await _timetable_fixture(db_session)
    db_session.add(UserCourse(
        user_id=user.id, semester="1151", course_key="client:x2", course_no="X2",
        course_name="Reading group", schedule_json=[None, "x", {"day": 5, "periods": ["1"]}],
    ))
    await db_session.commit()

    current = await student_timetable(_request(), student_id="B99915023", term=None, pool=pool)

    schedules = {c["course_no"]: c["schedule"] for c in current["courses"]}
    assert schedules["X2"] == [{"day": 5, "periods": ["1"]}]


async def test_timetable_changes_name_the_days_the_class_meets(db_session, pool):
    """A change carries the days it reaches by the shared rule
    (course_class_days): a holiday on its first Tuesday drops that day."""
    user = await _timetable_fixture(db_session)
    today = date.today()
    tuesday = today + timedelta(days=(1 - today.weekday()) % 7 or 7)
    db_session.add_all([
        AcademicHoliday(name_zh="校慶", name_en="Anniversary", start_date=tuesday, end_date=tuesday),
        CourseChange(
            term_code="1151", course_no="CS2001301", mode="range", kind="cancelled",
            start_date=tuesday, end_date=tuesday + timedelta(days=7), excluded_weekdays=[4],
            created_by="ops@example.com", updated_by="ops@example.com",
        ),
    ])
    await db_session.commit()

    current = await student_timetable(_request(), student_id="B99915023", term=None, pool=pool)

    ranged = [c for c in current["changes"] if c["mode"] == "range"]
    assert [c["class_days"] for c in ranged] == [[tuesday + timedelta(days=7)]]


async def test_timetable_offers_only_term_codes_as_terms(db_session, pool):
    """Moodle mirror rows are filed under an empty semester and a hand-added
    course may carry any string; neither is a term to toggle to."""
    user = await _timetable_fixture(db_session)
    db_session.add_all([
        UserCourse(
            user_id=user.id, semester="", course_key="moodle:4120",
            source="ntust_portal", course_no="CS2001301", course_name="資料結構",
        ),
        UserCourse(
            user_id=user.id, semester="fall", course_key="client:x1",
            course_no="X1", course_name="Reading group",
        ),
    ])
    await db_session.commit()

    current = await student_timetable(_request(), student_id="B99915023", term=None, pool=pool)

    assert current["terms"] == ["1151", "1142"]


async def test_timetable_of_an_earlier_term_carries_no_changes(db_session, pool):
    await _timetable_fixture(db_session)

    older = await student_timetable(_request(), student_id="B99915023", term="1142", pool=pool)
    missing = await student_timetable(_request(), student_id="B99999999", term=None, pool=pool)

    assert older["term"] == "1142"
    assert [c["course_no"] for c in older["courses"]] == ["CS1002301"]
    assert older["changes"] == []
    assert missing.status_code == 404


async def test_inspection_shows_the_name_sign_in_and_which_devices_take_push(
    db_session, pool
):
    user = await _user(db_session, "B99915023", "王小明")
    db_session.add(
        ExternalAccount(
            user_id=user.id, provider="ntust_sso", external_user_id="B99915023",
            credential_status="expired",
        )
    )
    phone = await _device(db_session, user)
    await _device(db_session, user, "macos")
    db_session.add(
        DevicePushToken(
            device_id=phone.id, provider="apns", token_kind="standard",
            token_hash="hash-std", token_value="std-tok", bundle_id="org.ntust.app.TigerDuck",
        )
    )
    await db_session.commit()

    events = await sync_events(_request(), student_id="B99915023", limit=100, pool=pool)

    assert events["student"] == {"display_name": "王小明", "credential_status": "expired"}
    assert {d["platform"]: d["has_push_token"] for d in events["devices"]} == {
        "ios": True, "macos": False,
    }


async def test_inspection_keeps_sent_pushes_and_the_jobs_a_device_started(
    db_session, pool
):
    """A device's Push tab and Source push tab show history, not only the
    queue: a sent sync trigger the iPhone caused, and its delivery to the iPad."""
    user = await _user(db_session, "B99915023", "王小明")
    phone = await _device(db_session, user)
    tablet = await _device(db_session, user, "ipados")
    now = datetime.now(UTC)
    job = PushJob(
        user_id=user.id, dedupe_key="sync_trigger:x", channel="system",
        scenario="sync_trigger", fire_at=now, available_at=now, status="sent",
        payload={"kind": "sync_trigger", "source_device_id": str(phone.id)},
    )
    db_session.add(job)
    await db_session.flush()
    db_session.add(
        PushDelivery(
            push_job_id=job.id, user_id=user.id, device_id=tablet.id, provider="apns",
            token_kind="standard", token_hash="hash", status="sent", attempts=1,
            sent_at=now, next_retry_at=now,
        )
    )
    await db_session.commit()

    events = await sync_events(_request(), student_id="B99915023", limit=100, pool=pool)

    assert [(j["id"], j["status"], j["source_device_id"]) for j in events["push_jobs"]] == [
        (job.id, "sent", str(phone.id)),
    ]
    assert [
        (d["push_job_id"], d["device_id"], d["status"], d["channel"], d["scenario"])
        for d in events["push_deliveries"]
    ] == [(job.id, tablet.id, "sent", "system", "sync_trigger")]


async def test_status_counts_what_the_retry_dialog_offers(db_session, pool):
    """Retry failed also takes a pending job that keeps its last error, and
    the notice goes to students waiting to sign in with an iPhone syncing."""
    later = datetime.now(UTC) + timedelta(hours=8)
    waiting = await _user(db_session, "b1")
    await _job(db_session, waiting, status="disabled", run_after=later)
    await _device(db_session, waiting)
    await _job(db_session, await _user(db_session, "b2"), status="disabled", run_after=later)
    await _job(
        db_session, await _user(db_session, "b3"), status="pending",
        last_error="sync_failed:http_503", run_after=later,
    )
    await _job(db_session, await _user(db_session, "b4"), status="pending", run_after=later)
    db_session.add(
        SyncPolicy(job_type="moodle_assignments", enabled=True, default_interval_seconds=28800)
    )
    await db_session.commit()

    status = await moodle_status(pool=pool)

    assert status["job_counts"] == {
        "active": 2, "running": 0, "failed": 0, "disabled": 2,
        "pending_with_error": 1, "waiting_to_sign_in": 1,
    }
    assert status["interval_seconds"] == 28800


async def test_stats_count_runs_per_hour_for_the_last_day(db_session, pool):
    user = await _user(db_session, "b1")
    job = await _job(db_session, user, status="pending")
    now = datetime.now(UTC)

    def run(ago: timedelta, status: str) -> SyncRun:
        return SyncRun(
            sync_job_id=job.id, user_id=user.id, started_at=now - ago,
            finished_at=now - ago + timedelta(seconds=3), status=status,
        )

    db_session.add_all([
        run(timedelta(minutes=1), "succeeded"),
        run(timedelta(minutes=2), "succeeded"),
        run(timedelta(hours=3), "failed"),
        run(timedelta(hours=25), "succeeded"),
    ])
    await db_session.commit()

    stats = await moodle_stats(_request(), pool=pool)

    hourly = {datetime.fromisoformat(h["hour"]): h for h in stats["hourly"]}
    hours = sorted(hourly)
    assert len(hours) == 24
    assert all(b - a == timedelta(hours=1) for a, b in zip(hours, hours[1:]))
    assert hours[-1] <= now < hours[-1] + timedelta(hours=1)
    failed_at = (now - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    assert hourly[failed_at]["failed"] == 1
    assert sum(h["succeeded"] for h in hourly.values()) == 2
    assert sum(h["failed"] for h in hourly.values()) == 1
