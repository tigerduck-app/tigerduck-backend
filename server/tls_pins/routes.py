"""The public pin document, and the operator actions on the admin listener."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select

from server.db import SessionDep
from server.tls_pins.models import TlsPinDocument

DOCUMENT_PATH = "/.well-known/tigerduck/tls-pins.json"

public_router = APIRouter(tags=["tls-pins"])


@public_router.get(DOCUMENT_PATH)
async def tls_pin_document(session: SessionDep) -> Response:
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
    return Response(
        content=bytes(row.envelope),
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=300", "ETag": f'"{row.serial}"'},
    )
