"""Client of the course announcement classifier, `scripts/serve.py` of
tigerduck-app/announcement-classifier, which the host runs
(deploy/launchd/ai.tigerduck.classifier.plist).

The service turns each post's subject and message HTML into the text it
scores and answers with it, unmasked. Nothing here puts a post's text into
an error or a log line.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

# The most posts serve.py takes in one request (its MAX_TEXTS).
MAX_POSTS = 64
# serve.py scores about 35 ms a post on the CPU, one request at a time.
_TIMEOUT_SECONDS = 60.0


class ClassifierError(Exception):
    """The service did not classify. The message is a fixed code."""


@dataclass(frozen=True)
class Post:
    subject: str
    message: str


@dataclass(frozen=True)
class ClassifierInfo:
    threshold: float
    # The model's release tag, None for a model not installed from a release.
    release: str | None


@dataclass(frozen=True)
class Classification:
    # The labels at or above the service's threshold.
    labels: tuple[str, ...]
    scores: dict[str, float]
    # The title, a blank line and the message as Markdown.
    text: str


class Classifier(Protocol):
    async def info(self) -> ClassifierInfo: ...

    async def classify(self, posts: Sequence[Post]) -> list[Classification | None]:
        """One result per post, None for a post too large for the service."""
        ...


def _number(value: object) -> bool:
    # Python's JSON parser takes NaN and Infinity, which jsonb refuses.
    return type(value) in (int, float) and math.isfinite(value)


def _parse_result(item: object) -> Classification | None:
    if not isinstance(item, dict):
        return None
    labels, scores, text = item.get("labels"), item.get("scores"), item.get("text")
    if not (
        isinstance(labels, list)
        and all(isinstance(label, str) for label in labels)
        and isinstance(scores, dict)
        and all(isinstance(k, str) and _number(v) for k, v in scores.items())
        and isinstance(text, str)
    ):
        return None
    # Postgres refuses NUL in text.
    return Classification(
        labels=tuple(labels), scores=dict(scores), text=text.replace("\x00", "")
    )


class HttpClassifier:
    def __init__(
        self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url, timeout=_TIMEOUT_SECONDS, transport=self._transport
        )

    async def info(self) -> ClassifierInfo:
        try:
            async with self._client() as client:
                response = await client.get("/health")
        except httpx.HTTPError as exc:
            raise ClassifierError(type(exc).__name__) from None
        if response.status_code != 200:
            raise ClassifierError(f"http_{response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise ClassifierError("invalid_json") from None
        threshold = body.get("threshold") if isinstance(body, dict) else None
        # Missing from a service older than the release field.
        release = body.get("release") if isinstance(body, dict) else None
        if not _number(threshold) or not 0 < threshold < 1:
            raise ClassifierError("unexpected_shape")
        if release is not None and not isinstance(release, str):
            raise ClassifierError("unexpected_shape")
        return ClassifierInfo(threshold=float(threshold), release=release or None)

    async def classify(self, posts: Sequence[Post]) -> list[Classification | None]:
        results: list[Classification | None] = []
        try:
            async with self._client() as client:
                for start in range(0, len(posts), MAX_POSTS):
                    results += await self._batch(client, posts[start : start + MAX_POSTS])
        except httpx.HTTPError as exc:
            raise ClassifierError(type(exc).__name__) from None
        return results

    async def _batch(
        self, client: httpx.AsyncClient, posts: Sequence[Post]
    ) -> list[Classification | None]:
        response = await client.post(
            "/v1/classify",
            json={"posts": [{"subject": p.subject, "message": p.message} for p in posts]},
        )
        if response.status_code == 413:
            # The body is over the service's 4 MB.
            if len(posts) == 1:
                return [None]
            half = len(posts) // 2
            return await self._batch(client, posts[:half]) + await self._batch(
                client, posts[half:]
            )
        if response.status_code != 200:
            raise ClassifierError(f"http_{response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise ClassifierError("invalid_json") from None
        items = body.get("results") if isinstance(body, dict) else None
        if not isinstance(items, list) or len(items) != len(posts):
            raise ClassifierError("unexpected_shape")
        parsed = [_parse_result(item) for item in items]
        if any(result is None for result in parsed):
            raise ClassifierError("unexpected_shape")
        return parsed


# The service's labels (tigerduck-app/announcement-classifier, labeling_guide.md).
_LABELS = (
    "exam_info",
    "exam_change",
    "exam_seating",
    "exam_review",
    "exam_prep",
    "grade_release",
    "assignment_new",
    "assignment_change",
    "assignment_note",
    "class_cancel",
    "class_change",
    "class_prep",
    "attendance",
    "materials",
    "grading_policy",
    "course_info",
    "grouping",
    "admin",
    "survey",
    "event",
)


class RecordingClassifier:
    """Test double: scores a post by its subject from `scores` (every label
    0 otherwise), converts it as the service does, and records the posts it
    was sent. Set `down` to make every call fail as an unreachable service."""

    def __init__(
        self,
        scores: Mapping[str, Mapping[str, float]] | None = None,
        *,
        threshold: float = 0.35,
        release: str | None = "model-test",
    ) -> None:
        self.scores = dict(scores or {})
        self.threshold = threshold
        self.release = release
        self.down = False
        self.posts: list[Post] = []

    async def info(self) -> ClassifierInfo:
        if self.down:
            raise ClassifierError("ConnectError")
        return ClassifierInfo(threshold=self.threshold, release=self.release)

    async def classify(self, posts: Sequence[Post]) -> list[Classification | None]:
        if self.down:
            raise ClassifierError("ConnectError")
        self.posts += posts
        results: list[Classification | None] = []
        for post in posts:
            scores = {label: 0.0 for label in _LABELS} | dict(self.scores.get(post.subject, {}))
            results.append(
                Classification(
                    labels=tuple(k for k, v in scores.items() if v >= self.threshold),
                    scores=scores,
                    text=f"{post.subject}\n\n{post.message}".strip(),
                )
            )
        return results

