"""Course announcements: the first post of each discussion in a Moodle
course's news forum, as the backend stores, classifies and extracts them
(models: server/course_announcements/models.py), and the review items an
operator approves or rejects.

Announcement text may name students, so every read is audited as a list of
students and the page keeps the text hidden until the operator shows it.
Approving an item writes through the course-changes write path
(course_changes.py), which checks it as an operator's own change.
"""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse

from ..audit import audit
from ..db import get_pool
from .course_changes import (
    _UNHELD,
    BIGINT_MAX,
    _bad,
    add_change,
    is_id,
    item_body,
    pending_item,
    remove_change,
    replace_change,
)
from .moodle.push import forward_to_backend

router = APIRouter(prefix="/api/course-announcements")

PER_PAGE = 10
EXTRACTION_FILTERS = ("all", "extracted", "failed", "differs")
_REASON_MAX = 500
# strpos runs over every listed row, so a search stays short.
_SEARCH_MAX = 200
_ITEM_COUNTS = ("pending", "applied", "rejected", "removed", "past", "exams")
_TERM = "SELECT 1 FROM semester_terms WHERE code = $1"
# "{discussion id}-{first post id}"; anything else could rewrite the forwarded path.
_ANNOUNCEMENT_ID = re.compile(r"[0-9]{1,20}-[0-9]{1,20}")

_REVIEW = """
SELECT i.id, i.course_no, i.kind, i.action, i.payload, i.evidence, i.checks,
       i.passes_checks, i.course_change_id, i.state,
       a.id AS announcement_id, a.subject, a.posted_at
FROM course_announcement_items AS i
JOIN course_announcement_extractions AS e ON e.id = i.extraction_id
JOIN course_announcements AS a ON a.id = e.announcement_id
WHERE a.term_code = $1 AND i.state = 'pending'
ORDER BY a.posted_at DESC, i.id
"""

_REVIEW_COUNTS = """
SELECT count(*) FILTER (WHERE i.state IN ('approved', 'rejected')) AS decided,
       (SELECT max(updated_at) FROM course_announcement_courses WHERE term_code = $1)
           AS polled_at
FROM course_announcement_items AS i
JOIN course_announcement_extractions AS e ON e.id = i.extraction_id
JOIN course_announcements AS a ON a.id = e.announcement_id
WHERE a.term_code = $1
"""

# Each announcement of the term with its newest extraction, its newest
# finished one and what became of that one's items. `differs`: that finished
# extraction and the one before it of the same version come from different
# models and hold different facts.
_LISTED = """
WITH listed AS (
    SELECT a.id, a.course_codes, a.subject, a.text, a.posted_at, a.labels, a.scores,
           a.route, latest.status AS extraction, done.id AS done_id,
           prev.id IS NOT NULL AND prev.model IS DISTINCT FROM done.model
               AND prev.raw_output IS DISTINCT FROM done.raw_output AS differs,
           coalesce(n.pending, 0) AS pending, coalesce(n.applied, 0) AS applied,
           coalesce(n.rejected, 0) AS rejected, coalesce(n.removed, 0) AS removed,
           coalesce(n.past, 0) AS past, coalesce(n.exams, 0) AS exams
    FROM course_announcements AS a
    LEFT JOIN LATERAL (
        SELECT e.status FROM course_announcement_extractions AS e
        WHERE e.announcement_id = a.id ORDER BY e.id DESC LIMIT 1
    ) AS latest ON true
    LEFT JOIN LATERAL (
        SELECT e.id, e.version, e.model, e.raw_output
        FROM course_announcement_extractions AS e
        WHERE e.announcement_id = a.id AND e.status = 'done' ORDER BY e.id DESC LIMIT 1
    ) AS done ON true
    LEFT JOIN LATERAL (
        SELECT e.id, e.model, e.raw_output FROM course_announcement_extractions AS e
        WHERE e.announcement_id = a.id AND e.status = 'done'
          AND e.version = done.version AND e.id < done.id
        ORDER BY e.id DESC LIMIT 1
    ) AS prev ON true
    LEFT JOIN LATERAL (
        SELECT count(*) FILTER (WHERE i.state = 'pending') AS pending,
               count(*) FILTER (WHERE i.state IN ('applied', 'approved', 'edited')) AS applied,
               count(*) FILTER (WHERE i.state = 'rejected') AS rejected,
               count(*) FILTER (WHERE i.state = 'removed') AS removed,
               count(*) FILTER (WHERE i.state = 'past') AS past,
               count(*) FILTER (WHERE i.kind = 'exam') AS exams
        FROM course_announcement_items AS i WHERE i.extraction_id = done.id
    ) AS n ON true
    WHERE a.term_code = $1
      AND ($2::text IS NULL
           OR strpos(lower(a.subject), lower($2)) > 0
           OR EXISTS (
               SELECT 1 FROM unnest(a.course_codes) AS code
               WHERE strpos(lower(code), lower($2)) > 0
           )
           OR EXISTS (
               SELECT 1 FROM term_courses AS t
               WHERE t.semester = a.term_code AND t.course_no = ANY (a.course_codes)
                 AND (strpos(lower(t.course_name), lower($2)) > 0
                      OR strpos(lower(coalesce(t.course_name_en, '')), lower($2)) > 0)
           ))
)
"""

_PAGE = _LISTED + """
SELECT * FROM listed
WHERE $3 = 'all'
   OR ($3 = 'extracted' AND done_id IS NOT NULL)
   OR ($3 = 'failed' AND extraction = 'failed')
   OR ($3 = 'differs' AND differs)
ORDER BY posted_at DESC, id DESC
LIMIT $4 OFFSET $5
"""

_COUNTS = _LISTED + """
SELECT count(*) AS all, count(done_id) AS extracted,
       count(*) FILTER (WHERE extraction = 'failed') AS failed,
       count(*) FILTER (WHERE differs) AS differs
FROM listed
"""


def _iso(value) -> str | None:
    return value and value.isoformat()


def _labels(row) -> list[dict]:
    scores = json.loads(row["scores"]) if row["scores"] else {}
    return sorted(
        ({"label": label, "score": scores.get(label)} for label in row["labels"] or ()),
        key=lambda label: -(label["score"] or 0),
    )


def _item_to_dict(row) -> dict:
    checks = json.loads(row["checks"]) if row["checks"] else None
    return {
        "id": row["id"],
        "course_no": row["course_no"],
        "kind": row["kind"],
        "action": row["action"],
        "payload": json.loads(row["payload"]),
        "evidence": row["evidence"],
        "checks": checks,
        "passes_checks": row["passes_checks"],
        # The change a replace or remove item acts on or an item wrote, or
        # for a pending add, the first it clashed with when extracted.
        "existing_change_id": (
            next(iter((checks or {}).get("rules", {}).get("change_ids") or ()), None)
            if (row["state"], row["action"]) == ("pending", "add")
            else row["course_change_id"]
        ),
    }


@router.get("/pending")
async def pending_count(pool=Depends(get_pool)) -> JSONResponse:
    """How many items wait for review, for the navigation."""
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM course_announcement_items WHERE state = 'pending'"
        )
    return JSONResponse(content={"pending": count})


@router.get("/terms/{term}/review")
async def list_reviews(term: str, request: Request, pool=Depends(get_pool)) -> JSONResponse:
    """The term's pending items, newest announcement first."""
    async with pool.acquire() as conn:
        if not await conn.fetchval(_TERM, term):
            return _bad("term not found", 404)
        rows = await conn.fetch(_REVIEW, term)
        counts = await conn.fetchrow(_REVIEW_COUNTS, term)
        items = [
            {
                **_item_to_dict(r),
                "announcement_id": r["announcement_id"],
                "subject": r["subject"],
                "posted_at": r["posted_at"].isoformat(),
            }
            for r in rows
        ]
        await audit(conn, request, "user.list", count=len(rows))
    return JSONResponse(
        content={
            "term": term,
            "auto_apply": request.app.state.settings.course_announcements_auto_apply,
            "polled_at": _iso(counts["polled_at"]),
            "decided": counts["decided"],
            "items": items,
        }
    )


@router.get("/terms/{term}/announcements")
async def list_announcements(
    term: str,
    request: Request,
    pool=Depends(get_pool),
    q: str | None = None,
    extraction: str = "all",
    page: int = 1,
) -> JSONResponse:
    """A page of the term's announcements, newest first, with the count each
    extraction filter would list."""
    if extraction not in EXTRACTION_FILTERS:
        return _bad(f"extraction must be one of {', '.join(EXTRACTION_FILTERS)}")
    if not 1 <= page <= BIGINT_MAX // PER_PAGE:
        return _bad("page out of range")
    if q and (len(q) > _SEARCH_MAX or _UNHELD.search(q)):
        return _bad(f"q must be at most {_SEARCH_MAX} characters of text")
    search = q.strip() if q and q.strip() else None
    async with pool.acquire() as conn:
        if not await conn.fetchval(_TERM, term):
            return _bad("term not found", 404)
        counts = await conn.fetchrow(_COUNTS, term, search)
        rows = await conn.fetch(
            _PAGE, term, search, extraction, PER_PAGE, (page - 1) * PER_PAGE
        )
        await audit(conn, request, "user.list", count=len(rows))
    return JSONResponse(
        content={
            "term": term,
            "page": page,
            "per_page": PER_PAGE,
            "counts": dict(counts),
            "announcements": [
                {
                    "id": r["id"],
                    "course_codes": list(r["course_codes"]),
                    "subject": r["subject"],
                    # The text is the title, a blank line and the body.
                    "excerpt": r["text"] and r["text"].partition("\n\n")[2][:200],
                    "posted_at": r["posted_at"].isoformat(),
                    "labels": _labels(r),
                    "route": r["route"],
                    "extraction": r["extraction"],
                    "extracted": r["done_id"] is not None,
                    "differs": r["differs"],
                    "items": {k: r[k] for k in _ITEM_COUNTS},
                }
                for r in rows
            ],
        }
    )


@router.get("/announcements/{announcement_id}")
async def get_announcement(
    announcement_id: str, request: Request, pool=Depends(get_pool)
) -> JSONResponse:
    """One announcement with its classification, its newest extraction and
    the items of its newest finished one."""
    async with pool.acquire() as conn:
        a = await conn.fetchrow("SELECT * FROM course_announcements WHERE id = $1", announcement_id)
        if a is None:
            return _bad("announcement not found", 404)
        latest = await conn.fetchrow(
            "SELECT status, model, certain, attempts, error_code, updated_at"
            " FROM course_announcement_extractions WHERE announcement_id = $1"
            " ORDER BY id DESC LIMIT 1",
            announcement_id,
        )
        done = await conn.fetchrow(
            "SELECT id, model, certain FROM course_announcement_extractions"
            " WHERE announcement_id = $1 AND status = 'done' ORDER BY id DESC LIMIT 1",
            announcement_id,
        )
        rows = done and await conn.fetch(
            "SELECT * FROM course_announcement_items WHERE extraction_id = $1 ORDER BY id",
            done["id"],
        )
        items = [
            {
                **_item_to_dict(r),
                "state": r["state"],
                "decided_by": r["decided_by"],
                "decided_at": _iso(r["decided_at"]),
                "reason": r["reason"],
            }
            for r in rows or ()
            if r["state"] != "skipped"
        ]
        await audit(conn, request, "user.list", count=1)
    discussion = announcement_id.partition("-")[0]
    return JSONResponse(
        content={
            "id": a["id"],
            "term": a["term_code"],
            "course_codes": list(a["course_codes"]),
            "subject": a["subject"],
            "text": a["text"],
            "posted_at": a["posted_at"].isoformat(),
            "modified_at": a["modified_at"].isoformat(),
            "group_id": a["group_id"],
            "visible_from": _iso(a["visible_from"]),
            "has_attachment": a["has_attachment"],
            "classifier_release": a["classifier_release"],
            "scores": json.loads(a["scores"]) if a["scores"] else None,
            "labels": list(a["labels"] or ()),
            "route": a["route"],
            "extraction": latest and {
                "status": latest["status"],
                "model": latest["model"],
                "attempts": latest["attempts"],
                "error_code": latest["error_code"],
                "updated_at": latest["updated_at"].isoformat(),
            },
            "model": done and done["model"],
            "certain": done and done["certain"],
            "items": items,
            # The codes no student had a schedule for, so no item was made.
            "skipped_codes": [r["course_no"] for r in rows or () if r["state"] == "skipped"],
            "moodle_url": (
                f"{request.app.state.settings.moodle_base_url}/mod/forum/discuss.php?d={discussion}"
            ),
        }
    )


@router.post("/items/{item_id}/approve")
async def approve_item(item_id: int, request: Request, pool=Depends(get_pool)) -> JSONResponse:
    """Adds, replaces or removes the item's change as the Course changes page
    would, with its announcement as the source."""
    actor = request.state.actor
    if not is_id(item_id):
        return _bad("item not found", 404)
    async with pool.acquire() as conn, conn.transaction():
        # Payload and links never change once an item is stored; the locks
        # come inside the write, items before the change they name.
        item = await conn.fetchrow(
            "SELECT i.id, i.course_no, i.kind, i.action, i.payload, i.course_change_id"
            " FROM course_announcement_items AS i WHERE i.id = $1",
            item_id,
        )
        if item is None:
            return _bad("item not found", 404)
        change_id = item["course_change_id"]
        if item["action"] == "add":
            locked = await pending_item(conn, item_id)
            if isinstance(locked, JSONResponse):
                return locked
            written = await add_change(
                conn, actor, locked["term_code"], item["course_no"], item_body(item), locked
            )
            action = "course_change.create"
        elif item["action"] == "replace" and change_id is not None:
            written = await replace_change(conn, actor, change_id, item_body(item), item_id)
            action = "course_change.update"
        elif item["action"] == "remove" and change_id is not None:
            written = await remove_change(conn, actor, change_id, item_id)
            action = "course_change.delete"
        else:
            return _bad("the item has no change to approve", 409)
        if isinstance(written, JSONResponse):
            return written
        change_id = change_id or written["id"]
        await audit(
            conn, request, action,
            change_id=change_id, term=written["term_code"], course_no=item["course_no"],
            kind=written["kind"], item_id=item_id,
        )
    return JSONResponse(content={"id": item_id, "state": "approved", "change_id": change_id})


@router.post("/items/{item_id}/reject")
async def reject_item(
    item_id: int, request: Request, pool=Depends(get_pool), body: dict = Body(...)
) -> JSONResponse:
    """Rejects a pending item, keeping the operator's reason with it."""
    if not is_id(item_id):
        return _bad("item not found", 404)
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        return _bad("reason must be text")
    reason = reason.strip() or None if reason else None
    if reason and (len(reason) > _REASON_MAX or _UNHELD.search(reason)):
        return _bad(f"reason must be at most {_REASON_MAX} characters of text")
    async with pool.acquire() as conn, conn.transaction():
        item = await pending_item(conn, item_id)
        if isinstance(item, JSONResponse):
            return item
        await conn.execute(
            "UPDATE course_announcement_items SET state = 'rejected', reason = $2,"
            " decided_by = $3, decided_at = now(), updated_at = now() WHERE id = $1",
            item_id,
            reason,
            request.state.actor,
        )
        await audit(conn, request, "course_announcement.reject", item_id=item_id)
    return JSONResponse(content={"id": item_id, "state": "rejected"})


@router.post("/announcements/{announcement_id}/reextract")
async def reextract(announcement_id: str, request: Request) -> JSONResponse:
    """Queues the stored version for extraction again, through the backend's
    admin listener, which queues it as the poller does."""
    if not _ANNOUNCEMENT_ID.fullmatch(announcement_id):
        return _bad("announcement not found", 404)
    return await forward_to_backend(
        request, "POST", f"/admin/course-announcements/{announcement_id}/extract"
    )
