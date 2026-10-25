"""The daily refresh of the course catalog from NTUST's public course query
(querycourse.ntust.edu.tw), measured in tigerduck-app#547.

The school has no whole-semester call, and it answers an empty filter with
HTTP 500 after 120 s, so `course_query` refuses to build one. A semester is
fetched as one CourseNo prefix query per first character, in zh and in en,
one request at a time. 3 is split into 3N and 3T, the alliance courses,
since as one query it would take over a minute; a course number starting
with any other 3x would be missed. A semester is stored only
when every request it needed succeeded; otherwise its last complete
snapshot stays.

Each course row keeps the fields the app's `CourseSearchResult` decodes
(`_FIELDS`) and adds three flags the device filters on, derived here:

* `IsIntensive`: the course number is among the rows an `OnlyIntensive=1`
  query returns. Nothing in a row says so.
* `IsAlliance`: the course number starts with 3 (3N NTNU, 3T NTU), the rows
  the school's `OnleyNTUST` filter leaves out. TU numbers are the school's own.
* `IsEnglishTaught`: the zh row's Contents holds `ENGLISH_TAUGHT_MARK`, which
  is what the school's `CourseNotes=eng` filter matches. The en rows take it
  from the zh rows, since their Contents is translated.

The rules match the school's own filters over semester 1151; a change in
how it numbers courses or words Contents breaks them.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import string
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.academic_calendar.terms import current_term_code, is_term_code
from server.course_catalog.models import CourseCatalogSnapshot
from server.db import session_scope

logger = structlog.get_logger(__name__)

BASE_URL = "https://querycourse.ntust.edu.tw/querycourse/api"
USER_AGENT = "TigerDuckCatalogBot/0.1"
LANGUAGES = ("zh", "en")
PREFIXES = (*string.ascii_uppercase, "0", "1", "2", "3N", "3T", *"456789")
# The current term and the ones listed after it: a summer term and the next
# term at most. A calendar left behind would otherwise widen the sweep.
MAX_SEMESTERS = 3
# The slowest prefix took 37 s; the school itself gives up at 120 s.
REQUEST_TIMEOUT = httpx.Timeout(100.0, connect=10.0)
ATTEMPTS = 3
RETRY_BASE_SECONDS = 10.0
ALLIANCE_PREFIX = "3"
ENGLISH_TAUGHT_MARK = "英語授課"

# CourseSearchResult in tigerduck-app's
# swift/TigerDuck/Services/API/NTUST/NTUSTAPIModels.swift. A row with one of
# the required fields missing fails the app's decode of the whole list.
_REQUIRED_TEXT = ("Semester", "CourseNo", "CourseName", "CourseTeacher", "CreditPoint")
_OPTIONAL_TEXT = (
    "Dimension", "RequireOption", "AllYear", "Restrict1", "Restrict2",
    "CourseTimes", "PracticalTimes", "ClassRoomNo", "Node", "Contents",
)
_FIELDS = (*_REQUIRED_TEXT, *_OPTIONAL_TEXT, "ChooseStudent")

# CourseSearchRequest's filters with nothing set.
_NO_FILTER = {
    "CourseNo": "", "CourseName": "", "CourseTeacher": "", "Dimension": "",
    "CourseNotes": "", "CampusNotes": "", "ForeignLanguage": 0, "OnlyGeneral": 0,
    "OnleyNTUST": 0, "OnlyMaster": 0, "OnlyUnderGraduate": 0, "OnlyNode": 0,
}

# (college, department, dimension) name fields per language.
_NAME_FIELDS = {
    "zh": ("CollegeName", "Department", "DimContent"),
    "en": ("CollegeEngName", "EngDepartment", "EngContent"),
}


class CatalogFetchError(Exception):
    """A request that failed after its retries, or an answer the catalog
    cannot use."""


def course_query(
    semester: str, language: str, *, course_no: str = "", only_intensive: bool = False
) -> dict[str, Any]:
    """A course query body, as the app's CourseSearchRequest sends it, plus
    the site's `OnlyIntensive`. Refuses the empty filter."""
    if not course_no and not only_intensive:
        raise ValueError("the school answers an empty filter with an error after 120 s")
    body = {"Semester": semester, **_NO_FILTER, "CourseNo": course_no, "Language": language}
    if only_intensive:
        body["OnlyIntensive"] = 1
    return body


async def _call(client: httpx.AsyncClient, path: str, body: dict | None = None) -> Any:
    """The JSON answer to one request, retrying a timeout, a connection error,
    a 429 or a 5xx with backoff."""
    error = ""
    for attempt in range(ATTEMPTS):
        if attempt:
            await asyncio.sleep(RETRY_BASE_SECONDS * 2 ** (attempt - 1))
        try:
            if body is None:
                response = await client.get(f"{BASE_URL}/{path}")
            else:
                response = await client.post(f"{BASE_URL}/{path}", json=body)
        except httpx.TransportError as exc:
            error = type(exc).__name__
            continue
        if response.status_code == 429 or response.status_code >= 500:
            error = f"HTTP {response.status_code}"
            continue
        if response.status_code != 200:
            raise CatalogFetchError(f"{path}: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise CatalogFetchError(f"{path}: the answer is not JSON") from None
    raise CatalogFetchError(f"{path}: {error} after {ATTEMPTS} attempts")


def _course_row(raw: Any, semester: str, prefix: str) -> dict[str, Any]:
    if not (
        isinstance(raw, dict)
        and all(isinstance(raw.get(k), str) for k in _REQUIRED_TEXT)
        and all(isinstance(raw.get(k), str | None) for k in _OPTIONAL_TEXT)
        and (raw.get("ChooseStudent") is None or type(raw["ChooseStudent"]) is int)
        and raw["Semester"] == semester
        and raw["CourseNo"].upper().startswith(prefix)
    ):
        raise CatalogFetchError(f"courses {semester} {prefix or 'intensive'}: an unreadable row")
    return {k: raw.get(k) for k in _FIELDS}


async def _courses(
    client: httpx.AsyncClient, semester: str, language: str, **query: Any
) -> list[dict[str, Any]]:
    rows = await _call(client, "courses", course_query(semester, language, **query))
    if not isinstance(rows, list):
        raise CatalogFetchError(f"courses {semester}: the answer is not a list")
    return [_course_row(raw, semester, query.get("course_no", "")) for raw in rows]


async def _target_semesters(client: httpx.AsyncClient, current: str | None) -> list[str]:
    """The current term and the ones the school lists after it, newest
    first. The newest listed term alone while no term is current or the
    school does not list it yet."""
    answer = await _call(client, "semestersinfo")
    try:
        listed = sorted({s["Semester"] for s in answer if is_term_code(s["Semester"])}, reverse=True)
    except (KeyError, TypeError):
        raise CatalogFetchError("semestersinfo: an unreadable answer") from None
    # Term codes sort in time order as strings: 1151 < 1152 < 115H < 1161.
    chosen = [code for code in listed if current and code >= current] or listed[:1]
    return chosen[:MAX_SEMESTERS]


async def _option_lists(client: httpx.AsyncClient) -> dict[str, dict[str, list]]:
    """The colleges with their departments, and the general-education
    dimensions, in each language. The school's dimension 0 is a placeholder
    its site shows as "all" and is left out."""
    dimensions = await _call(client, "dimensions")
    colleges = await _call(client, "Colleges/")
    try:
        departments = {
            c["CollegeNo"]: await _call(client, f"departments?collegeNo={c['CollegeNo']}")
            for c in colleges
        }
        return {
            language: {
                "colleges": [
                    {
                        "no": c["CollegeNo"],
                        "name": c[college],
                        # The school lists some departments twice.
                        "departments": list({
                            d["DeptNo"]: {"no": d["DeptNo"], "name": d[department]}
                            for d in departments[c["CollegeNo"]]
                        }.values()),
                    }
                    for c in colleges
                ],
                "dimensions": [
                    {"no": d["DimNo"], "name": d[dimension]}
                    for d in dimensions
                    if d["DimNo"] != "0"
                ],
            }
            for language, (college, department, dimension) in _NAME_FIELDS.items()
        }
    except (KeyError, TypeError):
        raise CatalogFetchError("option lists: an unreadable answer") from None


async def _fetch_semester(
    client: httpx.AsyncClient, semester: str
) -> dict[str, list[dict[str, Any]]]:
    """Every course of `semester` in each language, flagged."""
    rows: dict[str, list[dict[str, Any]]] = {}
    for language in LANGUAGES:
        rows[language] = []
        for prefix in PREFIXES:
            rows[language] += await _courses(client, semester, language, course_no=prefix)
        if not rows[language]:
            raise CatalogFetchError(f"courses {semester} {language}: no course at all")
    intensive = {
        r["CourseNo"] for r in await _courses(client, semester, "zh", only_intensive=True)
    }
    english_taught = {
        r["CourseNo"] for r in rows["zh"] if ENGLISH_TAUGHT_MARK in (r["Contents"] or "")
    }
    return {
        language: sorted(
            (
                r | {
                    "IsIntensive": r["CourseNo"] in intensive,
                    "IsAlliance": r["CourseNo"].startswith(ALLIANCE_PREFIX),
                    "IsEnglishTaught": r["CourseNo"] in english_taught,
                }
                for r in language_rows
            ),
            # The school's order may vary between fetches; the ETag should not.
            key=lambda r: (r["CourseNo"], r["Node"] or ""),
        )
        for language, language_rows in rows.items()
    }


def snapshot_json(
    semester: str,
    language: str,
    courses: list[dict[str, Any]],
    lists: dict[str, list],
    fetched_at: datetime,
) -> bytes:
    """The document `GET /v3/course-catalog/{semester}` serves."""
    document = {
        "semester": semester,
        "language": language,
        "fetched_at": fetched_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "courses": courses,
        **lists,
    }
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()


async def _store(
    session: AsyncSession, semester: str, documents: dict[str, bytes], fetched_at: datetime
) -> None:
    for language, document in documents.items():
        values = {
            "body": gzip.compress(document, mtime=0),
            "etag": f'W/"{hashlib.sha256(document).hexdigest()[:32]}"',
            "fetched_at": fetched_at,
        }
        await session.execute(
            insert(CourseCatalogSnapshot)
            .values(semester=semester, language=language, **values)
            .on_conflict_do_update(index_elements=["semester", "language"], set_=values)
        )


async def refresh_course_catalog(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Fetch and store the current term and the terms listed after it.

    Returns the semesters stored. A semester that failed keeps its last
    snapshot, and the refresh raises `CatalogFetchError` naming it once the
    others are stored, so the job reports the error.
    """
    now = now or datetime.now(UTC)
    async with session_scope(session_factory) as session:
        current = await current_term_code(session, now)
    stored: list[str] = []
    failed: list[str] = []
    async with httpx.AsyncClient(
        transport=transport, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}
    ) as client:
        semesters = await _target_semesters(client, current)
        lists = await _option_lists(client)
        for semester in semesters:
            try:
                courses = await _fetch_semester(client, semester)
            except CatalogFetchError as exc:
                logger.warning("course_catalog.semester_failed", semester=semester, error=str(exc))
                failed.append(semester)
                continue
            documents = {
                language: snapshot_json(semester, language, rows, lists[language], now)
                for language, rows in courses.items()
            }
            # Both languages or neither: the device searches the two together.
            async with session_scope(session_factory) as session:
                await _store(session, semester, documents, now)
            stored.append(semester)
            logger.info(
                "course_catalog.stored",
                semester=semester,
                courses=len(courses["zh"]),
                bytes={k: len(v) for k, v in documents.items()},
            )
    if failed:
        raise CatalogFetchError(f"kept the last snapshot of {', '.join(failed)}")
    return stored
