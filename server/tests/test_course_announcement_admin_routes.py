"""The admin listener's route that queues an announcement's extraction
again, which the portal's Re-extract calls. Every announcement here is
invented."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from server.academic_calendar.models import SemesterTerm
from server.admin_audit import AdminAuditLog
from server.course_announcements.models import (
    CourseAnnouncement,
    CourseAnnouncementExtraction,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

ACTOR = {"X-Admin-Actor": "ops@example.com"}
POSTED = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


async def _seed(client: AsyncClient) -> None:
    """Terms 1151, current, and 1142, with a sent, a not-sent, an
    unclassified and an earlier term's announcement."""
    async with client.app.state.session_factory() as session:
        session.add_all(
            [
                SemesterTerm(code="1142", start_date=date(2026, 2, 16), end_date=date(2026, 6, 19)),
                SemesterTerm(code="1151", start_date=date(2026, 9, 14), end_date=date(2027, 1, 8)),
            ]
        )
        for key, term, route in (
            ("101-901", "1151", "sent"),
            ("102-902", "1151", "not_sent"),
            ("103-903", "1151", "unclassified"),
            ("104-904", "1142", "sent"),
        ):
            classified = route != "unclassified"
            session.add(
                CourseAnnouncement(
                    id=key, term_code=term, moodle_course_id=7001, course_codes=["CS2001301"],
                    raw_subject=None if classified else "11/17 停課",
                    raw_message=None if classified else "<p>Invented notice</p>",
                    subject="11/17 停課",
                    text="11/17 停課\n\n同學好：11/17 停課。" if classified else None,
                    posted_at=POSTED, modified_at=POSTED, group_id=-1, has_attachment=False,
                    classifier_release="model-test" if classified else None,
                    scores={"class_cancel": 0.97} if classified else None,
                    labels=["class_cancel"] if classified else None,
                    route=route,
                )
            )
        await session.commit()


async def _queued(client: AsyncClient) -> list[tuple[str, datetime, str]]:
    async with client.app.state.session_factory() as session:
        rows = await session.execute(
            select(
                CourseAnnouncementExtraction.announcement_id,
                CourseAnnouncementExtraction.version,
                CourseAnnouncementExtraction.status,
            ).order_by(CourseAnnouncementExtraction.id)
        )
        return [tuple(r) for r in rows]


async def _audit(client: AsyncClient) -> list[tuple[str, str, dict]]:
    async with client.app.state.session_factory() as session:
        rows = (await session.execute(select(AdminAuditLog).order_by(AdminAuditLog.id))).scalars()
        return [(r.actor, r.action, r.detail) for r in rows]


async def test_queues_a_current_term_announcement_once(admin_client) -> None:
    await _seed(admin_client)

    first = await admin_client.post("/admin/course-announcements/101-901/extract", headers=ACTOR)
    again = await admin_client.post("/admin/course-announcements/101-901/extract", headers=ACTOR)

    assert first.status_code == 200
    assert again.status_code == 409
    assert again.json()["detail"] == "an extraction of this version is already waiting"
    assert await _queued(admin_client) == [("101-901", POSTED, "queued")]
    assert await _audit(admin_client) == [
        ("ops@example.com", "course_announcement.reextract", {"announcement_id": "101-901"})
    ]


@pytest.mark.parametrize(
    ("key", "code", "detail"),
    [
        ("102-902", 409, "the announcement is not sent for extraction"),
        ("103-903", 409, "the announcement is not sent for extraction"),
        ("104-904", 409, "the announcement is not of the current term"),
        ("1-1", 404, "announcement not found"),
    ],
)
async def test_refuses_what_the_route_rule_does_not_send(admin_client, key, code, detail) -> None:
    await _seed(admin_client)

    resp = await admin_client.post(f"/admin/course-announcements/{key}/extract", headers=ACTOR)

    assert (resp.status_code, resp.json()["detail"]) == (code, detail)
    assert await _queued(admin_client) == []
    assert await _audit(admin_client) == []


async def test_needs_the_operator(admin_client) -> None:
    await _seed(admin_client)

    resp = await admin_client.post("/admin/course-announcements/101-901/extract")

    assert resp.status_code == 422
    assert await _queued(admin_client) == []


async def test_the_api_port_does_not_serve_it(client) -> None:
    resp = await client.post("/admin/course-announcements/101-901/extract", headers=ACTOR)

    assert resp.status_code == 404
