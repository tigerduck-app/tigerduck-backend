"""Moodle sync management endpoints for the admin portal.

Split from one 724-line module. Each submodule owns a router prefixed
/api/moodle; this file mounts them in the original registration order.
"""

from fastapi import APIRouter

from . import inspection, jobs, logs, push, status

router = APIRouter()
router.include_router(status.router)
router.include_router(jobs.router)
router.include_router(inspection.router)
router.include_router(logs.router)
router.include_router(push.router)
