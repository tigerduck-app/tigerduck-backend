"""The classifier service's client, against a fake service
(httpx.MockTransport). Every announcement here is invented."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from server.course_announcements.classifier import (
    ClassifierError,
    HttpClassifier,
    Post,
    _without_inline_images,
)
from server.course_announcements.ingest import classify_pending, store_announcements
from server.course_announcements.models import CourseAnnouncement
from server.db import build_session_factory
from server.tests.conftest import RecordingClassifier
from server.syncjobs.moodle_client import FetchedDiscussion

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = "http://classifier.test:40002"
# serve.py's MAX_BODY_BYTES.
MAX_BODY_BYTES = 4 * 1024 * 1024
LARGE_IMAGE = "data:image/png;base64," + "A" * (5 * 1024 * 1024)


def _result(post: dict) -> dict:
    return {
        "labels": ["class_cancel"],
        "scores": {"class_cancel": 0.97, "materials": 0.01},
        "text": f"{post['subject']}\n\n{post['message']}",
    }


def _service(
    *, max_posts: int = 64, sizes: list[int] | None = None, crashing: str | None = None
):
    """Answers like serve.py, with 413 for a body over its limit or a request
    of more than `max_posts` posts, and 500 for one holding the subject
    `crashing`; records each request's size in `sizes`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            health = {"status": "ok", "threshold": 0.35, "release": "model-2026-11-15"}
            return httpx.Response(200, json=health)
        if len(request.content) > MAX_BODY_BYTES:
            return httpx.Response(413, json={"error": "body must be 1 to 4194304 bytes"})
        posts = json.loads(request.content)["posts"]
        if sizes is not None:
            sizes.append(len(posts))
        if len(posts) > max_posts:
            return httpx.Response(413, json={"error": "body must be 1 to 4194304 bytes"})
        if any(p["subject"] == crashing for p in posts):
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(200, json={"results": [_result(p) for p in posts]})

    return httpx.MockTransport(handler)


def _posts(n: int) -> list[Post]:
    return [Post(subject=f"Notice {i}", message=f"<p>Invented {i}</p>") for i in range(n)]


async def test_info_reads_the_threshold_and_release():
    info = await HttpClassifier(BASE, transport=_service()).info()
    assert (info.threshold, info.release) == (0.35, "model-2026-11-15")


@pytest.mark.parametrize("health", [{"threshold": 0.4}, {"threshold": 0.4, "release": None}])
async def test_a_service_without_a_release_reports_none(health):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=health))
    info = await HttpClassifier(BASE, transport=transport).info()
    assert (info.threshold, info.release) == (0.4, None)


async def test_posts_go_in_batches_of_64_in_order():
    sizes: list[int] = []
    classifier = HttpClassifier(BASE, transport=_service(sizes=sizes))

    results = await classifier.classify(_posts(65))

    assert sizes == [64, 1]
    assert [r.text.split("\n")[0] for r in results] == [f"Notice {i}" for i in range(65)]
    assert results[0].labels == ("class_cancel",)
    assert results[0].scores["class_cancel"] == 0.97


async def test_a_batch_too_large_is_split_in_half_and_sent_again():
    sizes: list[int] = []
    classifier = HttpClassifier(BASE, transport=_service(max_posts=2, sizes=sizes))

    results = await classifier.classify(_posts(5))

    assert sizes == [5, 2, 3, 1, 2]
    assert [r.text.split("\n")[0] for r in results] == [f"Notice {i}" for i in range(5)]


async def test_one_post_too_large_on_its_own_gets_no_result():
    classifier = HttpClassifier(BASE, transport=_service(max_posts=0))
    assert await classifier.classify(_posts(1)) == [None]


@pytest.mark.parametrize(
    ("html", "sent"),
    [
        ('<p>a</p><img alt="x" src="data:image/png;base64,iV">', '<p>a</p><img alt="x" src="">'),
        ("<IMG SRC = 'data:image/gif;base64,R0lG' width=2>", '<IMG SRC = "" width=2>'),
        ("<img src=data:image/gif;base64,R0lG>", '<img src="">'),
        ('<img title="a > b" src=" data:x">', '<img title="a > b" src="">'),
        ('<img src="https://moodle.test/a.png">', None),
        ('<img data-src="data:x" srcset="data:y">', None),
        ('<p>src="data:x"</p>', None),
    ],
)
async def test_only_the_data_of_inline_images_is_left_out(html, sent):
    assert _without_inline_images(html) == (sent or html)


async def test_a_post_with_a_large_inline_image_is_stored_as_the_service_reads_it(
    db_session, prepared_engine
):
    subject, before = "Room moved", "<p>Invented: room moved to T2-202</p>"
    created = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
    discussion = FetchedDiscussion(
        discussion_id=101,
        post_id=901,
        subject=subject,
        message=f'{before}<img src="{LARGE_IMAGE}">',
        created_at=created,
        modified_at=created,
        group_id=-1,
        visible_from=None,
        has_attachment=False,
        pinned=False,
        starred=False,
    )
    await store_announcements(
        db_session,
        term_code="1151",
        moodle_course_id=7001,
        course_codes=["CS1001301"],
        discussions=[discussion],
    )
    await db_session.commit()
    classifier = HttpClassifier(BASE, transport=_service())

    assert await classify_pending(build_session_factory(prepared_engine), classifier) == 1

    [without_data] = await classifier.classify([Post(subject, f'{before}<img src="">')])
    row = await db_session.get(CourseAnnouncement, "101-901", populate_existing=True)
    assert (row.route, row.text) == ("sent", without_data.text)


async def test_a_batch_the_service_fails_on_keeps_the_results_of_its_other_posts():
    sizes: list[int] = []
    classifier = HttpClassifier(BASE, transport=_service(sizes=sizes, crashing="Notice 2"))

    results = await classifier.classify(_posts(5))

    assert sizes == [5, 2, 3, 1, 2]
    assert [r and r.text.split("\n")[0] for r in results] == [
        "Notice 0",
        "Notice 1",
        None,
        "Notice 3",
        "Notice 4",
    ]


async def test_a_post_the_service_fails_on_waits_while_the_others_are_stored(
    db_session, prepared_engine
):
    created = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
    discussions = [
        FetchedDiscussion(
            discussion_id=100 + n,
            post_id=900 + n,
            subject=subject,
            message="<p>Invented</p>",
            created_at=created,
            modified_at=created,
            group_id=-1,
            visible_from=None,
            has_attachment=False,
            pinned=False,
            starred=False,
        )
        for n, subject in enumerate(("Room moved", "Crashes the model"), start=1)
    ]
    await store_announcements(
        db_session,
        term_code="1151",
        moodle_course_id=7001,
        course_codes=["CS1001301"],
        discussions=discussions,
    )
    await db_session.commit()
    classifier = HttpClassifier(BASE, transport=_service(crashing="Crashes the model"))

    assert await classify_pending(build_session_factory(prepared_engine), classifier) == 1

    rows = await db_session.execute(
        select(
            CourseAnnouncement.id,
            CourseAnnouncement.route,
            CourseAnnouncement.classify_failed_at,
        )
        .order_by(CourseAnnouncement.id)
        .execution_options(populate_existing=True)
    )
    [(_, stored, none), (_, waiting, failed_at)] = rows.all()
    assert (stored, none, waiting) == ("sent", None, "unclassified")
    assert failed_at is not None


async def test_a_refused_request_raises_its_status_without_the_text():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(400, json={"error": "expected 1 to 64 posts"})
    )
    with pytest.raises(ClassifierError) as caught:
        await HttpClassifier(BASE, transport=transport).classify(_posts(1))
    assert caught.value.args == ("http_400",)
    assert "Invented" not in repr(caught.value)


@pytest.mark.parametrize("call", ["info", "classify"])
async def test_the_service_down_raises_a_fixed_code(call):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    classifier = HttpClassifier(BASE, transport=httpx.MockTransport(handler))
    with pytest.raises(ClassifierError) as caught:
        await (classifier.info() if call == "info" else classifier.classify(_posts(1)))
    assert caught.value.args == ("ConnectError",)
    assert caught.value.__cause__ is None


async def test_a_result_count_that_does_not_match_is_refused():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"results": []}))
    with pytest.raises(ClassifierError, match="unexpected_shape"):
        await HttpClassifier(BASE, transport=transport).classify(_posts(1))


async def test_the_recording_fake_scores_by_subject_and_can_go_down():
    fake = RecordingClassifier({"Notice 0": {"class_cancel": 0.98}})

    first, other = await fake.classify(_posts(2))

    assert first.labels == ("class_cancel",)
    assert first.text == "Notice 0\n\n<p>Invented 0</p>"
    assert other.labels == () and other.scores["class_cancel"] == 0.0
    assert [p.subject for p in fake.posts] == ["Notice 0", "Notice 1"]
    fake.down = True
    with pytest.raises(ClassifierError):
        await fake.info()


async def test_a_score_jsonb_cannot_hold_is_no_result():
    def handler(request: httpx.Request) -> httpx.Response:
        body = '{"results": [{"labels": [], "scores": {"materials": NaN}, "text": "a"}]}'
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    classifier = HttpClassifier(BASE, transport=httpx.MockTransport(handler))
    assert await classifier.classify(_posts(1)) == [None]


async def test_a_malformed_result_is_no_result_for_its_post_alone():
    def handler(request: httpx.Request) -> httpx.Response:
        posts = json.loads(request.content)["posts"]
        results = [_result(p) for p in posts]
        results[1] = {"labels": "class_cancel", "scores": {}, "text": "x"}
        return httpx.Response(200, json={"results": results})

    results = await HttpClassifier(BASE, transport=httpx.MockTransport(handler)).classify(
        _posts(3)
    )

    assert [r and r.text.split("\n")[0] for r in results] == ["Notice 0", None, "Notice 2"]


async def test_characters_postgres_refuses_are_dropped_from_the_text():
    def handler(request: httpx.Request) -> httpx.Response:
        result = {"labels": [], "scores": {"materials": 0.1}, "text": "Notice\u0000 0\ud800"}
        body = json.dumps({"results": [result]})
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    [result] = await HttpClassifier(BASE, transport=httpx.MockTransport(handler)).classify(
        _posts(1)
    )
    assert result.text == "Notice 0"
