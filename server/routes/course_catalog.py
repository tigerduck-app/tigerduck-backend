"""/v3/course-catalog: a semester's whole course list, for on-device search.

The refresh (`server/course_catalog/refresh.py`) stores each semester
and language gzip-compressed with its ETag, and leaves a snapshot whose
content has not changed as it is. A device that already holds the snapshot
costs a 304, and a new one is the stored bytes, sent as they are.
"""

from __future__ import annotations

import gzip
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import select

from server.auth.dependencies import require_user
from server.course_catalog.models import CourseCatalogSnapshot
from server.db import SessionDep

router = APIRouter(
    prefix="/course-catalog", tags=["course-catalog"], dependencies=[Depends(require_user)]
)


@router.get("/{semester}")
async def get_course_catalog(
    semester: str,
    session: SessionDep,
    language: Literal["zh", "en"] = "zh",
    if_none_match: Annotated[str | None, Header()] = None,
    accept_encoding: Annotated[str, Header()] = "",
) -> Response:
    """The semester's last complete snapshot in `language`: its courses,
    colleges with departments, dimensions and `fetched_at`, when that content
    was first fetched. 404 until the refresh has stored one."""
    key = (CourseCatalogSnapshot.semester == semester, CourseCatalogSnapshot.language == language)
    # The ETag alone first: a 304 has no use for the body, a few hundred kilobytes.
    etag = await session.scalar(select(CourseCatalogSnapshot.etag).where(*key))
    if etag is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="course_catalog_not_found")
    # Weak comparison, as for If-None-Match: a proxy that recompresses weakens the tag.
    tags = {tag.strip().removeprefix("W/") for tag in (if_none_match or "").split(",")}
    if etag.removeprefix("W/") in tags or "*" in tags:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=_headers(etag))
    # Read again with the body, so a refresh committed since cannot pair them wrongly.
    row = (
        await session.execute(
            select(CourseCatalogSnapshot.etag, CourseCatalogSnapshot.body).where(*key)
        )
    ).one()
    headers = _headers(row.etag)
    if accepts_gzip(accept_encoding):
        return Response(
            row.body, media_type="application/json", headers={**headers, "Content-Encoding": "gzip"}
        )
    return Response(gzip.decompress(row.body), media_type="application/json", headers=headers)


def _headers(etag: str) -> dict[str, str]:
    return {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Accept-Encoding"}


def accepts_gzip(accept_encoding: str) -> bool:
    """Whether the Accept-Encoding header takes gzip at a q above 0, by name
    or through `*` when gzip is not named (RFC 9110 section 12.5.3)."""
    star = False
    for item in accept_encoding.split(","):
        coding, *params = (part.strip().lower() for part in item.split(";"))
        weights = [p.removeprefix("q=").strip() for p in params if p.startswith("q=")]
        try:
            accepted = not weights or float(weights[0]) > 0
        except ValueError:
            accepted = False
        if coding == "gzip":
            return accepted
        if coding == "*":
            star = accepted
    return star
