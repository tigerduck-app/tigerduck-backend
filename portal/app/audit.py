"""Operator actions recorded in `admin_audit_log` (model: server/admin_audit.py).

Each route that changes state, or shows one account's data, calls `audit()`
itself once the action has succeeded, inside the action's transaction where
it has one: a failed or rolled-back action leaves no row. Rows name accounts
and devices by `users.id` and `user_devices.id`; `detail` holds counts and
filters, never a student id, a typed search, a push body or a token.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import Request

# ip stays null: a token holder going around Cloudflare can forge Cf-Connecting-IP.
_INSERT = """
INSERT INTO admin_audit_log (actor, action, target_user_id, target_device_id, detail)
VALUES ($1, $2, $3::uuid, $4::uuid, $5::jsonb)
"""

# Data Inspection polls every few seconds; one view per actor and account per 10 min.
_INSERT_VIEW = """
INSERT INTO admin_audit_log (actor, action, target_user_id)
SELECT $1::text, 'user.view', $2::uuid
WHERE NOT EXISTS (
    SELECT 1 FROM admin_audit_log
    WHERE target_user_id = $2::uuid AND actor = $1::text
      AND action = 'user.view' AND ts > now() - interval '10 minutes'
)
"""


async def audit(
    conn,
    request: Request,
    action: str,
    *,
    user_id: Any = None,
    device_id: Any = None,
    **detail: Any,
) -> None:
    actor = request.state.actor
    if action == "user.view":
        await conn.execute(_INSERT_VIEW, actor, str(user_id))
        return
    await conn.execute(
        _INSERT,
        actor,
        action,
        None if user_id is None else str(user_id),
        None if device_id is None else str(device_id),
        json.dumps(detail),
    )
