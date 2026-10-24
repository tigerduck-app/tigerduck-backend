"""The public pin document, and the operator actions on the admin listener.

The portal calls the actions with the operator in `X-Portal-Actor`; each
one writes its own `admin_audit_log` row in the transaction it changes.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import AwareDatetime, BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server import metrics
from server.admin_audit import AdminAuditLog
from server.db import SessionDep
from server.security import require_shared_secret
from server.tls_pins import targets
from server.tls_pins.document import (
    current_document,
    document_sets,
    lock_pin_state,
    publish,
)
from server.tls_pins.models import TlsPinDocument, TlsPinSet

DOCUMENT_PATH = "/.well-known/tigerduck/tls-pins.json"

public_router = APIRouter(tags=["tls-pins"])


@public_router.get(DOCUMENT_PATH)
async def tls_pin_document(
    session: SessionDep, if_none_match: Annotated[str | None, Header()] = None
) -> Response:
    """The highest serial's envelope, as stored. Unauthenticated: the apps
    fetch it before sign-in, and its signature is what they trust."""
    row = (
        await session.execute(
            select(TlsPinDocument.serial, TlsPinDocument.envelope)
            .order_by(TlsPinDocument.serial.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="tls_pin_document_not_found")
    headers = {"Cache-Control": "public, max-age=300", "ETag": f'"{row.serial}"'}
    tags = {tag.strip().removeprefix("W/") for tag in (if_none_match or "").split(",")}
    if headers["ETag"] in tags or "*" in tags:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=bytes(row.envelope), media_type="application/json", headers=headers)


admin_router = APIRouter(
    prefix="/tls-pins", tags=["admin"], dependencies=[Depends(require_shared_secret)]
)

ActorDep = Annotated[str, Header(alias="X-Portal-Actor", min_length=1, max_length=320)]


class PinIn(BaseModel):
    spki: str
    label: str = Field(min_length=1, max_length=200)

    @field_validator("spki")
    @classmethod
    def _sha256_base64(cls, value: str) -> str:
        try:
            digest = base64.b64decode(value, validate=True)
        except ValueError:
            digest = b""
        if len(digest) != 32 or base64.b64encode(digest).decode() != value:
            raise ValueError("must be the padded base64 of a SHA-256 digest")
        return value


class SpkiIn(BaseModel):
    spki: str


class HostsIn(BaseModel):
    hosts: list[str] = Field(max_length=50)

    @field_validator("hosts")
    @classmethod
    def _targets(cls, hosts: list[str]) -> list[str]:
        for target in hosts:
            try:
                targets.check_target(target)
            except targets.TargetRefused as exc:
                raise ValueError(f"{target}: {exc}") from None
        return list(dict.fromkeys(hosts))


class ExpiryIn(BaseModel):
    expires_at: AwareDatetime


class AutoPublishIn(BaseModel):
    enabled: bool


async def _locked_set(session: AsyncSession, set_id: str) -> TlsPinSet:
    await lock_pin_state(session)
    pin_set = await session.get(TlsPinSet, set_id)
    if pin_set is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="pin_set_not_found")
    return pin_set


async def _finish(
    session: AsyncSession,
    request: Request,
    actor: str,
    action: str,
    detail: dict,
    *,
    pin_set: TlsPinSet | None = None,
    publish_now: bool,
    pin_sets: list[dict] | None = None,
) -> dict:
    """Publish when asked and a key is configured, and record the action.
    Without a key the change is kept and goes out with the next document."""
    if pin_set is not None:
        pin_set.updated_at = datetime.now(UTC)
        pin_set.updated_by = actor
        metrics.TLS_PIN_SET_EXPIRY.labels(set=pin_set.id).set(pin_set.expires_at.timestamp())
    signer = request.app.state.tls_pin_signer
    serial = None
    if publish_now and signer is not None:
        document = await publish(
            session, signer, reason=f"portal.{action}", pin_sets=pin_sets
        )
        serial = document.serial
    session.add(
        AdminAuditLog(actor=actor, action=f"tls_pins.{action}", detail={**detail, "serial": serial})
    )
    return {"ok": True, "serial": serial}


@admin_router.post("/sets/{set_id}/pins")
async def add_pin(
    set_id: str, body: PinIn, request: Request, session: SessionDep, actor: ActorDep
) -> dict:
    pin_set = await _locked_set(session, set_id)
    if any(pin["spki"] == body.spki for pin in pin_set.pins):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="pin_exists")
    pin_set.pins = [
        *pin_set.pins,
        {
            "spki": body.spki,
            "label": body.label,
            "first_seen_at": None,
            "last_seen_at": None,
            "source": "portal",
        },
    ]
    return await _finish(
        session, request, actor, "add_pin", {"set": set_id, "spki": body.spki},
        pin_set=pin_set, publish_now=True,
    )


@admin_router.post("/sets/{set_id}/pins/remove")
async def remove_pin(
    set_id: str, body: SpkiIn, request: Request, session: SessionDep, actor: ActorDep
) -> dict:
    pin_set = await _locked_set(session, set_id)
    remaining = [pin for pin in pin_set.pins if pin["spki"] != body.spki]
    if len(remaining) == len(pin_set.pins):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="pin_not_found")
    if not any(not pin.get("pending") for pin in remaining):
        # A set with no published pins would fail every chain on its hosts.
        raise HTTPException(status.HTTP_409_CONFLICT, detail="last_pin")
    pin_set.pins = remaining
    return await _finish(
        session, request, actor, "remove_pin", {"set": set_id, "spki": body.spki},
        pin_set=pin_set, publish_now=True,
    )


async def _resolve_refusal(target: str) -> str | None:
    try:
        await asyncio.to_thread(targets.resolve, *targets.parse_target(target))
    except targets.TargetRefused as exc:
        return f"{target}: {exc}"
    return None


@admin_router.put("/sets/{set_id}/monitored-hosts")
async def set_monitored_hosts(
    set_id: str, body: HostsIn, request: Request, session: SessionDep, actor: ActorDep
) -> dict:
    # Resolved before the transaction opens; a lookup can take seconds.
    refusals = await asyncio.gather(*(_resolve_refusal(t) for t in body.hosts))
    pin_set = await _locked_set(session, set_id)
    refusals += [
        f"{t}: not under the set's domains"
        for t in body.hosts
        if not targets.under_domains(targets.parse_target(t)[0], pin_set.domains)
    ]
    if any(refusals):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="; ".join(filter(None, refusals))
        )
    pin_set.monitored_hosts = body.hosts
    pin_set.watch_results = {
        host: result for host, result in pin_set.watch_results.items() if host in body.hosts
    }
    return await _finish(
        session, request, actor, "set_hosts", {"set": set_id, "hosts": len(body.hosts)},
        pin_set=pin_set, publish_now=False,
    )


@admin_router.put("/sets/{set_id}/expires-at")
async def extend_expiry(
    set_id: str, body: ExpiryIn, request: Request, session: SessionDep, actor: ActorDep
) -> dict:
    pin_set = await _locked_set(session, set_id)
    # The apps take the later of their built-in expiry and the document's.
    if body.expires_at <= pin_set.expires_at:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="expiry_not_later")
    pin_set.expires_at = body.expires_at
    return await _finish(
        session, request, actor, "extend_expiry",
        {"set": set_id, "expires_at": body.expires_at.isoformat()},
        pin_set=pin_set, publish_now=True,
    )


@admin_router.put("/sets/{set_id}/auto-publish")
async def set_auto_publish(
    set_id: str, body: AutoPublishIn, request: Request, session: SessionDep, actor: ActorDep
) -> dict:
    pin_set = await _locked_set(session, set_id)
    pin_set.auto_publish = body.enabled
    return await _finish(
        session, request, actor, "set_auto_publish", {"set": set_id, "enabled": body.enabled},
        pin_set=pin_set, publish_now=False,
    )


def _require_signer(request: Request) -> None:
    if request.app.state.tls_pin_signer is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="tls_pin_signing_disabled"
        )


@admin_router.post("/publish")
async def publish_document(request: Request, session: SessionDep, actor: ActorDep) -> dict:
    """Sign the stored sets, approving the pins the watcher found while a
    set's auto publish was off."""
    _require_signer(request)
    await lock_pin_state(session)
    approved = {}
    for pin_set in (await session.execute(select(TlsPinSet))).scalars():
        pending = [pin["spki"] for pin in pin_set.pins if pin.get("pending")]
        if pending:
            pin_set.pins = [{**pin, "pending": False} for pin in pin_set.pins]
            pin_set.updated_at = datetime.now(UTC)
            pin_set.updated_by = actor
            approved[pin_set.id] = pending
    return await _finish(
        session, request, actor, "publish", {"approved": approved}, publish_now=True
    )


@admin_router.post("/resign")
async def resign_document(request: Request, session: SessionDep, actor: ActorDep) -> dict:
    """Sign the current document's sets again under a new serial."""
    _require_signer(request)
    await lock_pin_state(session)
    document = await current_document(session)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="tls_pin_document_not_found")
    return await _finish(
        session, request, actor, "resign", {},
        publish_now=True, pin_sets=document_sets(document),
    )
