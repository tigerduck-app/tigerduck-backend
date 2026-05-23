"""Admin management — list, add, remove."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from ..auth import get_settings, require_admin
from ..config import Settings
from ..db import connect, log_action

router = APIRouter(prefix="/admins")


def _list_admins(db_path: str) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT email, added_at, added_by, notes FROM admins ORDER BY added_at"
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("", response_class=HTMLResponse)
async def list_admins(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    actor: Annotated[str, Depends(require_admin)],
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "admins.html",
        {
            "actor": actor,
            "admins": _list_admins(settings.portal_db_path),
            "bootstrap_admin": settings.portal_bootstrap_admin,
        },
    )


@router.post("")
async def add_admin(
    settings: Annotated[Settings, Depends(get_settings)],
    actor: Annotated[str, Depends(require_admin)],
    email: Annotated[str, Form()],
    notes: Annotated[str, Form()] = "",
) -> RedirectResponse:
    email = email.strip().lower()
    if "@" not in email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "email must contain '@'")
    with connect(settings.portal_db_path) as conn:
        conn.execute(
            """
            INSERT INTO admins (email, added_by, notes) VALUES (?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET notes = excluded.notes
            """,
            (email, actor, notes or None),
        )
    log_action(settings.portal_db_path, actor, "admin.add", email)
    return RedirectResponse("/admins", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{email}/delete")
async def remove_admin(
    settings: Annotated[Settings, Depends(get_settings)],
    actor: Annotated[str, Depends(require_admin)],
    email: str,
) -> RedirectResponse:
    # Removing the bootstrap admin is allowed — startup will re-add them.
    # Removing yourself is also allowed (next request 403s; not our
    # problem). Both behaviors are intentional per the design doc.
    with connect(settings.portal_db_path) as conn:
        conn.execute("DELETE FROM admins WHERE email = ?", (email,))
    log_action(settings.portal_db_path, actor, "admin.remove", email)
    return RedirectResponse("/admins", status_code=status.HTTP_303_SEE_OTHER)
