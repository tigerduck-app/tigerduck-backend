"""Device lists — named groups of devices, used as custom-push targets.

Reads/writes the `device_lists` + `device_list_members` tables directly
over tigerduck-db (no backend API call). Membership references v3
`user_devices.id`. Replaces the old `/v2/device-lists/*` proxy; the
`/api/device-lists/*` contract the SPA consumes is unchanged.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response

from ..db import get_pool

router = APIRouter(prefix="/api/device-lists")


def _list_to_dict(r) -> dict:
    return {
        "id": r["id"],
        "name": r["name"],
        "description": r["description"],
        "member_count": int(r["member_count"] or 0),
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


_LIST_SELECT = """
SELECT dl.id, dl.name, dl.description, dl.created_at, dl.updated_at,
       (SELECT count(*) FROM device_list_members m WHERE m.list_id = dl.id)
           AS member_count
FROM device_lists dl
"""


@router.get("")
async def list_lists(pool=Depends(get_pool)) -> JSONResponse:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_LIST_SELECT + " ORDER BY dl.name")
    return JSONResponse(content=[_list_to_dict(r) for r in rows])


@router.post("")
async def create_list(request: Request, pool=Depends(get_pool)) -> JSONResponse:
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse(status_code=400, content={"detail": "name required"})
    description = body.get("description")
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT 1 FROM device_lists WHERE name = $1", name
        )
        if existing:
            return JSONResponse(
                status_code=409, content={"detail": "list name already exists"}
            )
        row = await conn.fetchrow(
            """
            INSERT INTO device_lists (name, description)
            VALUES ($1, $2)
            RETURNING id, name, description, created_at, updated_at
            """,
            name,
            description,
        )
    return JSONResponse(content=_list_to_dict({**dict(row), "member_count": 0}))


@router.get("/{list_id}")
async def get_list(list_id: int, pool=Depends(get_pool)) -> JSONResponse:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_LIST_SELECT + " WHERE dl.id = $1", list_id)
    if row is None:
        return JSONResponse(status_code=404, content={"detail": "list not found"})
    return JSONResponse(content=_list_to_dict(row))


@router.patch("/{list_id}")
async def update_list(
    list_id: int, request: Request, pool=Depends(get_pool)
) -> JSONResponse:
    body = await request.json()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM device_lists WHERE id = $1", list_id
        )
        if not exists:
            return JSONResponse(
                status_code=404, content={"detail": "list not found"}
            )
        # Only update provided fields; COALESCE keeps the omitted ones.
        name = body.get("name")
        if name is not None and not name.strip():
            return JSONResponse(
                status_code=400, content={"detail": "name cannot be blank"}
            )
        await conn.execute(
            """
            UPDATE device_lists
            SET name = COALESCE($2, name),
                description = CASE WHEN $3::bool THEN $4 ELSE description END,
                updated_at = now()
            WHERE id = $1
            """,
            list_id,
            name.strip() if name else None,
            "description" in body,
            body.get("description"),
        )
        row = await conn.fetchrow(_LIST_SELECT + " WHERE dl.id = $1", list_id)
    return JSONResponse(content=_list_to_dict(row))


@router.delete("/{list_id}")
async def delete_list(list_id: int, pool=Depends(get_pool)) -> Response:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM device_lists WHERE id = $1", list_id)
    return Response(status_code=204)


_MEMBERS_SQL = """
SELECT
    m.device_id::text                      AS device_id,
    COALESCE(u.student_id, u.id::text)     AS user_id,
    ud.platform                            AS platform,
    CASE ud.platform
        WHEN 'ios'    THEN 'iphone'
        WHEN 'ipados' THEN 'ipad'
        ELSE ''
    END                                    AS device_class,
    m.added_at                             AS added_at
FROM device_list_members m
JOIN user_devices ud ON ud.id = m.device_id
JOIN users u ON u.id = ud.user_id
WHERE m.list_id = $1
ORDER BY m.added_at DESC
LIMIT $2 OFFSET $3
"""


@router.get("/{list_id}/members")
async def list_members(
    list_id: int,
    pool=Depends(get_pool),
    limit: int = Query(default=500, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    async with pool.acquire() as conn:
        meta = await conn.fetchrow(
            "SELECT name FROM device_lists WHERE id = $1", list_id
        )
        if meta is None:
            return JSONResponse(
                status_code=404, content={"detail": "list not found"}
            )
        total = await conn.fetchval(
            "SELECT count(*) FROM device_list_members WHERE list_id = $1",
            list_id,
        )
        rows = await conn.fetch(_MEMBERS_SQL, list_id, limit, offset)
    return JSONResponse(
        content={
            "list_id": list_id,
            "list_name": meta["name"],
            "items": [
                {
                    "device_id": r["device_id"],
                    "user_id": r["user_id"],
                    "platform": r["platform"],
                    "device_class": r["device_class"],
                    "added_at": r["added_at"].isoformat()
                    if r["added_at"]
                    else None,
                }
                for r in rows
            ],
            "total": int(total or 0),
        }
    )


@router.post("/{list_id}/members")
async def add_members(
    list_id: int, request: Request, pool=Depends(get_pool)
) -> JSONResponse:
    body = await request.json()
    raw_ids = body.get("device_ids") or []
    # Dedupe while preserving the operator's selection.
    device_ids = list(dict.fromkeys(str(d) for d in raw_ids))
    if not device_ids:
        return JSONResponse(
            status_code=400, content={"detail": "device_ids required"}
        )
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM device_lists WHERE id = $1", list_id
        )
        if not exists:
            return JSONResponse(
                status_code=404, content={"detail": "list not found"}
            )
        # Which submitted ids are real, undeleted devices? Casting each id
        # individually would 500 on a malformed uuid; filter to valid uuids
        # in SQL instead.
        known = {
            str(r["id"])
            for r in await conn.fetch(
                """
                SELECT id FROM user_devices
                WHERE deleted_at IS NULL
                  AND id = ANY($1::uuid[])
                """,
                _valid_uuids(device_ids),
            )
        }
        already = {
            str(r["device_id"])
            for r in await conn.fetch(
                """
                SELECT device_id FROM device_list_members
                WHERE list_id = $1 AND device_id = ANY($2::uuid[])
                """,
                list_id,
                _valid_uuids(list(known)),
            )
        }
        to_add = [d for d in device_ids if d in known and d not in already]
        if to_add:
            await conn.executemany(
                """
                INSERT INTO device_list_members (list_id, device_id)
                VALUES ($1, $2::uuid)
                ON CONFLICT DO NOTHING
                """,
                [(list_id, d) for d in to_add],
            )
    unknown = [d for d in device_ids if d not in known]
    return JSONResponse(
        content={
            "added": len(to_add),
            "already_present": len(already),
            "unknown": len(unknown),
        }
    )


@router.delete("/{list_id}/members/{device_id}")
async def remove_member(
    list_id: int, device_id: str, pool=Depends(get_pool)
) -> Response:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM device_list_members
            WHERE list_id = $1 AND device_id = $2::uuid
            """,
            list_id,
            device_id,
        )
    return Response(status_code=204)


def _valid_uuids(ids: list[str]) -> list[str]:
    """Keep only well-formed UUID strings so `$1::uuid[]` casts cleanly."""
    import uuid as _uuid

    out: list[str] = []
    for d in ids:
        try:
            out.append(str(_uuid.UUID(d)))
        except (ValueError, AttributeError, TypeError):
            continue
    return out
