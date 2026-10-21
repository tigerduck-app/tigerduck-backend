"""A course added by hand, removed again, and later really taken.

Both apps add a course by hand with an upload that forces its key, and
remove it with the DELETE that also hides an enrolled course. Every device's
full sync reads the tombstone that DELETE leaves as "hide this course here",
and neither app uploads a course it hides. So once the student really takes
the course, only the device that removed it can bring it back with its
roster: its snapshot leaves that tombstone out, and its upload releases it.
"""

from __future__ import annotations

import pytest

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier

pytestmark = pytest.mark.asyncio(loop_scope="session")

SEMESTER = "1151"
ENROLLED = "CS1001301"
ADDED = "GE1010001"
OTHER = "PE1010002"


def _key(course_no: str) -> str:
    return f"client:{SEMESTER}:{course_no}"


async def _login(client, device: str, platform: str) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="b11015000")
    )
    response = await client.post(
        "/v3/auth/login",
        json={
            "student_id": "B11015000",
            "password": "pw",
            "moodle_token": "tok",
            "device_info": {"client_device_id": device, "platform": platform},
        },
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _upload(client, headers: dict, course_nos: list[str], force: list[str] = ()) -> dict:
    response = await client.post(
        "/v3/sync/courses/upload",
        headers=headers,
        json={
            "courses": [
                {"semester": SEMESTER, "course_no": no, "course_name": no}
                for no in course_nos
            ],
            "force_keys": [_key(no) for no in force],
        },
    )
    assert response.status_code == 200
    return response.json()


async def _delete(client, headers: dict, course_no: str) -> None:
    response = await client.delete(f"/v3/sync/courses/{_key(course_no)}", headers=headers)
    assert response.json() == {"deleted": 1}


async def _snapshot(client, headers: dict) -> tuple[dict[str, str], dict[str, dict]]:
    """The term's course rows (course number to `source`) and tombstones."""
    response = await client.get("/v3/sync/full", headers=headers)
    assert response.status_code == 200
    body = response.json()
    rows = {c["course_no"]: c["source"] for c in body["courses"] if c["semester"] == SEMESTER}
    tombstones = {
        t["course_no"]: t for t in body["course_tombstones"] if t["semester"] == SEMESTER
    }
    return rows, tombstones


async def test_the_roster_brings_back_a_course_its_device_added_and_removed(client) -> None:
    phone = await _login(client, "iphone-hand", "ios")
    tablet = await _login(client, "android-hand", "android")
    await _upload(client, phone, [ENROLLED])
    await _upload(client, phone, [ENROLLED, ADDED], force=[ADDED])
    rows, _ = await _snapshot(client, tablet)
    assert rows == {ENROLLED: "ntust_portal", ADDED: "user_added"}

    await _delete(client, phone, ADDED)

    # Read as "hide", the tombstone would keep the phone from ever uploading
    # the course again; the tablet still has to drop its copy.
    _, on_phone = await _snapshot(client, phone)
    assert ADDED not in on_phone
    _, on_tablet = await _snapshot(client, tablet)
    assert on_tablet[ADDED]["deleted_by_reset"] is False
    assert on_tablet[ADDED]["deleted_by_this_device"] is False

    # The tablet's copy, sent before it synced, stays out.
    stale = await _upload(client, tablet, [ENROLLED, ADDED])
    assert stale["skipped_tombstoned"] == 1
    assert stale["accepted_keys"] == [_key(ENROLLED)]

    # The student takes the course, and the phone's roster lists it.
    roster = await _upload(client, phone, [ENROLLED, ADDED])
    assert roster["skipped_tombstoned"] == 0
    assert roster["accepted_keys"] == sorted([_key(ENROLLED), _key(ADDED)])
    rows, on_tablet = await _snapshot(client, tablet)
    assert rows == {ENROLLED: "ntust_portal", ADDED: "ntust_portal"}
    assert on_tablet == {}


async def test_a_course_added_by_hand_twice_comes_back_after_each_removal(client) -> None:
    phone = await _login(client, "iphone-twice", "ios")
    await _upload(client, phone, [ENROLLED])
    for _ in range(2):
        await _upload(client, phone, [ENROLLED, ADDED], force=[ADDED])
        await _delete(client, phone, ADDED)

    roster = await _upload(client, phone, [ENROLLED, ADDED])
    assert roster["skipped_tombstoned"] == 0


async def test_hiding_an_enrolled_course_still_binds_the_device_that_hid_it(client) -> None:
    """Added back by hand and taken back again, the course is hidden as
    before the add: the row the add put back is the enrolled course."""
    phone = await _login(client, "iphone-hide", "ios")
    await _upload(client, phone, [ENROLLED, ADDED])
    await _delete(client, phone, ENROLLED)
    _, on_phone = await _snapshot(client, phone)
    assert on_phone[ENROLLED]["deleted_by_this_device"] is True
    assert (await _upload(client, phone, [ENROLLED, ADDED]))["skipped_tombstoned"] == 1

    await _upload(client, phone, [ENROLLED, ADDED], force=[ENROLLED])
    rows, _ = await _snapshot(client, phone)
    assert rows[ENROLLED] == "ntust_portal"
    await _delete(client, phone, ENROLLED)

    _, on_phone = await _snapshot(client, phone)
    assert ENROLLED in on_phone
    assert (await _upload(client, phone, [ENROLLED, ADDED]))["skipped_tombstoned"] == 1


async def test_a_course_counts_as_added_by_hand_once_its_own_add_lands(client) -> None:
    """The iPhone sends an add with the term as the class table shows it, so
    a second course added in the same sitting can reach the server in the
    first one's upload, unforced, before its own add lands. A course added
    by hand that a roster put on the server already counts the same way."""
    phone = await _login(client, "iphone-pair", "ios")
    await _upload(client, phone, [ENROLLED, ADDED, OTHER], force=[ADDED])
    rows, _ = await _snapshot(client, phone)
    assert rows[OTHER] == "ntust_portal"
    await _upload(client, phone, [ENROLLED, ADDED, OTHER], force=[OTHER])
    rows, _ = await _snapshot(client, phone)
    assert rows[OTHER] == "user_added"
    await _upload(client, phone, [ENROLLED, ADDED, OTHER])
    rows, _ = await _snapshot(client, phone)
    assert rows[OTHER] == "user_added"

    await _delete(client, phone, OTHER)
    roster = await _upload(client, phone, [ENROLLED, ADDED, OTHER])
    assert roster["skipped_tombstoned"] == 0


async def test_an_add_landing_after_a_removal_does_not_bring_the_course_back(client) -> None:
    """Android sends an add with the term as it was when the course was
    added, which can list a course taken back by the time the add lands."""
    phone = await _login(client, "android-late-add", "android")
    await _upload(client, phone, [ENROLLED, ADDED], force=[ADDED])
    await _delete(client, phone, ADDED)

    late = await _upload(client, phone, [ENROLLED, ADDED, OTHER], force=[OTHER])
    assert late["skipped_tombstoned"] == 1
    assert late["accepted_keys"] == sorted([_key(ENROLLED), _key(OTHER)])


async def test_a_course_added_by_hand_stays_so_through_a_semester_reset(client) -> None:
    """The reset's tombstone remembers the course was added by hand, so
    adding it by hand again after the reset and removing it lets the
    roster bring it back."""
    phone = await _login(client, "iphone-reset", "ios")
    await _upload(client, phone, [ENROLLED, ADDED], force=[ADDED])
    reset = await client.delete(f"/v3/sync/courses?semester={SEMESTER}", headers=phone)
    assert reset.json() == {"deleted": 2}
    await _upload(client, phone, [ENROLLED])

    await _upload(client, phone, [ENROLLED, ADDED], force=[ADDED])
    rows, _ = await _snapshot(client, phone)
    assert rows[ADDED] == "user_added"
    await _delete(client, phone, ADDED)
    assert (await _upload(client, phone, [ENROLLED, ADDED]))["skipped_tombstoned"] == 0
