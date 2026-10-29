"""The sync fetchers hand over the site-info full name they already fetch."""

from __future__ import annotations

import httpx
import pytest

from server.syncjobs.moodle_client import HttpAssignmentFetcher, HttpCourseFetcher

BASE = "https://moodle.example.test"


def _handler(request: httpx.Request) -> httpx.Response:
    fn = request.url.params.get("wsfunction")
    if fn == "core_webservice_get_site_info":
        return httpx.Response(200, json={"userid": 42, "fullname": "Test Student"})
    if fn == "core_enrol_get_users_courses":
        return httpx.Response(200, json=[])
    if fn == "mod_assign_get_submission_status":
        return httpx.Response(200, json={})
    raise AssertionError(f"unexpected wsfunction {fn}")


@pytest.mark.asyncio
async def test_the_course_fetch_hands_over_the_full_name():
    fetcher = HttpCourseFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=httpx.MockTransport(_handler)
    )
    names: list[str] = []

    await fetcher.fetch_courses(token="tok", on_fullname=names.append)

    assert names == ["Test Student"]


@pytest.mark.asyncio
async def test_the_submission_probe_hands_over_the_full_name():
    fetcher = HttpAssignmentFetcher(
        base_url=BASE, timeout_seconds=5.0, transport=httpx.MockTransport(_handler)
    )
    names: list[str] = []

    await fetcher.fetch_submission_status(
        token="tok", assignment_ids=[1], max_concurrency=1, on_fullname=names.append
    )

    assert names == ["Test Student"]
