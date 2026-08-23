"""Client sync endpoints.

Split from one 880-line module. Each submodule owns a router prefixed
/sync and this file mounts them in the original registration order. That
order is load-bearing: FastAPI resolves paths in the order they are
added, and DELETE /courses has to stay ahead of
DELETE /courses/{course_key:path}, whose `path` converter also matches an
empty key and would otherwise swallow it.
"""

from fastapi import APIRouter

from . import assignments, courses, snapshot, uploads

router = APIRouter()
router.include_router(snapshot.router)
router.include_router(uploads.router)
router.include_router(courses.router)
router.include_router(assignments.router)
