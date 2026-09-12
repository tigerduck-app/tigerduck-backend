"""Backend string bundle resolution — reads app-translation's generated JSON."""

from __future__ import annotations

import pytest

from server.i18n import MISSING_KEY_SENTINEL, resolve_locale, translate


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("zh-Hant", "zh-Hant"),
        ("zh-Hant-TW", "zh-Hant"),
        ("zh-TW", "zh-Hant"),
        ("zh-HK", "yue-HK"),
        ("ja", "ja"),
        ("ja-JP", "ja"),
        ("en", "en"),
        ("en-GB", "en-GB"),
        ("EN-gb", "en-GB"),
        ("pt", "pt-PT"),
        ("en-AU", "en-GB"),
        ("xx", "en"),
        ("", "en"),
        (None, "en"),
    ],
)
def test_resolve_locale(requested, expected):
    assert resolve_locale(requested) == expected


def test_translate_returns_localized_string():
    zh = translate("notification_reauth_required_title", "zh-Hant")
    en = translate("notification_reauth_required_title", "en")
    assert zh
    assert en
    assert zh != en


def test_translate_falls_back_to_english_for_unknown_locale():
    assert translate("notification_reauth_required_title", "xx") == translate(
        "notification_reauth_required_title", "en"
    )


def test_translate_unknown_key_returns_sentinel_not_raise():
    assert translate("no_such_key_anywhere", "en") == MISSING_KEY_SENTINEL


def test_missing_bundle_directory_gives_actionable_error(monkeypatch):
    import server.i18n as i18n

    monkeypatch.setattr(i18n, "_BUNDLE_DIR", i18n.Path("/nonexistent/backend"))
    i18n.load_bundle.cache_clear()
    with pytest.raises(RuntimeError, match="app-translation"):
        i18n.load_bundle("en")
    i18n.load_bundle.cache_clear()


@pytest.mark.asyncio(loop_scope="session")
async def test_the_server_refuses_to_start_without_the_fallback_bundle(
    monkeypatch, test_settings, prepared_engine
):
    """A missing bundle must stop the process, not every push it sends.

    Without the canonical bundle every lookup raises, so the push pipeline
    would retry each server-composed push into `failed/pipeline_crash`
    while the server looked healthy -- what an image built without the
    submodule did. The lifespan is the startup path the API, the push
    pipeline and the sync worker all share.

    `skip_llm_probe` and `prepared_engine` exist for the failure this test
    guards against: if the check is dropped, the lifespan runs to
    completion against the test database instead of waiting on the LLM
    probe, and the test fails on DID NOT RAISE.
    """
    import server.i18n as i18n
    from server.main import create_app, lifespan

    app = create_app(test_settings.model_copy(update={"skip_llm_probe": True}))
    monkeypatch.setattr(i18n, "_BUNDLE_DIR", i18n.Path("/nonexistent/backend"))
    i18n.load_bundle.cache_clear()
    i18n._available.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="app-translation"):
            async with lifespan(app):
                pass
    finally:
        i18n.load_bundle.cache_clear()
        i18n._available.cache_clear()
