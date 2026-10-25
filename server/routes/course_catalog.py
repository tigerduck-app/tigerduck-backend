"""/v3/course-catalog: a semester's whole course list, for on-device search.

The daily refresh (`server/course_catalog/refresh.py`) stores each semester
and language gzip-compressed with its ETag, so a device that already holds
the snapshot costs a 304 and a new one is the stored bytes, sent as they are.
"""

from __future__ import annotations

import gzip
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

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
    colleges with departments, dimensions and `fetched_at`. 404 until the
    refresh has stored one."""
    row = await session.get(CourseCatalogSnapshot, (semester, language))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="course_catalog_not_found")
    headers = {"ETag": row.etag, "Cache-Control": "private, no-cache", "Vary": "Accept-Encoding"}
    # Weak comparison, as for If-None-Match: a proxy that recompresses weakens the tag.
    tags = {tag.strip().removeprefix("W/") for tag in (if_none_match or "").split(",")}
    if row.etag.removeprefix("W/") in tags or "*" in tags:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    if "gzip" in accept_encoding.lower():
        return Response(
            row.body, media_type="application/json", headers={**headers, "Content-Encoding": "gzip"}
        )
    return Response(gzip.decompress(row.body), media_type="application/json", headers=headers)
