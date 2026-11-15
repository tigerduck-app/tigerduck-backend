"""The classifier service's client, against a fake service
(httpx.MockTransport). Every announcement here is invented."""

from __future__ import annotations

import json

import httpx
import pytest

from server.course_announcements.classifier import (
    ClassifierError,
    HttpClassifier,
    Post,
    RecordingClassifier,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = "http://classifier.test:40002"


def _result(post: dict) -> dict:
    return {
        "labels": ["class_cancel"],
        "scores": {"class_cancel": 0.97, "materials": 0.01},
        "text": f"{post['subject']}\n\n{post['message']}",
    }


def _service(*, max_posts: int = 64, sizes: list[int] | None = None):
    """Answers like serve.py, with 413 for a request of more than
    `max_posts` posts; records each request's size in `sizes`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            health = {"status": "ok", "threshold": 0.35, "release": "model-2026-11-15"}
            return httpx.Response(200, json=health)
        posts = json.loads(request.content)["posts"]
        if sizes is not None:
            sizes.append(len(posts))
        if len(posts) > max_posts:
            return httpx.Response(413, json={"error": "body must be 1 to 4194304 bytes"})
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


async def test_a_score_jsonb_cannot_hold_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        body = '{"results": [{"labels": [], "scores": {"materials": NaN}, "text": "a"}]}'
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    with pytest.raises(ClassifierError, match="unexpected_shape"):
        await HttpClassifier(BASE, transport=httpx.MockTransport(handler)).classify(_posts(1))


async def test_a_nul_character_in_the_text_is_dropped():
    def handler(request: httpx.Request) -> httpx.Response:
        result = {"labels": [], "scores": {"materials": 0.1}, "text": "Notice\u0000 0"}
        return httpx.Response(200, json={"results": [result]})

    [result] = await HttpClassifier(BASE, transport=httpx.MockTransport(handler)).classify(
        _posts(1)
    )
    assert result.text == "Notice 0"
