"""The portal's Tests sections: sends that exist to exercise one push path
end to end against a real device, instead of waiting for the server to
decide to send it.

* Backend › Tests sends the Moodle-token-expired notice to the account.
* An iPhone or iPad's Tests tab starts a Live Activity on it through the
  server's push-to-start path, and Queued Jobs can fire a Live Activity's
  end early.

Every job goes through the real push pipeline; only the trigger is manual.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...db import get_pool
from .push import run_pipeline_now

router = APIRouter(prefix="/api/moodle/tests")

#: The scenario the pipeline keys on to write the sign-in-expired copy in
#: each recipient's language (`server.push.notification_copy.REAUTH_SCENARIO`).
_REAUTH_SCENARIO = "reauth_required"

#: `LiveActivityScenarioKind` raw values in the iOS app.
LiveActivityScenario = Literal["inClass", "classPreparing", "assignmentUrgent"]

#: The blue the app falls back to when it has no accent colour to hand.
_ACCENT_HEX = 0x007AFF


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_live_activity_payload(
    *,
    scenario: LiveActivityScenario,
    title: str,
    subtitle: str,
    location: str,
    minutes: int,
    now: datetime,
    source_id: str,
) -> dict:
    """The push_jobs payload for a test Live Activity start.

    The shape `/schedule/sync` files: the app's `LiveActivitySnapshot`
    flattened together with the routing keys the pipeline reads. It has to
    be a snapshot the app can decode — ActivityKit drops a start it cannot
    — so every non-optional field is present, and dates are ISO 8601 the
    way the app uploads them (the pipeline converts them for APNs).
    `activity_id` is composed as the app composes it, `{scenario}::{sourceId}`,
    which is what lets the phone register the started activity and the
    server file and send its end.
    """
    target = now + timedelta(minutes=minutes)
    snapshot = {
        "scenario": scenario,
        "title": title,
        "subtitle": subtitle,
        "locationText": location or None,
        "instructor": None,
        "countdownTarget": _iso(target),
        # The app draws a progress bar for a class in progress and for an
        # assignment, not for a class yet to start; here it runs from now.
        "progressStart": None if scenario == "classPreparing" else _iso(now),
        "accentHex": _ACCENT_HEX,
        "deepLink": None,
        "sourceId": source_id,
    }
    return {
        **snapshot,
        "kind": "schedule",
        "source_id": source_id,
        "activity_id": f"{scenario}::{source_id}",
    }


@router.post("/reauth-push")
async def send_test_reauth_push(
    request: Request,
    student_id: str = Query(..., min_length=1),
    pool=Depends(get_pool),
) -> JSONResponse:
    """Queue the Moodle-token-expired notice for this account, as the sync
    worker does when Moodle rejects the stored token, and run the pipeline.

    Only the notice: the account's credential and its sync jobs are left as
    they are, so nothing needs repairing afterwards. The pipeline sends it
    to every device on the account holding a push token except a Mac, with
    the copy in each device's own language. It has a dedupe key of its own,
    so it never collides with, or holds back, a real notice.
    """
    async with pool.acquire() as conn:
        uid = await conn.fetchval(
            "SELECT id FROM users WHERE student_id = $1", student_id
        )
        if uid is None:
            return JSONResponse({"ok": False, "error": "user not found"}, 404)
        devices = await conn.fetchval(
            "SELECT count(*) FROM user_devices d "
            "WHERE d.user_id = $1 AND d.deleted_at IS NULL "
            "AND d.platform <> 'macos' "
            "AND EXISTS (SELECT 1 FROM device_push_tokens t "
            "WHERE t.device_id = d.id AND t.token_kind = 'standard' "
            "AND t.status = 'active' "
            "AND (t.expires_at IS NULL OR t.expires_at > now()))",
            uid,
        )
        job_id = await conn.fetchval(
            "INSERT INTO push_jobs (user_id, dedupe_key, channel, scenario, fire_at, payload) "
            "VALUES ($1, $2, 'system', $3, now(), $4::jsonb) RETURNING id",
            uid,
            f"system:test_reauth:{uid}:{secrets.token_hex(4)}",
            _REAUTH_SCENARIO,
            json.dumps(
                {
                    "kind": _REAUTH_SCENARIO,
                    "provider": "ntust_sso",
                    "source": "portal_test",
                }
            ),
        )
    await run_pipeline_now(request, what="test reauth push")
    return JSONResponse({"ok": True, "push_job_id": job_id, "devices": devices})


class _LiveActivityTest(BaseModel):
    student_id: str = Field(min_length=1)
    device_id: str = Field(min_length=1, max_length=64)
    scenario: LiveActivityScenario = "inClass"
    title: str = Field(min_length=1, max_length=120)
    subtitle: str = Field(default="", max_length=120)
    location: str = Field(default="", max_length=60)
    minutes: int = Field(default=2, ge=1, le=240)


@router.post("/live-activity")
async def start_test_live_activity(
    body: _LiveActivityTest,
    request: Request,
    pool=Depends(get_pool),
) -> JSONResponse:
    """Start a Live Activity on one iPhone or iPad through the server.

    Files a start job for the device the way `/schedule/sync` does, due now,
    and runs the pipeline. APNs push-to-start puts the activity up, the
    phone registers its update token, `/live-activities/register` files the
    end at the countdown target, and that end push takes it down — one press
    covers the whole path from start to dismissal without waiting for a
    class.

    The dedupe key is deliberately not `schedule:{device}:…`: every
    `/schedule/sync` cancels this device's pending jobs under that prefix
    that its upload does not list, and the app uploads on each foreground.
    """
    async with pool.acquire() as conn:
        device = await conn.fetchrow(
            "SELECT d.id, d.user_id, d.platform FROM user_devices d "
            "JOIN users u ON u.id = d.user_id "
            "WHERE u.student_id = $1 AND d.id::text = $2 AND d.deleted_at IS NULL",
            body.student_id,
            body.device_id,
        )
        if device is None:
            return JSONResponse({"ok": False, "error": "device not found"}, 404)
        if device["platform"] not in ("ios", "ipados"):
            return JSONResponse(
                {"ok": False, "error": "Live Activities run only on an iPhone or iPad"},
                400,
            )
        has_token = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM device_push_tokens "
            "WHERE device_id = $1 AND token_kind = 'push_to_start' "
            "AND status = 'active' "
            "AND (expires_at IS NULL OR expires_at > now()))",
            device["id"],
        )
        if not has_token:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "no push-to-start token — turn Live Activity on in "
                    "the app and open it once",
                },
                409,
            )
        now = datetime.now(UTC)
        source_id = f"portal-test-{secrets.token_hex(4)}"
        payload = build_live_activity_payload(
            scenario=body.scenario,
            title=body.title,
            subtitle=body.subtitle,
            location=body.location,
            minutes=body.minutes,
            now=now,
            source_id=source_id,
        )
        job_id = await conn.fetchval(
            "INSERT INTO push_jobs "
            "(user_id, device_id, dedupe_key, channel, scenario, fire_at, payload) "
            "VALUES ($1, $2, $3, 'schedule', $4, $5, $6::jsonb) RETURNING id",
            device["user_id"],
            device["id"],
            f"portal_la:{device['id']}:{source_id}",
            body.scenario,
            now,
            json.dumps(payload),
        )
    await run_pipeline_now(request, what="test Live Activity")
    return JSONResponse(
        {
            "ok": True,
            "push_job_id": job_id,
            "activity_id": payload["activity_id"],
            "ends_at": payload["countdownTarget"],
        }
    )


@router.post("/live-activity-end-now")
async def end_live_activity_now(
    request: Request,
    student_id: str = Query(..., min_length=1),
    job_id: int = Query(..., ge=1),
    pool=Depends(get_pool),
) -> JSONResponse:
    """Fire a queued Live Activity end now instead of at its countdown target.

    Moves the end job `/live-activities/register` filed to now and runs the
    pipeline, so the dismissal can be checked without waiting out the
    countdown. Only a pending end job on this account is touched.
    """
    async with pool.acquire() as conn:
        moved = await conn.fetchval(
            "UPDATE push_jobs pj SET fire_at = now(), updated_at = now() "
            "FROM users u "
            "WHERE u.id = pj.user_id AND u.student_id = $1 AND pj.id = $2 "
            "AND pj.channel = 'schedule' "
            "AND pj.payload->>'kind' = 'live_activity_end' "
            "AND pj.status = 'pending' "
            "RETURNING pj.id",
            student_id,
            job_id,
        )
    if moved is None:
        return JSONResponse(
            {"ok": False, "error": "no pending Live Activity end with that id"}, 404
        )
    await run_pipeline_now(request, what="Live Activity end-now")
    return JSONResponse({"ok": True, "push_job_id": moved})
