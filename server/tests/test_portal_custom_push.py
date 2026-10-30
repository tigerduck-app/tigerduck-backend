"""The portal's custom push, checked against the server code that sends it.

The portal writes these push_jobs rows with raw SQL and does not import the
server package, so nothing else keeps the payload in the shape the push
pipeline and the two apps expect. Lives in the server suite, the only one
with the server code and a database.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portal.app.db import _asyncpg_dsn  # noqa: E402
from portal.app.routes.custom_push import (  # noqa: E402
    _PreviewRequest,
    _SendRequest,
    build_custom_push_payload,
    preview,
    send,
)
from server.academic_calendar.models import SemesterTerm  # noqa: E402
from server.academic_calendar.terms import current_term_code  # noqa: E402
from server.auth.models import DevicePushToken, User, UserDevice  # noqa: E402
from server.models import DeviceRegistration  # noqa: E402
from server.push.custom_push_targeting import TargetFilter, count_by_class  # noqa: E402
from server.push.job_payloads import build_apns_for_job, build_fcm_for_job  # noqa: E402
from server.sync.models.courses import UserCourse  # noqa: E402

_NOW = datetime(2026, 10, 4, 2, 0, tzinfo=UTC)
_NOTIFICATION_ID = "ecc66bcb4a75f302"
_REQUEST = SimpleNamespace(state=SimpleNamespace(actor="ops@example.com"))


def _popup(*, force_ring: bool = False) -> dict:
    return build_custom_push_payload(
        title="停課通知",
        body="明天停課",
        force_ring=force_ring,
        notification_id=_NOTIFICATION_ID,
    )


def _record(*, force_ring: bool = False) -> dict:
    return build_custom_push_payload(
        title="停課通知",
        body="明天停課",
        force_ring=force_ring,
        bulletin_id=77,
    )


def _fcm_data(payload: dict) -> dict:
    return build_fcm_for_job(
        payload=payload, channel="custom", token_value="fcm-tok"
    ).data


def _apns_message(payload: dict) -> dict:
    return build_apns_for_job(
        payload=payload,
        channel="custom",
        token_value="ab" * 32,
        bundle_id="org.ntust.app.TigerDuck",
        now=_NOW,
    ).message


def test_a_popup_reaches_android_as_a_popup_it_shows() -> None:
    # FcmService shows a custom_push_popup only when it carries a
    # notification_id; a message of any other kind without a bulletin_id is
    # dropped without a word.
    data = _fcm_data(_popup())

    assert data["kind"] == "custom_push_popup"
    assert data["notification_id"] == _NOTIFICATION_ID
    assert (data["title"], data["body"]) == ("停課通知", "明天停課")


def test_a_popup_on_an_iphone_carries_what_the_tap_handler_reads() -> None:
    # TigerDuckApp.routeServerPushTap opens the in-app popup only when
    # kind, notification_id, title and body are all top-level keys.
    message = _apns_message(_popup())

    assert message["kind"] == "custom_push_popup"
    assert message["notification_id"] == _NOTIFICATION_ID
    assert (message["title"], message["body"]) == ("停課通知", "明天停課")
    assert message["aps"]["alert"] == {"title": "停課通知", "body": "明天停課"}


def test_a_recorded_push_points_both_apps_at_its_bulletin() -> None:
    data = _fcm_data(_record())
    message = _apns_message(_record())

    assert data["kind"] == "custom_push_bulletin"
    assert data["bulletin_id"] == "77"
    assert message["kind"] == "custom_push_bulletin"
    assert int(message["bulletin_id"]) == 77


@pytest.mark.parametrize("build", [_popup, _record], ids=["popup", "record"])
@pytest.mark.parametrize("force_ring", [False, True])
def test_an_iphone_rings_only_when_the_operator_asked(build, force_ring) -> None:
    aps = _apns_message(build(force_ring=force_ring))["aps"]

    assert ("sound" in aps) is force_ring


@pytest.mark.parametrize("build", [_popup, _record], ids=["popup", "record"])
@pytest.mark.parametrize("force_ring", [False, True])
def test_android_is_told_to_ring_only_when_the_operator_asked(build, force_ring) -> None:
    # FcmService picks the audible or silent channel from force_ring; the
    # channel id alongside it has to say the same thing.
    data = _fcm_data(build(force_ring=force_ring))

    assert data["force_ring"] == ("true" if force_ring else "false")
    assert data["android_channel_id"] == (
        "bulletins_sound" if force_ring else "bulletins_silent"
    )


@pytest_asyncio.fixture(loop_scope="session")
async def portal_pool(test_settings, db_session):
    pool = await asyncpg.create_pool(
        _asyncpg_dsn(test_settings.database_url), min_size=1, max_size=2
    )
    yield pool
    await pool.close()


async def _recipients(db_session) -> None:
    """A signed-in iPhone, and anonymous devices a recorded push's filter
    takes (an iPhone, an Apple row from before device_class, a Pixel) or
    leaves out (one linked to the student, one without a token)."""
    user = User(student_id="b1")
    db_session.add(user)
    await db_session.flush()
    phone = UserDevice(
        user_id=user.id, client_device_id="iphone-1", platform="ios", device_class="iphone"
    )
    db_session.add(phone)
    await db_session.flush()
    db_session.add(
        DevicePushToken(
            device_id=phone.id,
            provider="apns",
            token_kind="standard",
            token_hash="iphone-1",
            token_value="tok",
            status="active",
        )
    )
    for device_id, platform, device_class, token, linked in (
        ("anon-iphone", "apple", "iphone", "ab", None),
        ("anon-legacy", "apple", "", "ab", None),
        ("anon-pixel", "android", "android", "fcm", None),
        ("iphone-1", "apple", "iphone", "ab", user.id),
        ("anon-no-token", "apple", "iphone", None, None),
    ):
        apple = platform == "apple"
        db_session.add(
            DeviceRegistration(
                device_id=device_id,
                user_id=f"u-{device_id}",
                platform=platform,
                device_class=device_class,
                pts_token_hex="" if apple else token,
                device_token_hex=token if apple else None,
                bundle_id="",
                attrs_type="",
                apns_env="",
                linked_user_id=linked,
            )
        )
    await db_session.commit()


async def _preview(pool, *, keeps_record: bool, **filt) -> dict:
    response = await preview(_PreviewRequest(keeps_record=keeps_record, **filt), pool=pool)
    return json.loads(response.body)["matched"]


@pytest.mark.asyncio(loop_scope="session")
async def test_a_send_reports_and_stores_every_device_it_reaches(db_session, portal_pool):
    """The toast after a send and the Devices reached tile, which sums each
    send's total, count the anonymous devices a recorded push's preview shows
    too; a popup reaches none of them."""
    await _recipients(db_session)
    expected, queued = {}, {}
    for keeps_record in (True, False):
        kind = "record" if keeps_record else "popup"
        expected[kind] = (
            await _preview(portal_pool, keeps_record=keeps_record, target_classes=["iphone"])
        )["total"]
        response = await send(
            _SendRequest(
                target_classes=["iphone"], title="t", body="b",
                keeps_record=keeps_record, force_ring=False,
            ),
            _REQUEST,
            pool=portal_pool,
        )
        queued[kind] = json.loads(response.body)["queued"]

    rows = await portal_pool.fetch("SELECT kind, total FROM custom_push_sends")

    assert {r["kind"]: r["total"] for r in rows} == expected
    assert queued == expected
    assert expected["record"] > expected["popup"]


@pytest.mark.asyncio(loop_scope="session")
async def test_the_preview_counts_the_anonymous_devices_only_a_recorded_push_reaches(
    db_session, portal_pool
):
    """tigerduck-backend#96: the bulletin dispatcher also sends a recorded
    push to the anonymous devices its filter matches; a popup goes to
    signed-in devices only."""
    await _recipients(db_session)
    classes = {"target_classes": ["iphone", "android"]}

    assert await _preview(portal_pool, keeps_record=True, **classes) == {
        "iphone": 3,
        "android": 1,
        "total": 4,
    }
    assert await _preview(portal_pool, keeps_record=False, **classes) == {
        "iphone": 1,
        "android": 0,
        "total": 1,
    }


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "filt",
    [
        {"target_classes": ["iphone", "ipad", "mac", "android", "android_tablet"]},
        {"target_classes": ["ipad"]},
        {"target_classes": ["iphone"], "device_id": "anon-iphone"},
        {"target_classes": ["iphone", "android"], "user_id": "u-anon-pixel"},
    ],
    ids=["all", "ipad", "device", "user"],
)
async def test_the_anonymous_count_agrees_with_the_dispatcher(
    db_session, portal_pool, filt
):
    """The portal cannot import `server.push.custom_push_targeting`, which
    resolves the anonymous half of a recorded push, so it repeats its
    filter."""
    await _recipients(db_session)
    record = await _preview(portal_pool, keeps_record=True, **filt)
    popup = await _preview(portal_pool, keeps_record=False, **filt)

    anonymous = {key: count - popup.get(key, 0) for key, count in record.items()}

    assert anonymous == await count_by_class(db_session, TargetFilter(**filt))


async def _iphone(db_session, student_id: str) -> UserDevice:
    """A signed-in iPhone with an active token."""
    user = User(student_id=student_id)
    db_session.add(user)
    await db_session.flush()
    phone = UserDevice(
        user_id=user.id,
        client_device_id=f"iphone-{student_id}",
        platform="ios",
        device_class="iphone",
    )
    db_session.add(phone)
    await db_session.flush()
    db_session.add(
        DevicePushToken(
            device_id=phone.id,
            provider="apns",
            token_kind="standard",
            token_hash=f"iphone-{student_id}",
            token_value="tok",
            status="active",
        )
    )
    return phone


async def _course_takers(db_session) -> list[UserDevice]:
    """Three published terms, the middle one current, and four students
    with an iPhone each: two take CS2001301 this term, one dropped it and
    one took it last term. The anonymous iPhones from `_recipients` take
    nothing."""
    await _recipients(db_session)
    today = date.today()
    db_session.add_all(
        [
            SemesterTerm(
                code="1142",
                start_date=today - timedelta(days=200),
                end_date=today - timedelta(days=100),
            ),
            SemesterTerm(
                code="1151",
                start_date=today - timedelta(days=30),
                end_date=today + timedelta(days=60),
            ),
            SemesterTerm(
                code="1152",
                start_date=today + timedelta(days=90),
                end_date=today + timedelta(days=180),
            ),
        ]
    )
    phones = []
    for student, term, status in (
        ("s1", "1151", "enrolled"),
        ("s2", "1151", "enrolled"),
        ("s3", "1151", "dropped"),
        ("s4", "1142", "enrolled"),
    ):
        phone = await _iphone(db_session, student)
        db_session.add(
            UserCourse(
                user_id=phone.user_id,
                semester=term,
                course_key="CS2001301",
                course_no="CS2001301",
                course_name="資料結構",
                enrollment_status=status,
            )
        )
        phones.append(phone)
    await db_session.commit()
    return phones


@pytest.mark.asyncio(loop_scope="session")
async def test_a_course_code_finds_its_takers_in_the_current_term(
    db_session, portal_pool
):
    await _course_takers(db_session)

    response = await preview(
        _PreviewRequest(
            target_classes=["iphone"], keeps_record=True, course_code=" cs2001301 "
        ),
        pool=portal_pool,
    )
    body = json.loads(response.body)

    assert body["term"] == await current_term_code(db_session, datetime.now(UTC))
    assert body["term"] == "1151"
    assert body["course"] == {"code": "CS2001301", "name": "資料結構", "students": 2}
    # The two takers' iPhones; anonymous devices take no course.
    assert body["matched"] == {"iphone": 2, "total": 2}
    assert body["anonymous"] == {"iphone": 0, "total": 0}


@pytest.mark.asyncio(loop_scope="session")
async def test_a_course_nobody_takes_this_term_is_a_miss(db_session, portal_pool):
    await _course_takers(db_session)

    response = await preview(
        _PreviewRequest(target_classes=["iphone"], course_code="CS9999301"),
        pool=portal_pool,
    )
    body = json.loads(response.body)

    assert body["course"] is None
    assert body["matched"]["total"] == 0
    with pytest.raises(HTTPException) as refused:
        await send(
            _SendRequest(
                target_classes=["iphone"],
                course_code="CS9999301",
                title="停課通知",
                body="明天停課",
                keeps_record=False,
                force_ring=False,
            ),
            _REQUEST,
            pool=portal_pool,
        )
    assert refused.value.status_code == 422
    assert await portal_pool.fetchval("SELECT count(*) FROM custom_push_sends") == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_a_course_push_goes_to_its_takers_devices_only(db_session, portal_pool):
    phones = await _course_takers(db_session)

    response = await send(
        _SendRequest(
            target_classes=["iphone", "android"],
            course_code="CS2001301",
            title="停課通知",
            body="明天停課",
            keeps_record=False,
            force_ring=False,
        ),
        _REQUEST,
        pool=portal_pool,
    )

    assert json.loads(response.body)["matched"] == 2
    jobs = await portal_pool.fetch("SELECT device_id FROM push_jobs")
    assert {row["device_id"] for row in jobs} == {phones[0].id, phones[1].id}
    assert await portal_pool.fetchval("SELECT count(*) FROM bulletins") == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_a_course_push_cannot_keep_a_record(db_session, portal_pool):
    """A recorded push becomes a bulletin every user's feed lists, which a
    push meant for one course's students must not."""
    await _course_takers(db_session)

    with pytest.raises(HTTPException) as refused:
        await send(
            _SendRequest(
                target_classes=["iphone"],
                course_code="CS2001301",
                title="停課通知",
                body="明天停課",
                keeps_record=True,
                force_ring=False,
            ),
            _REQUEST,
            pool=portal_pool,
        )

    assert refused.value.status_code == 422
    assert refused.value.detail == "a course push cannot keep a record"
    assert await portal_pool.fetchval("SELECT count(*) FROM bulletins") == 0
    assert await portal_pool.fetchval("SELECT count(*) FROM push_jobs") == 0
    assert await portal_pool.fetchval("SELECT count(*) FROM custom_push_sends") == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_a_course_push_is_audited_with_its_course_and_term(
    db_session, portal_pool
):
    await _course_takers(db_session)

    await send(
        _SendRequest(
            target_classes=["iphone"],
            course_code="CS2001301",
            title="停課通知",
            body="明天停課",
            keeps_record=False,
            force_ring=True,
        ),
        _REQUEST,
        pool=portal_pool,
    )

    row = await portal_pool.fetchrow(
        "SELECT actor, action, target_user_id, detail FROM admin_audit_log"
    )
    detail = json.loads(row["detail"])
    assert (row["actor"], row["action"]) == ("ops@example.com", "push.custom.send")
    assert row["target_user_id"] is None
    assert detail["course_code"] == "CS2001301"
    assert detail["term"] == "1151"
    assert detail["matched"] == 2
