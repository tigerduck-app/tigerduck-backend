"""Course membership by the TigerSync clock (`server/sync/membership.py`):
adds, hides, removals and resets under `X-TigerSync-Protocol: 2`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.auth.moodle import MoodleVerifyResult, StaticMoodleVerifier
from server.db import build_session_factory
from server.sync.merge import to_clock
from server.sync.models import UserChangeLog

pytestmark = pytest.mark.asyncio(loop_scope="session")

SEMESTER = "1141"
PROTOCOL = {"X-TigerSync-Protocol": "2"}


def key(course_no: str, semester: str = SEMESTER) -> str:
    return f"client:{semester}:{course_no}"


async def login(client, device: str = "iphone-abc", *, protocol: bool = True) -> dict:
    client.app.state.moodle_verifier = StaticMoodleVerifier(
        MoodleVerifyResult(ok=True, username="b11015000")
    )
    response = await client.post(
        "/v3/auth/login",
        json={
            "student_id": "B11015000",
            "password": "pw",
            "moodle_token": "tok",
            "device_info": {"client_device_id": device, "platform": "ios"},
        },
    )
    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    return {**headers, **PROTOCOL} if protocol else headers


async def upload(
    client,
    headers: dict,
    roster: list[str] = (),
    adds: dict[str, datetime] | None = None,
    semester: str = SEMESTER,
) -> dict:
    adds = adds or {}
    response = await client.post(
        "/v3/sync/courses/upload",
        headers=headers,
        json={
            "courses": [
                {"semester": semester, "course_no": no, "course_name": no}
                for no in [*roster, *adds]
            ],
            "adds": [
                {"course_key": key(no, semester), "hlc": to_clock(at)} for no, at in adds.items()
            ],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def delete(client, headers: dict, course_no: str, at: datetime, kind: str = "hide") -> dict:
    response = await client.delete(
        f"/v3/sync/courses/{key(course_no)}",
        headers=headers,
        params={"hlc": to_clock(at), "kind": kind},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def reset(client, headers: dict, at: datetime, semester: str | None = SEMESTER) -> dict:
    params = {"hlc": to_clock(at)} | ({"semester": semester} if semester else {})
    response = await client.delete("/v3/sync/courses", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def colour(client, headers: dict, course_no: str, hex_: str, at: datetime) -> dict:
    response = await client.patch(
        f"/v3/sync/courses/{SEMESTER}{course_no}/override",
        headers=headers,
        json={"color_hex": hex_, "hlc": to_clock(at)},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def snapshot(client, headers: dict) -> dict:
    response = await client.get("/v3/sync/full", headers=headers)
    assert response.status_code == 200
    return response.json()


async def shown(client, headers: dict) -> set[str]:
    return {c["course_no"] for c in (await snapshot(client, headers))["courses"]}


async def tombstones(client, headers: dict) -> dict[str, tuple[str, int]]:
    return {
        t["course_no"]: (t["kind"], t["hlc"])
        for t in (await snapshot(client, headers))["course_tombstones"]
    }


async def revision(client, headers: dict) -> int:
    return (await client.get("/v3/sync/revision", headers=headers)).json()["revision"]


@pytest.fixture
def t() -> datetime:
    return datetime.now(UTC) - timedelta(hours=2)


def at(base: datetime, minutes: int) -> datetime:
    return base + timedelta(minutes=minutes)


# --- Hide, remove and add -----------------------------------------------------


async def test_a_hide_wins_only_when_later_than_the_add(client, t) -> None:
    phone = await login(client)
    await upload(client, phone, adds={"CS1": at(t, 10)})

    early = await delete(client, phone, "CS1", at(t, 5))
    assert early == {"deleted": 0, "applied": False, "hlc": to_clock(at(t, 10))}
    assert await shown(client, phone) == {"CS1"}

    late = await delete(client, phone, "CS1", at(t, 20))
    assert late == {"deleted": 1, "applied": True, "hlc": to_clock(at(t, 20))}
    assert await shown(client, phone) == set()
    assert await tombstones(client, phone) == {"CS1": ("hide", to_clock(at(t, 20)))}


async def test_an_older_add_leaves_the_newer_add_in_charge(client, t) -> None:
    phone = await login(client)
    first = await upload(client, phone, adds={"CS1": at(t, 10)})
    assert first["adds"] == [
        {"course_key": key("CS1"), "applied": True, "hlc": to_clock(at(t, 10))}
    ]
    older = await upload(client, phone, adds={"CS1": at(t, 0)})
    assert older["adds"] == [
        {"course_key": key("CS1"), "applied": False, "hlc": to_clock(at(t, 10))}
    ]

    # A hide between the two adds loses to the newer one.
    assert (await delete(client, phone, "CS1", at(t, 5)))["applied"] is False
    assert await shown(client, phone) == {"CS1"}
    (row,) = (await snapshot(client, phone))["courses"]
    assert row["added_hlc"] == to_clock(at(t, 10))


async def test_a_hide_of_a_course_never_uploaded_binds_the_roster(client, t) -> None:
    phone = await login(client)
    hidden = await delete(client, phone, "CS1", at(t, 0))
    assert hidden == {"deleted": 0, "applied": True, "hlc": to_clock(at(t, 0))}

    answer = await upload(client, phone, roster=["CS1", "CS2"])
    assert answer["accepted_keys"] == [key("CS2")]
    assert await shown(client, phone) == {"CS2"}

    # Only a later add brings it back.
    assert (await upload(client, phone, adds={"CS1": at(t, 5)}))["adds"][0]["applied"] is True
    assert await shown(client, phone) == {"CS1", "CS2"}
    assert await tombstones(client, phone) == {}


async def course_changes(client) -> list[str]:
    factory = build_session_factory(client.app.state.engine)
    async with factory() as session:
        return list(
            (
                await session.execute(
                    select(UserChangeLog.operation)
                    .where(UserChangeLog.entity_type == "course")
                    .order_by(UserChangeLog.revision)
                )
            ).scalars()
        )


async def test_a_hide_of_a_course_never_uploaded_logs_a_delete(client, t) -> None:
    phone = await login(client)
    await delete(client, phone, "CS1", at(t, 0))
    assert await course_changes(client) == ["delete"]


async def test_a_removal_that_keeps_the_school_row_logs_an_upsert(client, t) -> None:
    phone = await login(client)
    await upload(client, phone, adds={"CS1": at(t, 0)})
    await upload(client, phone, roster=["CS1"])
    before = len(await course_changes(client))
    await delete(client, phone, "CS1", at(t, 10), kind="remove")
    assert (await course_changes(client))[before:] == ["upsert"]


async def test_the_same_hide_again_moves_only_its_stamp(client, t) -> None:
    phone = await login(client)
    await delete(client, phone, "CS1", at(t, 0))
    before = await revision(client, phone)

    again = await delete(client, phone, "CS1", at(t, 5))
    assert again == {"deleted": 0, "applied": True, "hlc": to_clock(at(t, 5))}
    assert await revision(client, phone) == before
    assert await tombstones(client, phone) == {"CS1": ("hide", to_clock(at(t, 5)))}


async def test_a_removed_course_the_roster_lists_shows_and_keeps_its_stamp(client, t) -> None:
    phone = await login(client)
    await upload(client, phone, adds={"CS1": at(t, 0)})
    removed = await delete(client, phone, "CS1", at(t, 10), kind="remove")
    assert removed["applied"] is True
    assert await shown(client, phone) == set()

    # The student enrolls; the roster lists the course.
    assert (await upload(client, phone, roster=["CS1"]))["accepted_keys"] == [key("CS1")]
    assert await shown(client, phone) == {"CS1"}
    assert await tombstones(client, phone) == {"CS1": ("remove", to_clock(at(t, 10)))}

    # A hide made before the removal arrives late and loses.
    assert (await delete(client, phone, "CS1", at(t, 5)))["applied"] is False
    assert await shown(client, phone) == {"CS1"}
    # Released apps hide every tombstone they read, so they do not get it.
    legacy = await snapshot(client, await login(client, "android-1", protocol=False))
    assert legacy["course_tombstones"] == []
    assert [c["course_no"] for c in legacy["courses"]] == ["CS1"]



@pytest.mark.parametrize("roster_first", [False, True], ids=["removal first", "roster first"])
async def test_a_removed_hand_add_the_roster_lists_shows_in_either_order(
    client, t, roster_first
) -> None:
    """The phone removes its hand-added course at 10:00 while offline; the
    Mac uploads a roster that lists it. Whichever lands first, the course
    shows from the roster and the removal keeps its stamp."""
    phone = await login(client)
    mac = await login(client, "mac-1")
    await upload(client, phone, adds={"CS1": at(t, 0)})
    if roster_first:
        await upload(client, mac, roster=["CS1"])
    removed = await delete(client, phone, "CS1", at(t, 10), kind="remove")
    assert (removed["applied"], removed["deleted"]) == (True, 0 if roster_first else 1)
    if not roster_first:
        await upload(client, mac, roster=["CS1"])

    (row,) = (await snapshot(client, mac))["courses"]
    assert (row["course_no"], row["source"], row["added_hlc"]) == ("CS1", "ntust_portal", None)
    assert await tombstones(client, mac) == {"CS1": ("remove", to_clock(at(t, 10)))}
    assert (await delete(client, phone, "CS1", at(t, 5)))["applied"] is False

async def test_the_latest_of_two_adds_of_one_key_counts(client, t) -> None:
    phone = await login(client)
    await delete(client, phone, "CS1", at(t, 20))
    response = await client.post(
        "/v3/sync/courses/upload",
        headers=phone,
        json={
            "courses": [{"semester": SEMESTER, "course_no": "CS1", "course_name": "CS1"}],
            "adds": [
                {"course_key": key("CS1"), "hlc": to_clock(at(t, 30))},
                {"course_key": key("CS1"), "hlc": to_clock(at(t, 10))},
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["adds"] == [
        {"course_key": key("CS1"), "applied": True, "hlc": to_clock(at(t, 30))}
    ]
    assert await shown(client, phone) == {"CS1"}


async def test_a_released_apps_roster_updates_a_row_beside_its_removal(client, t) -> None:
    """Their snapshot leaves that removal out, so they show the course."""
    phone = await login(client)
    android = await login(client, "android-1", protocol=False)
    await upload(client, phone, adds={"CS1": at(t, 0)})
    await upload(client, phone, roster=["CS1"])
    await delete(client, phone, "CS1", at(t, 5), kind="remove")

    response = await client.post(
        "/v3/sync/courses/upload",
        headers=android,
        json={
            "courses": [
                {
                    "semester": SEMESTER,
                    "course_no": "CS1",
                    "course_name": "CS1",
                    "classroom": "TR-313",
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["skipped_tombstoned"] == 0
    (row,) = (await snapshot(client, phone))["courses"]
    assert row["classroom"] == "TR-313"
    assert await tombstones(client, phone) == {"CS1": ("remove", to_clock(at(t, 5)))}


async def test_a_hand_add_the_roster_lists_moves_the_revision(client, t) -> None:
    """It becomes a school row, which a removal or a reset treats otherwise."""
    phone = await login(client)
    await upload(client, phone, adds={"CS1": at(t, 0)})
    before = await revision(client, phone)
    await upload(client, phone, roster=["CS1"])
    assert await revision(client, phone) > before
    (row,) = (await snapshot(client, phone))["courses"]
    assert row["source"] == "ntust_portal"


async def test_a_membership_edit_stamped_ahead_of_the_server_is_refused(client) -> None:
    phone = await login(client)
    await upload(client, phone, roster=["CS1"])
    before = await revision(client, phone)
    ahead = {"hlc": to_clock(datetime.now(UTC) + timedelta(seconds=30))}
    responses = [
        await client.delete(f"/v3/sync/courses/{key('CS1')}", headers=phone, params=ahead),
        await client.delete(
            "/v3/sync/courses", headers=phone, params={**ahead, "semester": SEMESTER}
        ),
        await client.post(
            "/v3/sync/courses/upload",
            headers=phone,
            json={
                "courses": [{"semester": SEMESTER, "course_no": "CS2", "course_name": "CS2"}],
                "adds": [{"course_key": key("CS2"), **ahead}],
            },
        ),
    ]
    assert [(r.status_code, r.json()["detail"]) for r in responses] == [
        (409, "clock_ahead")
    ] * 3
    assert await revision(client, phone) == before
    full = await snapshot(client, phone)
    assert [c["course_no"] for c in full["courses"]] == ["CS1"]
    assert (full["course_tombstones"], full["term_resets"]) == ([], {})


async def test_a_protocol_2_delete_needs_a_stamp(client) -> None:
    phone = await login(client)
    response = await client.delete(f"/v3/sync/courses/{key('CS1')}", headers=phone)
    assert (response.status_code, response.json()["detail"]) == (422, "hlc_required")
    response = await client.delete("/v3/sync/courses", headers=phone, params={"semester": SEMESTER})
    assert (response.status_code, response.json()["detail"]) == (422, "hlc_required")


async def test_an_add_must_name_a_course_it_uploads(client, t) -> None:
    phone = await login(client)
    response = await client.post(
        "/v3/sync/courses/upload",
        headers=phone,
        json={"courses": [], "adds": [{"course_key": key("CS1"), "hlc": to_clock(t)}]},
    )
    assert (response.status_code, response.json()["detail"]) == (422, "add_not_in_courses")


# --- Resets -------------------------------------------------------------------


async def test_a_reset_refuses_the_terms_edits_stamped_before_it(client, t) -> None:
    phone = await login(client)
    await upload(client, phone, roster=["CS1", "CS2"])
    assert (await reset(client, phone, at(t, 10)))["applied"] is True
    await upload(client, phone, roster=["CS1", "CS2"])

    assert (await delete(client, phone, "CS1", at(t, 5)))["applied"] is False
    assert (await upload(client, phone, adds={"CS3": at(t, 5)}))["adds"][0]["applied"] is False
    stale = await colour(client, phone, "CS2", "#FF0000", at(t, 5))
    assert (stale["color_hex"], stale["applied"]) == (None, [])
    fresh = await colour(client, phone, "CS2", "#FF0000", at(t, 15))
    assert (fresh["color_hex"], fresh["applied"]) == ("#FF0000", ["color_hex"])
    assert await shown(client, phone) == {"CS1", "CS2"}


async def test_a_reset_arriving_last_keeps_the_choices_made_after_it(client, t) -> None:
    phone = await login(client)
    await upload(client, phone, roster=["CS1", "CS2", "CS3", "CS4"], adds={"CS5": at(t, 10)})
    await delete(client, phone, "CS1", at(t, 10))
    await colour(client, phone, "CS2", "#00FF00", at(t, 10))
    names = await client.patch(
        f"/v3/sync/courses/{SEMESTER}CS2/override",
        headers=phone,
        json={"custom_name": "Old", "locale": "zh", "hlc": to_clock(at(t, 1))},
    )
    assert names.status_code == 200
    await colour(client, phone, "CS3", "#0000FF", at(t, 1))

    answer = await reset(client, phone, at(t, 5))
    assert answer == {"deleted": 2, "applied": True, "reset_hlc": to_clock(at(t, 5))}

    full = await snapshot(client, phone)
    assert {c["course_no"] for c in full["courses"]} == {"CS2", "CS5"}
    (kept,) = [o for o in full["course_overrides"] if o["course_no"] == "CS2"]
    assert (kept["color_hex"], kept["custom_names"]) == ("#00FF00", {"zh": ""})
    assert kept["custom_names_hlc"] == {"zh": to_clock(at(t, 5))}
    assert {t_["course_no"]: t_["kind"] for t_ in full["course_tombstones"]} == {
        "CS1": "hide",
        "CS3": "reset",
        "CS4": "reset",
    }
    assert full["term_resets"] == {SEMESTER: to_clock(at(t, 5))}



async def test_a_reset_clears_an_older_hand_add_despite_a_newer_colour(client, t) -> None:
    """Added by hand at 08:00 and reset at 09:00 on an offline device, while
    a colour stamped 10:00 arrived first: the course and its colour go. A
    school course with the same colour stays."""
    phone = await login(client)
    await upload(client, phone, roster=["CS2"], adds={"CS1": at(t, 0)})
    await colour(client, phone, "CS1", "#FF0000", at(t, 120))
    await colour(client, phone, "CS2", "#FF0000", at(t, 120))

    assert (await reset(client, phone, at(t, 60)))["deleted"] == 1

    full = await snapshot(client, phone)
    assert [c["course_no"] for c in full["courses"]] == ["CS2"]
    assert [(o["course_no"], o["color_hex"]) for o in full["course_overrides"]] == [
        ("CS2", "#FF0000")
    ]

async def test_an_edit_stamped_at_the_reset_is_refused(client, t) -> None:
    phone = await login(client)
    await upload(client, phone, roster=["CS1"])
    await reset(client, phone, at(t, 10))
    await upload(client, phone, roster=["CS1", "CS2"])

    tie = await colour(client, phone, "CS2", "#FF0000", at(t, 10))
    assert (tie["color_hex"], tie["applied"]) == (None, [])
    assert (await delete(client, phone, "CS1", at(t, 10)))["applied"] is False
    assert (await upload(client, phone, adds={"CS3": at(t, 10)}))["adds"][0]["applied"] is False
    assert await shown(client, phone) == {"CS1", "CS2"}


async def test_a_value_a_reset_cleared_stays_cleared_through_every_writer(client, t) -> None:
    """A newer name keeps the school course; its older colour is cleared and
    must not come back through the roster's colours or the academics PUT."""
    phone = await login(client)
    android = await login(client, "android-1", protocol=False)
    await upload(client, phone, roster=["CS2"])
    await colour(client, phone, "CS2", "#FF0000", at(t, 1))
    named = await client.patch(
        f"/v3/sync/courses/{SEMESTER}CS2/override",
        headers=phone,
        json={"custom_name": "New", "locale": "zh", "hlc": to_clock(at(t, 20))},
    )
    assert named.status_code == 200
    assert (await reset(client, phone, at(t, 10)))["deleted"] == 0

    response = await client.post(
        "/v3/sync/courses/upload",
        headers=android,
        json={
            "courses": [{"semester": SEMESTER, "course_no": "CS2", "course_name": "CS2"}],
            "course_overrides": [{"course_key": key("CS2"), "color_hex": "#FF0000"}],
        },
    )
    assert response.status_code == 200, response.text
    (course,) = (await snapshot(client, phone))["courses"]
    response = await client.put(
        f"/v3/courses/{course['id']}/override",
        headers=android,
        json={"color_hex": "#FF0000", "color_hex_updated_at": at(t, 5).isoformat()},
    )
    assert response.status_code == 200, response.text

    (kept,) = (await snapshot(client, phone))["course_overrides"]
    assert (kept["color_hex"], kept["custom_names"]) == (None, {"zh": "New"})


async def test_a_reset_clearing_an_older_name_sends_it_as_empty(client, t) -> None:
    """Released apps apply only the locales a snapshot lists."""
    phone = await login(client)
    android = await login(client, "android-1", protocol=False)
    await upload(client, phone, roster=["CS2"])
    named = await client.patch(
        f"/v3/sync/courses/{SEMESTER}CS2/override",
        headers=phone,
        json={"custom_name": "Old", "locale": "en", "hlc": to_clock(at(t, 1))},
    )
    assert named.status_code == 200
    await colour(client, phone, "CS2", "#00FF00", at(t, 20))
    await reset(client, phone, at(t, 10))

    (kept,) = (await snapshot(client, android))["course_overrides"]
    assert (kept["color_hex"], kept["custom_names"]) == ("#00FF00", {"en": ""})


async def test_the_academics_put_refuses_what_a_reset_found_unstored(client, t) -> None:
    """A newer name keeps the school course; its colour and ignore setting
    were never stored, and a value stamped before the reset must not land."""
    phone = await login(client)
    android = await login(client, "android-1", protocol=False)
    await upload(client, phone, roster=["CS2"])
    named = await client.patch(
        f"/v3/sync/courses/{SEMESTER}CS2/override",
        headers=phone,
        json={"custom_name": "New", "locale": "zh", "hlc": to_clock(at(t, 20))},
    )
    assert named.status_code == 200
    await reset(client, phone, at(t, 10))
    (course,) = (await snapshot(client, phone))["courses"]

    async def put(minutes: int) -> None:
        stamp = at(t, minutes).isoformat()
        response = await client.put(
            f"/v3/courses/{course['id']}/override",
            headers=android,
            json={
                "color_hex": "#FF0000",
                "color_hex_updated_at": stamp,
                "ignore_assignments": True,
                "ignore_assignments_updated_at": stamp,
            },
        )
        assert response.status_code == 200, response.text

    await put(5)
    (kept,) = (await snapshot(client, phone))["course_overrides"]
    assert (kept["color_hex"], kept["ignore_assignments"]) == (None, False)
    await put(15)
    (kept,) = (await snapshot(client, phone))["course_overrides"]
    assert (kept["color_hex"], kept["ignore_assignments"]) == ("#FF0000", True)


async def test_an_imported_hand_add_outlives_an_older_reset(client, t) -> None:
    phone = await login(client)
    response = await client.post(
        "/v3/sync/initial-upload",
        headers=phone,
        json={
            "courses": [
                {
                    "semester": SEMESTER,
                    "course_key": key("CS9"),
                    "course_no": "CS9",
                    "source": "user_added",
                    "course_name": "CS9",
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    # A reset made offline before the import arrives after it.
    assert (await reset(client, phone, at(t, 10)))["deleted"] == 0
    assert await shown(client, phone) == {"CS9"}


@pytest.mark.parametrize("timed", [True, False], ids=["timed", "untimed"])
async def test_an_import_keeps_the_reset_from_older_times_on_rows_it_had(client, t, timed) -> None:
    """A newer name keeps CS2, whose colour was never stored. An import timed
    before the reset, or untimed, leaves it unset, but colours a row the import
    creates. An untimed ignore setting is no decision, so it never lands."""
    phone = await login(client)
    android = await login(client, "android-1", protocol=False)
    await upload(client, phone, roster=["CS2"])
    named = await client.patch(
        f"/v3/sync/courses/{SEMESTER}CS2/override",
        headers=phone,
        json={"custom_name": "New", "locale": "zh", "hlc": to_clock(at(t, 20))},
    )
    assert named.status_code == 200
    await reset(client, phone, at(t, 10))

    stamp = at(t, 5).isoformat() if timed else None
    response = await client.post(
        "/v3/sync/initial-upload",
        headers=android,
        json={
            "courses": [
                {"semester": SEMESTER, "course_key": key(no), "course_no": no, "course_name": no}
                for no in ("CS2", "CS3")
            ],
            "course_overrides": [
                {
                    "semester": SEMESTER,
                    "course_key": key(no),
                    "color_hex": "#FF0000",
                    "color_hex_updated_at": stamp,
                    "ignore_assignments": True,
                    "ignore_assignments_updated_at": stamp,
                }
                for no in ("CS2", "CS3")
            ],
        },
    )
    assert response.status_code == 200, response.text

    overrides = {o["course_no"]: o for o in (await snapshot(client, phone))["course_overrides"]}
    assert (overrides["CS2"]["color_hex"], overrides["CS2"]["ignore_assignments"]) == (None, False)
    assert (overrides["CS3"]["color_hex"], overrides["CS3"]["ignore_assignments"]) == (
        "#FF0000",
        timed,
    )


async def test_a_roster_colour_skips_a_course_the_reset_kept(client, t) -> None:
    """A newer name keeps CS2, whose colour was never stored. The roster's
    colour fill counts as the oldest, so it leaves CS2 unset but colours CS3,
    which the upload adds after the reset."""
    phone = await login(client)
    android = await login(client, "android-1", protocol=False)
    await upload(client, phone, roster=["CS2"])
    named = await client.patch(
        f"/v3/sync/courses/{SEMESTER}CS2/override",
        headers=phone,
        json={"custom_name": "New", "locale": "zh", "hlc": to_clock(at(t, 20))},
    )
    assert named.status_code == 200
    await reset(client, phone, at(t, 10))

    response = await client.post(
        "/v3/sync/courses/upload",
        headers=android,
        json={
            "courses": [
                {"semester": SEMESTER, "course_no": no, "course_name": no} for no in ("CS2", "CS3")
            ],
            "course_overrides": [
                {"course_key": key(no), "color_hex": "#FF0000"} for no in ("CS2", "CS3")
            ],
        },
    )
    assert response.status_code == 200, response.text

    overrides = {o["course_no"]: o for o in (await snapshot(client, phone))["course_overrides"]}
    assert overrides["CS2"]["color_hex"] is None
    assert overrides["CS3"]["color_hex"] == "#FF0000"


async def test_a_second_reset_of_an_emptied_term_still_binds_released_apps(client, t) -> None:
    phone = await login(client)
    android = await login(client, "android-1", protocol=False)
    await upload(client, phone, roster=["CS1"], adds={"CS9": at(t, 0)})
    await reset(client, phone, at(t, 10))
    assert (await reset(client, phone, at(t, 20)))["applied"] is True

    stale = await upload(client, android, roster=["CS1", "CS9"])
    assert stale["skipped_tombstoned"] == 2
    assert await tombstones(client, phone) == {
        "CS1": ("reset", to_clock(at(t, 20))),
        "CS9": ("reset", to_clock(at(t, 20))),
    }


async def test_a_reset_drops_the_older_removal_beside_a_course_it_keeps(client, t) -> None:
    """Released apps would hide a course a reset tombstone names."""
    phone = await login(client)
    await upload(client, phone, adds={"CS1": at(t, 0)})
    await upload(client, phone, roster=["CS1"])
    await delete(client, phone, "CS1", at(t, 5), kind="remove")
    await colour(client, phone, "CS1", "#FF0000", at(t, 20))
    await reset(client, phone, at(t, 10))

    legacy = await snapshot(client, await login(client, "android-1", protocol=False))
    assert [c["course_no"] for c in legacy["courses"]] == ["CS1"]
    assert legacy["course_tombstones"] == []


async def test_a_released_apps_reset_of_an_empty_term_moves_the_revision(client) -> None:
    phone = await login(client)
    android = await login(client, "android-1", protocol=False)
    before = await revision(client, phone)
    response = await client.delete(
        "/v3/sync/courses", headers=android, params={"semester": SEMESTER}
    )
    assert response.status_code == 200
    assert await revision(client, phone) > before


async def test_an_older_reset_changes_nothing(client, t) -> None:
    phone = await login(client)
    await reset(client, phone, at(t, 10))
    await upload(client, phone, adds={"CS1": at(t, 20)})

    older = await reset(client, phone, at(t, 5))
    assert older == {"deleted": 0, "applied": False, "reset_hlc": to_clock(at(t, 10))}
    # Nor does a term reset older than a full reset.
    await reset(client, phone, at(t, 30), semester=None)
    term = await reset(client, phone, at(t, 25))
    assert (term["applied"], term["reset_hlc"]) == (False, to_clock(at(t, 30)))
    full = await snapshot(client, phone)
    assert (full["courses_reset_hlc"], full["term_resets"]) == (
        to_clock(at(t, 30)),
        {SEMESTER: to_clock(at(t, 10))},
    )


async def test_a_released_apps_upload_is_still_bound_by_a_protocol_2_reset(client, t) -> None:
    phone = await login(client)
    android = await login(client, "android-1", protocol=False)
    await upload(client, phone, roster=["CS1"], adds={"CS9": at(t, 0)})
    await reset(client, phone, at(t, 10))

    # The released app still holds the term as it was, the hand-added course too.
    stale = await upload(client, android, roster=["CS1", "CS9"])
    assert stale["skipped_tombstoned"] == 2
    assert await shown(client, phone) == set()

    await upload(client, phone, roster=["CS1"])
    assert await shown(client, phone) == {"CS1"}
    assert {t_["course_no"] for t_ in (await snapshot(client, android))["course_tombstones"]} == {
        "CS9"
    }


async def test_a_released_apps_reset_also_refuses_older_stamps(client, t) -> None:
    phone = await login(client)
    android = await login(client, "android-1", protocol=False)
    response = await client.delete(
        "/v3/sync/courses", headers=android, params={"semester": SEMESTER}
    )
    assert response.status_code == 200
    reset_hlc = response.json()["reset_hlc"]

    assert (await upload(client, phone, adds={"CS1": t}))["adds"][0] == {
        "course_key": key("CS1"),
        "applied": False,
        "hlc": reset_hlc,
    }
    assert (await snapshot(client, phone))["term_resets"] == {SEMESTER: reset_hlc}



async def test_turning_course_sync_off_everywhere_drops_the_resets(client, t) -> None:
    phone = await login(client)
    await reset(client, phone, at(t, 10))
    response = await client.patch(
        "/v3/devices/iphone-abc/preferences", headers=phone, json={"sync_courses": False}
    )
    assert response.status_code == 200, response.text
    assert (await snapshot(client, phone))["term_resets"] == {}

# --- The cases of tigerduck-app/tigerduck-app#689 ------------------------------


async def test_a_hide_queued_before_a_re_add_elsewhere_loses(client, t) -> None:
    phone = await login(client)
    mac = await login(client, "mac-1")
    await upload(client, mac, roster=["CS1"])
    # The phone hides the course offline at 09:00; the Mac adds it at 09:30;
    # the phone's hide lands at 10:00.
    await upload(client, mac, adds={"CS1": at(t, 30)})
    assert (await delete(client, phone, "CS1", at(t, 0)))["applied"] is False
    assert await shown(client, mac) == {"CS1"}


async def test_an_add_waiting_through_a_reset_elsewhere_loses(client, t) -> None:
    phone = await login(client)
    mac = await login(client, "mac-1")
    await upload(client, mac, roster=["CS1"])
    # The phone's add is queued at 09:00; the Mac resets the term at 09:10.
    await reset(client, mac, at(t, 10))
    waiting = await upload(client, phone, adds={"CS9": at(t, 0)})
    assert waiting["adds"][0]["applied"] is False
    assert await shown(client, mac) == set()

    # An add made after the reset stands.
    assert (await upload(client, phone, adds={"CS9": at(t, 20)}))["adds"][0]["applied"] is True
    assert await shown(client, mac) == {"CS9"}


async def test_a_retried_hide_after_a_lost_reply_changes_nothing(client, t) -> None:
    phone = await login(client)
    mac = await login(client, "mac-1")
    await upload(client, phone, roster=["CS1", "CS2"])
    # The reply to each hide is lost, and the phone sends it again.
    first = await delete(client, phone, "CS1", at(t, 0))
    before = await revision(client, phone)
    retry = await delete(client, phone, "CS1", at(t, 0))
    assert (first["applied"], retry) == (
        True,
        {"deleted": 0, "applied": False, "hlc": to_clock(at(t, 0))},
    )
    assert await revision(client, phone) == before

    # A re-add elsewhere between the hide and its retry stands.
    await delete(client, phone, "CS2", at(t, 5))
    await upload(client, mac, adds={"CS2": at(t, 10)})
    assert (await delete(client, phone, "CS2", at(t, 5)))["applied"] is False
    assert await shown(client, mac) == {"CS2"}
