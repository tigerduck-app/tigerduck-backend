"""Protocol and data types shared by all LLM backends.

Having the protocol here (rather than coupling the scheduler to a specific
provider class) means tests can swap in `RecordingProvider` with zero
monkeypatching, and we can route to llama.cpp, Gemini, or OpenAI through a
single interface.

`BulletinMetadata` matches the JSON schema in `prompts.RESPONSE_SCHEMA`.
The schema is the source of truth — keep them in lockstep or responses will
fail validation in `openai_compat._parse_response`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from server.bulletins.taxonomy import CanonicalOrg, ContentTag, Importance


@dataclass(frozen=True)
class BulletinMetadata:
    """Structured output of the LLM classifier.

    * `canonical_org` — Dim-1 publisher after synonym normalization.
    * `content_tags` — Dim-2 multi-label tags (possibly empty).
    * `title` — normalized topic title (≤24 全形 chars, no decorative
      prefixes like 【】, no publisher prefix). Empty string when the
      provider has no title rewrite (e.g. `RecordingProvider` in tests).
    * `summary` — ≤60-char Chinese summary, used as the push notification
      body. Trimmed on the way out so the push envelope stays small.
    * `body_clean` — reformatted full content; factual info must be
      preserved, only filler and repetition stripped.
    * `importance` — coarse triage label used by the UI and for default
      subscription rules.
    """

    canonical_org: CanonicalOrg
    content_tags: tuple[ContentTag, ...] = field(default_factory=tuple)
    title: str = ""
    summary: str = ""
    body_clean: str = ""
    importance: Importance = Importance.normal


class LLMError(RuntimeError):
    """Raised when the LLM gives no usable answer after its retries. The
    bulletin job catches this and marks the bulletin `failed`, leaving
    `body_md` intact so a later model can retry.

    It holds a fixed `code` and, for an HTTP error, the `status`, never
    prompt or answer text: `http_status`, `timeout`, `transport`,
    `invalid_response` (no message content), `invalid_json` (the content is
    not a JSON object) or `invalid_answer` (the caller's parse refused it)."""

    def __init__(self, code: str, status: int | None = None) -> None:
        super().__init__(code if status is None else f"{code} (HTTP {status})")
        self.code = code
        self.status = status


@runtime_checkable
class LLMProvider(Protocol):
    async def classify(
        self,
        *,
        title: str,
        raw_publisher: str,
        body_md: str,
    ) -> BulletinMetadata: ...

    async def close(self) -> None: ...
