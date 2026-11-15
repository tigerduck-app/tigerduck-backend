"""Review items: what one extraction asks of `course_changes`, per course
code, and the make-up classes and exams it shows.

A course change becomes an add item, or a replace item when the
announcement already has an applied change of that kind on its day. An
applied change of the announcement that the extraction no longer states, or
whose day this or a later announcement of the course holds as usual,
becomes a remove item. A code no student has class times for is listed as
skipped. An item whose days have all passed in Taipei is past, at
extraction or by the daily pass.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.academic_calendar.models import CourseChange
from server.academic_calendar.terms import SCHOOL_TZ
from server.course_announcements.apply import apply_items
from server.course_announcements.checks import (
    Announcement,
    Statement,
    change_payload,
    check_change,
    contradiction,
    course_statements,
    days_of,
    has_schedule,
    lock_courses,
    passes,
    stored_payload,
)
from server.course_announcements.models import (
    CourseAnnouncement,
    CourseAnnouncementExtraction,
    CourseAnnouncementItem,
    ItemAction,
    ItemKind,
    ItemState,
)
from server.db import in_array, session_scope

# Fields a stored change keeps that an extraction never states.
_UNSTATED = ("note_en",)


async def record_items(
    session: AsyncSession,
    extraction: CourseAnnouncementExtraction,
    announcement: CourseAnnouncement,
    output: Mapping[str, Any],
    now: datetime,
    *,
    auto_apply: bool,
) -> list[CourseAnnouncementItem]:
    """Store the items of `output`, the stored result of `extraction`, with
    their checks, and with `auto_apply` write its add items that may apply.
    Takes the lock of every course code the announcement lists, which the
    caller's transaction holds until it ends."""
    term = announcement.term_code
    codes = sorted(set(announcement.course_codes))
    term_days = await lock_courses(session, term, codes)
    statements = await course_statements(session, term, codes)
    facts = _unique((change_payload(fact), fact["evidence"]) for fact in output["course_changes"])
    held = [(date.fromisoformat(h["date"]), h["evidence"]) for h in output["held_as_usual"]]
    context = Announcement(
        text=announcement.text or "",
        labels=announcement.labels or [],
        group_id=announcement.group_id,
        posted_at=announcement.posted_at,
        visible_from=announcement.visible_from,
        llm_labels=output["labels"],
        certain=output["certain"],
    )
    removing = set(
        await session.scalars(
            select(CourseAnnouncementItem.course_change_id).where(
                in_array(CourseAnnouncementItem.course_no, codes),
                CourseAnnouncementItem.action == ItemAction.remove.value,
                CourseAnnouncementItem.state == ItemState.pending.value,
                CourseAnnouncementItem.course_change_id.is_not(None),
            )
        )
    )
    items: list[CourseAnnouncementItem] = []
    for code in codes:
        if not await has_schedule(session, term, code):
            if facts:
                items.append(_item(extraction, code, state=ItemState.skipped))
            continue
        sourced = await _sourced_changes(session, term, code)
        own = [change for change, _ in sourced if change.source_announcement_id == announcement.id]
        for payload, evidence in facts:
            current = next((c for c in own if _covers(c, payload)), None)
            if current is not None and _same(stored_payload(current), payload):
                continue
            checks = await check_change(
                session,
                term=term,
                term_days=term_days,
                course_no=code,
                payload=payload,
                evidence=evidence,
                announcement=context,
                editing=current and current.id,
            )
            checks["contradiction"] = contradiction(
                course_no=code,
                payload=payload,
                announcement_id=announcement.id,
                posted_at=announcement.posted_at,
                statements=statements,
            )
            items.append(
                _item(
                    extraction,
                    code,
                    action=ItemAction.replace if current else ItemAction.add,
                    payload=payload,
                    change=current,
                    evidence=evidence,
                    checks=checks,
                )
            )
        for change, source_posted_at in sourced:
            reason = _removal(
                change,
                source_posted_at,
                announcement=announcement,
                code=code,
                facts=[payload for payload, _ in facts],
                held=held,
                statements=statements,
            )
            if reason is None or change.id in removing:
                continue
            removing.add(change.id)
            items.append(
                _item(
                    extraction,
                    code,
                    action=ItemAction.remove,
                    payload=stored_payload(change),
                    change=change,
                    evidence=reason or None,
                )
            )
    for kind, key in ((ItemKind.makeup, "makeup_classes"), (ItemKind.exam, "exams")):
        for fact in output[key]:
            payload = {field: value for field, value in fact.items() if field != "evidence"}
            items.append(
                _item(extraction, None, kind=kind, state=ItemState.info, payload=payload,
                      evidence=fact["evidence"])
            )
    today = now.astimezone(SCHOOL_TZ).date()
    for item in items:
        if item.state != ItemState.skipped.value and _last_day(item.payload) is not None:
            if _last_day(item.payload) < today:
                item.state = ItemState.past.value
    session.add_all(items)
    await session.flush()
    if auto_apply:
        await apply_items(session, announcement, context, items, term_days=term_days, now=now)
    return items


async def refresh_contradictions(
    session: AsyncSession, term: str, course_nos: Iterable[str]
) -> None:
    """Mark again, against the announcements now stored and extracted, every
    pending add or replace item of the term's `course_nos`."""
    codes = sorted(set(course_nos))
    rows = (
        await session.execute(
            select(CourseAnnouncementItem, CourseAnnouncement.id, CourseAnnouncement.posted_at)
            .join(
                CourseAnnouncementExtraction,
                CourseAnnouncementExtraction.id == CourseAnnouncementItem.extraction_id,
            )
            .join(
                CourseAnnouncement,
                CourseAnnouncement.id == CourseAnnouncementExtraction.announcement_id,
            )
            .where(
                CourseAnnouncement.term_code == term,
                in_array(CourseAnnouncementItem.course_no, codes),
                CourseAnnouncementItem.state == ItemState.pending.value,
                CourseAnnouncementItem.action.in_(
                    [ItemAction.add.value, ItemAction.replace.value]
                ),
            )
            .order_by(CourseAnnouncementItem.id)
            # An item another transaction holds is being superseded or decided
            # there; waiting for it could deadlock with a store that locked
            # items in another order.
            .with_for_update(of=CourseAnnouncementItem, skip_locked=True)
        )
    ).all()
    if not rows:
        return
    statements = await course_statements(session, term, codes)
    for item, announcement_id, posted_at in rows:
        mark = _mark(item, announcement_id, posted_at, statements)
        checks = dict(item.checks or {})
        if checks.get("contradiction") != mark:
            checks["contradiction"] = mark
            item.checks = checks
            item.passes_checks = passes(checks)


async def mark_past_items(
    session_factory: async_sessionmaker[AsyncSession], now: datetime
) -> int:
    """Mark past every waiting or informational item whose days have all
    passed in Taipei. Returns how many."""
    last_day = cast(
        func.coalesce(
            CourseAnnouncementItem.payload["end_date"].astext,
            CourseAnnouncementItem.payload["date"].astext,
        ),
        Date,
    )
    async with session_scope(session_factory) as session:
        result = await session.execute(
            update(CourseAnnouncementItem)
            .where(
                CourseAnnouncementItem.state.in_(
                    [ItemState.pending.value, ItemState.info.value]
                ),
                last_day < now.astimezone(SCHOOL_TZ).date(),
            )
            .values(state=ItemState.past.value)
        )
    return result.rowcount or 0


def _mark(
    item: CourseAnnouncementItem,
    announcement_id: str,
    posted_at: datetime,
    statements: Sequence[Statement],
) -> dict[str, Any]:
    return contradiction(
        course_no=item.course_no,
        payload=item.payload,
        announcement_id=announcement_id,
        posted_at=posted_at,
        statements=statements,
    )


def _item(
    extraction: CourseAnnouncementExtraction,
    course_no: str | None,
    *,
    kind: ItemKind = ItemKind.course_change,
    action: ItemAction | None = None,
    state: ItemState = ItemState.pending,
    payload: dict[str, Any] | None = None,
    change: CourseChange | None = None,
    evidence: str | None = None,
    checks: dict[str, Any] | None = None,
) -> CourseAnnouncementItem:
    return CourseAnnouncementItem(
        extraction_id=extraction.id,
        course_no=course_no,
        kind=kind.value,
        action=action and action.value,
        payload=payload or {},
        course_change_id=change and change.id,
        evidence=evidence,
        checks=checks,
        passes_checks=checks is not None and passes(checks),
        state=state.value,
    )


def _unique(
    facts: Iterable[tuple[dict[str, Any], str]],
) -> list[tuple[dict[str, Any], str]]:
    """The facts, a change stated twice kept once."""
    seen: dict[str, tuple[dict[str, Any], str]] = {}
    for payload, evidence in facts:
        seen.setdefault(json.dumps(payload, sort_keys=True), (payload, evidence))
    return list(seen.values())


async def _sourced_changes(
    session: AsyncSession, term: str, course_no: str
) -> list[tuple[CourseChange, datetime]]:
    """The course's changes taken from an announcement, each with that
    announcement's post time."""
    rows = await session.execute(
        select(CourseChange, CourseAnnouncement.posted_at)
        .join(CourseAnnouncement, CourseAnnouncement.id == CourseChange.source_announcement_id)
        .where(CourseChange.term_code == term, CourseChange.course_no == course_no)
        .order_by(CourseChange.id)
    )
    return [(change, posted_at) for change, posted_at in rows.all()]


def _covers(change: CourseChange, payload: Mapping[str, Any]) -> bool:
    first, _ = days_of(payload)
    return change.kind == payload["kind"] and change.start_date <= first <= change.end_date


def _same(stored: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    return all(stored[field] == payload[field] for field in payload if field not in _UNSTATED)


def _removal(
    change: CourseChange,
    source_posted_at: datetime,
    *,
    announcement: CourseAnnouncement,
    code: str,
    facts: Sequence[Mapping[str, Any]],
    held: Sequence[tuple[date, str]],
    statements: Sequence[Statement],
) -> str | None:
    """Why `change` should be removed: the evidence of a day held as usual,
    "" when the announcement no longer states it, None to keep it."""
    own = change.source_announcement_id == announcement.id

    def held_on(day: date) -> bool:
        return change.start_date <= day <= change.end_date

    if own or source_posted_at < announcement.posted_at:
        for day, evidence in held:
            if held_on(day):
                return evidence
    if not own:
        return None
    if not any(_covers(change, payload) for payload in facts):
        return ""
    later = any(
        statement.announcement_id != announcement.id
        and code in statement.course_codes
        and statement.posted_at > announcement.posted_at
        and any(held_on(day) for day in statement.held)
        for statement in statements
    )
    return "" if later else None


def _last_day(payload: Mapping[str, Any]) -> date | None:
    """The last day an item's payload names, None when it names none."""
    day = payload.get("end_date") or payload.get("date")
    return date.fromisoformat(day) if day else None
