"""Localized strings for server-composed push copy.

Source of truth is the `app-translation` submodule; this module reads the
`generated/backend/<locale>.json` bundles it produces (each one is
`shared ∪ backend`, flattened and key-sorted).

Locale resolution is BCP-47 by truncation — `zh-Hant-TW` tries `zh-Hant-TW`,
then `zh-Hant`, then `zh` — plus an explicit alias table for the tags that
truncation cannot reach. It deliberately does NOT reuse
`app-translation/config/locales.json`: those alias maps exist to name Android
resource directories and Apple `.lproj` folders, and mapping a tag to a
directory is not the same question as mapping it to a bundle.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

CANONICAL_LOCALE = "en"

# Returned instead of raising when a key is absent. A push with a visibly
# broken string is a bug report; a push that raised would be silence.
MISSING_KEY_SENTINEL = "??"

_BUNDLE_DIR = Path(__file__).resolve().parents[1] / "app-translation" / "generated" / "backend"

# Tags whose bundle cannot be reached by truncating subtags.
_ALIASES: dict[str, str] = {
    "zh": "zh-Hans",
    "zh-tw": "zh-Hant",
    "zh-cn": "zh-Hans",
    "zh-sg": "zh-Hans",
    "zh-hk": "yue-HK",
    "zh-mo": "yue-HK",
    "tl": "fil",
    "nb": "no",
    "in": "id",
    "iw": "he",
    # `en` and `pt` each have more than one regional bundle; a bare or
    # cross-region tag needs an explicit pointer to the right one instead
    # of falling through to the canonical English bundle.
    "pt": "pt-PT",
    "en-au": "en-GB",
}


@lru_cache(maxsize=None)
def _available() -> dict[str, str]:
    """lowercased locale tag -> actual bundle stem."""
    if not _BUNDLE_DIR.is_dir():
        return {}
    return {p.stem.lower(): p.stem for p in _BUNDLE_DIR.glob("*.json")}


def resolve_locale(requested: str | None) -> str:
    """The bundle stem to use for `requested`, falling back to English."""
    available = _available()
    if not requested:
        return CANONICAL_LOCALE
    tag = requested.strip().replace("_", "-")
    if not tag:
        return CANONICAL_LOCALE

    parts = tag.split("-")
    while parts:
        candidate = "-".join(parts).lower()
        if candidate in available:
            return available[candidate]
        if candidate in _ALIASES:
            aliased = _ALIASES[candidate].lower()
            if aliased in available:
                return available[aliased]
        parts.pop()
    return CANONICAL_LOCALE


@lru_cache(maxsize=None)
def load_bundle(locale: str) -> dict[str, str]:
    path = _BUNDLE_DIR / f"{locale}.json"
    if not path.is_file():
        raise RuntimeError(
            f"Missing string bundle {path}. The app-translation submodule is "
            "absent or stale — run `git submodule update --init --recursive`."
        )
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def require_fallback_bundle() -> None:
    """Load the canonical bundle, or raise `load_bundle`'s actionable error.

    Called once at startup. Every lookup falls back to `CANONICAL_LOCALE`,
    so without this bundle it is not one language that breaks but every
    server-composed push: each delivery raises at send time and the
    pipeline retries it into `failed/pipeline_crash`, while the server
    otherwise looks healthy. Failing here instead stops the process.
    """
    load_bundle(CANONICAL_LOCALE)


def translate(key: str, locale: str | None) -> str:
    """Localized string for `key`, or `MISSING_KEY_SENTINEL` if absent."""
    resolved = resolve_locale(locale)
    value = load_bundle(resolved).get(key)
    if value is not None:
        return value
    if resolved != CANONICAL_LOCALE:
        value = load_bundle(CANONICAL_LOCALE).get(key)
        if value is not None:
            return value
    return MISSING_KEY_SENTINEL
