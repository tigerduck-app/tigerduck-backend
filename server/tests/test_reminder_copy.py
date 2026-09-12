"""Assignment reminders: the apps' own copy, in each device's language.

Spec §4.1 makes the backend the owner of per-device-language notification
copy, and 2.1.0 hands iOS its assignment reminders entirely -- there is no
local scheduler left to fall back on. So the copy has to be the apps' own
(`notification_assignment_reminder_*`, chosen per offset the way iOS 2.0.2
chose it), resolved from the recipient device's locale at send time. It
also has to read correctly for the sub-hour offsets that only
`assignments.reminder_offsets_minutes` can carry (spec §4.6), which the
scan reads in preference to `reminder_offsets_hours`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.models import DevicePushToken, PushJob, User, UserDevice
from server.db import build_session_factory
from server.i18n import _BUNDLE_DIR, translate
from server.push.apns_client import SendResult
from server.push.notification_copy import (
    build_assignment_reminder_payload,
    reminder_body_key,
)
from server.push.pipeline import PushPipelineWorker, run_push_tick
from server.push.reminders import CHANNEL as REMINDER_CHANNEL
from server.push.reminders import _dedupe_key, scan_assignment_reminders
from server.push.router import PushRouter
from server.sync.models import UserAssignment, UserSettingsDocument

# Applied per-function rather than as a module-level `pytestmark`: most of
# this module is plain sync functions with no fixture, and pytest-asyncio
# warns when the asyncio mark reaches one (same note in test_reauth_push.py).
asyncio_session = pytest.mark.asyncio(loop_scope="session")

TITLE_KEY = "notification_assignment_reminder_title"
BODY_KEY = "notification_assignment_reminder_body_"
COURSE = "資料結構"
ASSIGNMENT = "HW3"

# iOS 2.0.2's `AssignmentReminderOffset.notificationBody`, case by case
# (tigerduck-app, swift/TigerDuck/LiveActivity/Models/
# AssignmentReminderOffset.swift); Android's `AssignmentReminderOffset`
# enum copies it. Minutes before the due date -> body key suffix.
IOS_OFFSET_BODIES = [
    (48 * 60, "48h"),
    (24 * 60, "24h"),
    (16 * 60, "16h"),
    (8 * 60, "multi_hour"),
    (4 * 60, "multi_hour"),
    (2 * 60, "multi_hour"),
    (1 * 60, "multi_hour"),
    (30, "30m"),
    (15, "15m"),
    (10, "15m"),
    (5, "5m"),
]

LOCALES = sorted(path.stem for path in _BUNDLE_DIR.glob("*.json"))


def _reminder(offset_hours: float, *, course: str | None = COURSE) -> dict:
    """A payload in the shape `scan_assignment_reminders` files."""
    return {
        "kind": "assignment_reminder",
        "assignment_title": ASSIGNMENT,
        "course_name": course,
        "moodle_assignment_id": 3,
        "moodle_course_id": 7001,
        "due_at": "2026-09-20T00:00:00+00:00",
        "due_epoch": 1_789_862_400,
        "offset_hours": offset_hours,
    }


def _expected(key: str, locale: str, *args: str) -> str:
    text = translate(key, locale)
    for position, value in enumerate(args, start=1):
        text = text.replace(f"%{position}$s", value)
    return text


# --- which body each offset gets ------------------------------------------


@pytest.mark.parametrize(("minutes", "suffix"), IOS_OFFSET_BODIES)
def test_body_key_mirrors_the_ios_offset_mapping(minutes, suffix):
    assert reminder_body_key(minutes) == BODY_KEY + suffix


@pytest.mark.parametrize(
    ("minutes", "suffix"),
    [
        (72 * 60, "48h"),  # longer than any offered offset
        (36 * 60, "48h"),  # the 24h body says "24 hours"; this is not 24 hours
        (20 * 60, "16h"),
        (12 * 60, "multi_hour"),
        (45, "30m"),
        (20, "15m"),
        (9, "5m"),
        (0, "5m"),
    ],
)
def test_an_offset_no_app_offers_reads_like_the_nearest_shorter_one(
    minutes, suffix
):
    assert reminder_body_key(minutes) == BODY_KEY + suffix


# --- what the device receives ---------------------------------------------


def test_every_locale_renders_every_offset_completely():
    """No placeholder left behind, and no fractional hour, in any locale.

    The old copy was built in code as "剩 {hours:g} 小時", which said
    "剩 0.5 小時" for a 30-minute reminder and was Chinese on every device.
    Collected across all bundles before asserting, so a failure lists every
    locale that breaks rather than the first.
    """
    assert "en" in LOCALES and "zh-Hant" in LOCALES  # the glob found bundles
    problems = []
    for locale in LOCALES:
        for minutes, _ in IOS_OFFSET_BODIES:
            sent = build_assignment_reminder_payload(
                _reminder(minutes / 60), locale=locale
            )
            want_title = _expected(TITLE_KEY, locale, COURSE)
            want_body = _expected(reminder_body_key(minutes), locale, COURSE, ASSIGNMENT)
            if sent["title"] != want_title or sent["body"] != want_body:
                problems.append((locale, minutes, sent["title"], sent["body"]))
            for text in (sent["title"], sent["body"]):
                if re.search(r"%\d+\$s", text) or re.search(r"\d\.\d", text):
                    problems.append((locale, minutes, text))
    assert not problems, problems[:10]


def test_the_title_names_the_course_and_the_body_names_both():
    """The apps' argument order: title (course), body (course, assignment)."""
    sent = build_assignment_reminder_payload(_reminder(24.0), locale="en")
    assert sent["title"] == _expected(TITLE_KEY, "en", COURSE)
    assert COURSE in sent["title"] and ASSIGNMENT not in sent["title"]
    assert sent["body"] == _expected(BODY_KEY + "24h", "en", COURSE, ASSIGNMENT)


def test_an_unknown_course_leaves_no_empty_title_and_no_double_space():
    sent = build_assignment_reminder_payload(_reminder(24.0, course=None), locale="en")
    assert sent["title"] == _expected(TITLE_KEY, "en", ASSIGNMENT)
    assert ASSIGNMENT in sent["body"]
    assert "  " not in sent["body"]
    assert sent["body"] == sent["body"].strip()


def test_the_device_gets_copy_and_routing_keys_but_not_the_copy_inputs():
    sent = build_assignment_reminder_payload(_reminder(2.0), locale="ja")
    assert "assignment_title" not in sent
    assert "course_name" not in sent
    assert sent["kind"] == "assignment_reminder"
    assert sent["moodle_assignment_id"] == 3
    assert sent["offset_hours"] == 2.0


def test_a_job_filed_with_finished_copy_goes_out_as_filed():
    """A job filed before copy moved to send time has no copy inputs."""
    filed = {
        "kind": "assignment_reminder",
        "title": f"作業提醒：{ASSIGNMENT}",
        "body": f"{COURSE} · 剩 24 小時",
        "moodle_assignment_id": 3,
        "offset_hours": 24.0,
    }
    assert build_assignment_reminder_payload(dict(filed), locale="ja") == filed


# --- end to end: scan -> pipeline -> each device ---------------------------


class ScriptedSender:
    """PushSender double recording every request it is handed.

    Duplicated rather than shared, as in every push test module here.
    """

    def __init__(self):
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        return SendResult(success=True, status="200")

    async def send_multi(self, requests):
        return [await self.send(r) for r in requests]

    async def close(self):
        pass


async def _iphone(session, user, client_device_id, locale, platform="ios"):
    device = UserDevice(
        user_id=user.id,
        client_device_id=client_device_id,
        platform=platform,
        app_version="2.1.0",
        locale=locale,
    )
    session.add(device)
    await session.flush()
    session.add(
        DevicePushToken(
            device_id=device.id,
            provider="apns",
            token_kind="standard",
            token_hash=f"hash-{client_device_id}",
            token_value=f"tok-{client_device_id}",
            bundle_id="org.ntust.app.TigerDuck",
        )
    )
    return device


async def _user_with_assignment(session, *, due_in_hours=30.0, document=None):
    user = User(student_id="b11203058")
    session.add(user)
    await session.flush()
    assignment = UserAssignment(
        user_id=user.id,
        moodle_course_id=7001,
        moodle_assignment_id=3,
        course_name=COURSE,
        title=ASSIGNMENT,
        due_at=datetime.now(UTC) + timedelta(hours=due_in_hours),
    )
    session.add(assignment)
    if document is not None:
        session.add(
            UserSettingsDocument(
                user_id=user.id, namespace="notification", document=document
            )
        )
    await session.flush()
    return user, assignment


async def _jobs(session, user_id):
    return (
        (
            await session.execute(
                select(PushJob)
                .where(PushJob.user_id == user_id)
                .order_by(PushJob.fire_at)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


@asyncio_session
async def test_each_device_gets_the_reminder_in_its_own_language(
    db_session, prepared_engine, test_settings
):
    user, _ = await _user_with_assignment(db_session)
    await _iphone(db_session, user, "iphone-tw", "zh-Hant-TW")
    await _iphone(db_session, user, "ipad-jp", "ja-JP", platform="ipados")
    await db_session.commit()

    factory = build_session_factory(prepared_engine)
    await scan_assignment_reminders(factory, test_settings)  # default [24, 2]
    job = next(j for j in await _jobs(db_session, user.id) if j.scenario == "reminder_24h")
    past = datetime.now(UTC) - timedelta(minutes=5)
    job.fire_at = past
    job.available_at = past
    await db_session.commit()

    apple = ScriptedSender()
    await run_push_tick(
        PushPipelineWorker(
            session_factory=factory,
            settings=test_settings,
            router=PushRouter(apple=apple, android=ScriptedSender()),
            worker_id="reminder-copy-test-worker",
        )
    )

    messages = {r.device_token: r.message for r in apple.requests}
    assert set(messages) == {"tok-iphone-tw", "tok-ipad-jp"}
    for token, locale in (("tok-iphone-tw", "zh-Hant"), ("tok-ipad-jp", "ja")):
        message = messages[token]
        assert message["aps"]["alert"] == {
            "title": _expected(TITLE_KEY, locale, COURSE),
            "body": _expected(BODY_KEY + "24h", locale, COURSE, ASSIGNMENT),
        }, token
        assert "assignment_title" not in message
        assert "course_name" not in message
        assert message["moodle_assignment_id"] == "3"


# --- which offsets the scan reads (spec §4.6) -------------------------------


@asyncio_session
async def test_minutes_offsets_are_authoritative_when_present(
    db_session, prepared_engine, test_settings
):
    """iOS 2.1.0 writes both fields, with its sub-hour offsets in minutes only."""
    user, assignment = await _user_with_assignment(
        db_session,
        document={
            "assignments": {
                "enabled": True,
                "reminder_offsets_hours": [24],
                "reminder_offsets_minutes": [30, 10],
            }
        },
    )
    await db_session.commit()

    await scan_assignment_reminders(build_session_factory(prepared_engine), test_settings)

    jobs = await _jobs(db_session, user.id)
    assert [j.scenario for j in jobs] == ["reminder_0.5h", "reminder_0.166667h"]
    assert [assignment.due_at - j.fire_at for j in jobs] == [
        timedelta(minutes=30),
        timedelta(minutes=10),
    ]
    assert [j.payload["offset_hours"] for j in jobs] == [0.5, 10 / 60]


@asyncio_session
async def test_an_empty_minutes_list_means_no_reminders(
    db_session, prepared_engine, test_settings
):
    """Authoritative includes "none": the user turned every offset off."""
    user, _ = await _user_with_assignment(
        db_session,
        document={
            "assignments": {
                "enabled": True,
                "reminder_offsets_hours": [24],
                "reminder_offsets_minutes": [],
            }
        },
    )
    await db_session.commit()

    await scan_assignment_reminders(build_session_factory(prepared_engine), test_settings)

    assert await _jobs(db_session, user.id) == []


_ABSENT = object()


@asyncio_session
@pytest.mark.parametrize("minutes", [_ABSENT, None, "30"], ids=["absent", "null", "string"])
async def test_hours_are_read_when_the_document_carries_no_minutes_list(
    db_session, prepared_engine, test_settings, minutes
):
    section = {"enabled": True, "reminder_offsets_hours": [8]}
    if minutes is not _ABSENT:
        section["reminder_offsets_minutes"] = minutes
    user, _ = await _user_with_assignment(db_session, document={"assignments": section})
    await db_session.commit()

    await scan_assignment_reminders(build_session_factory(prepared_engine), test_settings)

    assert [j.scenario for j in await _jobs(db_session, user.id)] == ["reminder_8h"]


@asyncio_session
async def test_minutes_elements_are_validated_like_hours(
    db_session, prepared_engine, test_settings
):
    user, _ = await _user_with_assignment(
        db_session,
        document={
            "assignments": {
                "reminder_offsets_minutes": [30, "15", None, {"m": 5}, 10.0],
            }
        },
    )
    await db_session.commit()

    await scan_assignment_reminders(build_session_factory(prepared_engine), test_settings)

    scenarios = [j.scenario for j in await _jobs(db_session, user.id)]
    assert scenarios == ["reminder_0.5h", "reminder_0.166667h"]


@asyncio_session
async def test_a_pending_job_filed_with_chinese_copy_is_rewritten_in_place(
    db_session, prepared_engine, test_settings
):
    """A reminder filed before this change must not go out in Chinese to a
    device set to another language: the next scan swaps its finished copy
    for the copy inputs, keeping the job, its key and its fire time."""
    user, assignment = await _user_with_assignment(db_session)
    due_epoch = int(assignment.due_at.timestamp())
    fire_at = assignment.due_at - timedelta(hours=24)
    filed = PushJob(
        user_id=user.id,
        dedupe_key=_dedupe_key(3, 24.0, due_epoch),
        channel=REMINDER_CHANNEL,
        scenario="reminder_24h",
        fire_at=fire_at,
        payload={
            "kind": "assignment_reminder",
            "title": f"作業提醒：{ASSIGNMENT}",
            "body": f"{COURSE} · 剩 24 小時",
            "moodle_assignment_id": 3,
            "moodle_course_id": 7001,
            "due_at": assignment.due_at.isoformat(),
            "due_epoch": due_epoch,
            "offset_hours": 24.0,
        },
    )
    db_session.add(filed)
    await db_session.commit()
    filed_id = filed.id

    await scan_assignment_reminders(build_session_factory(prepared_engine), test_settings)

    rewritten = next(j for j in await _jobs(db_session, user.id) if j.id == filed_id)
    assert rewritten.status == "pending"
    assert rewritten.fire_at == fire_at
    assert rewritten.payload["assignment_title"] == ASSIGNMENT
    assert rewritten.payload["course_name"] == COURSE
    assert "title" not in rewritten.payload
    assert "body" not in rewritten.payload
