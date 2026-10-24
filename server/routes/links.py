"""`GET /links/{name}`, the redirects the apps follow, and the operator
routes that edit them, which only the admin listener serves."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from server.admin_audit import AdminAuditLog
from server.db import SessionDep
from server.links import checker
from server.links.models import ALERT_OK, Link
from server.links.rules import Rule, dump
from server.links.targets import (
    NAME_PATTERN,
    InvalidTarget,
    is_valid_name,
    validate_target,
)
from server.security import require_shared_secret

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["links"])
admin_router = APIRouter(
    prefix="/admin/links",
    tags=["admin"],
    dependencies=[Depends(require_shared_secret)],
)

_NOT_FOUND = "link not found"


@router.get("/links/{name}")
async def follow_link(name: str, request: Request, session: SessionDep) -> RedirectResponse:
    link = await session.get(Link, name) if is_valid_name(name) else None
    if link is None or not link.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    try:
        target = validate_target(
            link.target_url, request.app.state.settings.links_allowed_hosts
        )
    except InvalidTarget as exc:
        logger.warning("links.target_refused", name=name, reason=str(exc))
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND
        ) from None
    return RedirectResponse(
        target,
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        headers={"Cache-Control": "no-store"},
    )


# The portal sends the operator its Cloudflare Access check verified.
Actor = Annotated[str, Header(alias="X-Admin-Actor", min_length=1, max_length=320)]

TargetUrl = Annotated[str, Field(min_length=1, max_length=2048)]
Expectations = Annotated[list[Rule], Field(max_length=20)]
IntervalMinutes = Annotated[int, Field(ge=5, le=1440)]


class LinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    target_url: TargetUrl
    enabled: bool = True
    expectations: Expectations = Field(default_factory=list)
    check_interval_minutes: IntervalMinutes = 60


class LinkUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_url: TargetUrl | None = None
    enabled: bool | None = None
    expectations: Expectations | None = None
    check_interval_minutes: IntervalMinutes | None = None


def _checked_target(url: str, request: Request) -> str:
    try:
        return validate_target(url, request.app.state.settings.links_allowed_hosts)
    except InvalidTarget as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"invalid target: {exc}",
        ) from None


def _link_dict(link: Link) -> dict:
    return {
        "name": link.name,
        "target_url": link.target_url,
        "enabled": link.enabled,
        "expectations": link.expectations,
        "check_interval_minutes": link.check_interval_minutes,
        "last_check_at": link.last_check_at.isoformat() if link.last_check_at else None,
        "last_check_ok": link.last_check_ok,
        "last_check_detail": link.last_check_detail,
        "alert_state": link.alert_state,
        "updated_at": link.updated_at.isoformat(),
        "updated_by": link.updated_by,
    }


@admin_router.get("")
async def list_links(session: SessionDep) -> dict:
    links = (await session.execute(select(Link).order_by(Link.name))).scalars().all()
    return {"links": [_link_dict(link) for link in links]}


@admin_router.post("", status_code=status.HTTP_201_CREATED)
async def create_link(
    payload: LinkCreate, actor: Actor, request: Request, session: SessionDep
) -> dict:
    target = _checked_target(payload.target_url, request)
    inserted = await session.scalar(
        pg_insert(Link)
        .values(
            name=payload.name,
            target_url=target,
            enabled=payload.enabled,
            expectations=dump(payload.expectations),
            check_interval_minutes=payload.check_interval_minutes,
            updated_by=actor,
        )
        .on_conflict_do_nothing(index_elements=[Link.name])
        .returning(Link.name)
    )
    if inserted is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="a link with this name exists"
        )
    session.add(
        AdminAuditLog(
            actor=actor,
            action="link.create",
            detail={"name": payload.name, "target_url": target},
        )
    )
    return _link_dict(await session.get(Link, payload.name))


def _update_action(changes: dict) -> str:
    if changes == {"enabled": False}:
        return "link.disable"
    if changes == {"enabled": True}:
        return "link.enable"
    return "link.update"


@admin_router.patch("/{name}")
async def update_link(
    name: str, payload: LinkUpdate, actor: Actor, request: Request, session: SessionDep
) -> dict:
    link = await session.get(Link, name, with_for_update=True)
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    requested = payload.model_dump(exclude_none=True, by_alias=True)
    if "target_url" in requested:
        _checked_target(requested["target_url"], request)
    changes = {k: v for k, v in requested.items() if getattr(link, k) != v}
    if changes:
        for field, value in changes.items():
            setattr(link, field, value)
        link.updated_at = datetime.now(UTC)
        link.updated_by = actor
        if "enabled" in changes:
            # A link turned off or on starts with no alert, so turning it back
            # on cannot page with the failure it had before.
            link.alert_state = ALERT_OK
        if changes.keys() & {"target_url", "expectations"} or changes.get("enabled"):
            # Due at once: the next tick checks what was saved.
            link.last_check_at = None
        session.add(
            AdminAuditLog(
                actor=actor,
                action=_update_action(changes),
                detail={"name": name, **changes},
            )
        )
    return _link_dict(link)


@admin_router.post("/{name}/test")
async def test_link(name: str, actor: Actor, request: Request) -> dict:
    """Check the link now, whether enabled or not, and record the result
    as a scheduled check would."""
    factory = request.app.state.session_factory
    async with factory() as session:
        if await session.get(Link, name) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    result = await checker.check_link(
        factory, request.app.state.settings, name, actor=actor
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the link changed or was removed during the check",
        )
    return {"ok": result.ok, "detail": result.detail}
