"""/v3/settings — JSONB settings documents with revision-based optimistic
concurrency (sync-and-push spec §6)."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import CurrentAuth, CurrentAuthDep
from server.db import SessionDep
from server.sync import serializers
from server.sync.changelog import append_change
from server.sync.models import SETTINGS_NAMESPACES, UserSettingsDocument

router = APIRouter(prefix="/settings", tags=["settings"])
logger = structlog.get_logger(__name__)


class SettingsPut(BaseModel):
    schema_version: int = Field(default=1, ge=1)
    document: dict
    # null = "create this namespace"; an integer = optimistic update.
    base_revision: int | None = Field(default=None, ge=1)


def _validate_namespace(namespace: str) -> None:
    if namespace not in SETTINGS_NAMESPACES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown namespace {namespace!r}",
        )


async def _get_document(
    session: AsyncSession, auth: CurrentAuth, namespace: str
) -> UserSettingsDocument | None:
    return (
        await session.execute(
            select(UserSettingsDocument).where(
                UserSettingsDocument.user_id == auth.user_id,
                UserSettingsDocument.namespace == namespace,
                UserSettingsDocument.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


def _conflict_response(existing: UserSettingsDocument) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error": "settings_conflict",
            "namespace": existing.namespace,
            "server": serializers.settings_document_to_dict(existing),
        },
    )


@router.get("")
async def batch_read(
    auth: CurrentAuthDep,
    session: SessionDep,
    namespaces: str = Query(min_length=1),
):
    requested = [n.strip() for n in namespaces.split(",") if n.strip()]
    for namespace in requested:
        _validate_namespace(namespace)
    rows = (
        (
            await session.execute(
                select(UserSettingsDocument).where(
                    UserSettingsDocument.user_id == auth.user_id,
                    UserSettingsDocument.namespace.in_(requested),
                    UserSettingsDocument.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return {"items": [serializers.settings_document_to_dict(d) for d in rows]}


@router.get("/{namespace}")
async def read_document(
    namespace: str, auth: CurrentAuthDep, session: SessionDep
):
    _validate_namespace(namespace)
    document = await _get_document(session, auth, namespace)
    if document is None:
        raise HTTPException(status_code=404, detail="settings document not found")
    return serializers.settings_document_to_dict(document)


@router.put("/{namespace}")
async def put_document(
    namespace: str,
    payload: SettingsPut,
    auth: CurrentAuthDep,
    session: SessionDep,
):
    _validate_namespace(namespace)

    if payload.base_revision is None:
        existing = await _get_document(session, auth, namespace)
        if existing is not None:
            return _conflict_response(existing)
        document = UserSettingsDocument(
            user_id=auth.user_id,
            namespace=namespace,
            schema_version=payload.schema_version,
            document=payload.document,
            created_by_device_id=auth.device_id,
            updated_by_device_id=auth.device_id,
        )
        session.add(document)
        try:
            await session.flush()
        except IntegrityError:
            # Two devices raced the create on the partial unique index —
            # surface the winner's state, same as the explicit-check path.
            await session.rollback()
            existing = await _get_document(session, auth, namespace)
            if existing is None:  # pragma: no cover - extremely tight race
                raise HTTPException(status_code=409, detail="settings_conflict")
            return _conflict_response(existing)
    else:
        # Atomic compare-and-swap on revision: no rows updated = conflict.
        result = await session.execute(
            update(UserSettingsDocument)
            .where(
                UserSettingsDocument.user_id == auth.user_id,
                UserSettingsDocument.namespace == namespace,
                UserSettingsDocument.revision == payload.base_revision,
                UserSettingsDocument.deleted_at.is_(None),
            )
            .values(
                document=payload.document,
                schema_version=payload.schema_version,
                revision=UserSettingsDocument.revision + 1,
                updated_by_device_id=auth.device_id,
            )
        )
        if result.rowcount == 0:
            existing = await _get_document(session, auth, namespace)
            if existing is None:
                raise HTTPException(
                    status_code=404, detail="settings document not found"
                )
            return _conflict_response(existing)
        document = await _get_document(session, auth, namespace)
        assert document is not None

    await append_change(
        session,
        user_id=auth.user_id,
        entity_type="settings_document",
        entity_id=namespace,
        operation="upsert",
        payload={"revision": document.revision},
        device_id=auth.device_id,
    )
    logger.info(
        "settings.updated",
        user_id=str(auth.user_id),
        namespace=namespace,
        revision=document.revision,
    )
    return serializers.settings_document_to_dict(document)
