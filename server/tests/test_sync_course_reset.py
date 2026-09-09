"""DELETE /v3/sync/courses: a reset must not be undone by a stale device.

Two devices, one account. B resets the term and uploads the roster the
portal now returns; A still has the pre-reset roster on disk and uploads
it on its next refresh. Before the tombstone rewrite that second upload
resurrected every course B had just dropped, and B picked them back up on
its own next sync -- the reset undid itself in about twenty seconds.
"""

from __future__ import annotations

import pytest

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier

pytestmark = pytest.mark.asyncio(loop_scope="session")

SEMESTER = "1151"
# The seven the portal still enrols after 加退選, and the four it dropped.
KEPT = ["CS3009301", "CS3025301", "CS3051701", "CS4001302"]
DROPPED = ["CH1004301", "CH1004302", "CS3044701", "CS4001301"]


def _login_body(device: str, platform: str) -> dict:
    return {
        "student_id": "B11015000",
        "password": "pw",
        "moodle_token": "tok",
        "device_info": {"client_device_id": device, "platform": platform},
    }


async def _login(client, device: str, platform: str) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="b11015000")
    )
    response = await client.post("/v3/auth/login", json=_login_body(device, platform))
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _upload(client, headers: dict, course_nos: list[str]):
    return await client.post(
        "/v3/sync/courses/upload",
        headers=headers,
        json={
            "courses": [
                {"semester": SEMESTER, "course_no": no, "course_name": no}
                for no in course_nos
            ]
        },
    )


async def _server_course_nos(client, headers: dict) -> list[str]:
    response = await client.get("/v3/sync/full", headers=headers)
    assert response.status_code == 200
    return sorted(
        row["course_no"]
        for row in response.json()["courses"]
        if row.get("semester") == SEMESTER
    )


async def test_stale_device_cannot_resurrect_a_reset_semester(client) -> None:
    android = await _login(client, "android-a", "android")
    iphone = await _login(client, "iphone-b", "ios")

    # Both devices agree on the pre-reset roster.
    assert (await _upload(client, android, KEPT + DROPPED)).status_code == 200
    assert await _server_course_nos(client, iphone) == sorted(KEPT + DROPPED)

    # B resets the term and re-uploads what the portal now returns. The
    # resetting device must not be blocked by the tombstones its own reset
    # just wrote, or the reset would leave the term empty.
    reset = await client.delete(f"/v3/sync/courses?semester={SEMESTER}", headers=iphone)
    assert reset.status_code == 200
    assert reset.json()["deleted"] == len(KEPT) + len(DROPPED)
    assert (await _upload(client, iphone, KEPT)).status_code == 200
    assert await _server_course_nos(client, iphone) == sorted(KEPT)

    # A refreshes before it has reconciled and pushes the stale roster.
    stale = await _upload(client, android, KEPT + DROPPED)
    assert stale.status_code == 200
    assert stale.json()["skipped_tombstoned"] == len(DROPPED)
    assert await _server_course_nos(client, android) == sorted(KEPT)


async def test_explicit_re_add_still_clears_a_reset_tombstone(client) -> None:
    """force_keys is how a user says "no, I really do want this one back"."""
    iphone = await _login(client, "iphone-force", "ios")
    assert (await _upload(client, iphone, KEPT + DROPPED)).status_code == 200
    assert (
        await client.delete(f"/v3/sync/courses?semester={SEMESTER}", headers=iphone)
    ).status_code == 200

    android = await _login(client, "android-force", "android")
    re_added = await client.post(
        "/v3/sync/courses/upload",
        headers=android,
        json={
            "courses": [
                {"semester": SEMESTER, "course_no": DROPPED[0], "course_name": "back"}
            ],
            "force_keys": [f"client:{SEMESTER}:{DROPPED[0]}"],
        },
    )
    assert re_added.status_code == 200
    assert re_added.json()["skipped_tombstoned"] == 0
    assert await _server_course_nos(client, android) == [DROPPED[0]]
