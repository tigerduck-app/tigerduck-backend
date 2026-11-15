"""Client of the course announcement classifier, `scripts/serve.py` of
tigerduck-app/announcement-classifier, which the host runs
(deploy/launchd/ai.tigerduck.classifier.plist).

The service turns each post's subject and message HTML into the text it
scores and answers with it, unmasked. Nothing here puts a post's text into
an error or a log line.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

from server.db import storable_text

# The most posts serve.py takes in one request (its MAX_TEXTS).
MAX_POSTS = 64
# serve.py scores about 35 ms a post on the CPU, one request at a time.
_TIMEOUT_SECONDS = 60.0

_IMG_TAG = re.compile(r"""<img\b(?:[^>"']|"[^"]*"|'[^']*')*>""", re.IGNORECASE)
_DATA_SRC = re.compile(
    r"""(\ssrc\s*=\s*)(?:"\s*data:[^"]*"|'\s*data:[^']*'|data:[^\s>]*)""", re.IGNORECASE
)


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
        """One result per post, None for a post too large for the service or
        whose result is malformed."""
        ...


def _number(value: object) -> bool:
    # Python's JSON parser takes NaN and Infinity, which jsonb refuses.
    return type(value) in (int, float) and math.isfinite(value)


def _without_inline_images(html: str) -> str:
    """`html` with every `<img>` whose source is a `data:` URI given an empty
    `src`, and nothing else changed."""
    # The classifier's scripts/moodle_text.py replaces each <img> without reading its src.
    return _IMG_TAG.sub(lambda tag: _DATA_SRC.sub(r'\1""', tag[0]), html)


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
    return Classification(labels=tuple(labels), scores=dict(scores), text=storable_text(text))


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
        sent = [Post(p.subject, _without_inline_images(p.message)) for p in posts]
        results: list[Classification | None] = []
        try:
            async with self._client() as client:
                for start in range(0, len(sent), MAX_POSTS):
                    results += await self._batch(client, sent[start : start + MAX_POSTS])
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
        return [_parse_result(item) for item in items]
