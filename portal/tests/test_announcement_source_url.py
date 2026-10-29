"""A new bulletin's source URL is opened from the apps and the portal, so it
must be an http or https URL."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.routes.announcement import _CreateSubmission

_BULLETIN = {"title": "t", "canonical_org": "server"}


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,x", "ftp://example.com/a", "example.com"])
def test_a_source_url_of_another_scheme_is_refused(url) -> None:
    with pytest.raises(ValidationError):
        _CreateSubmission(source_url=url, **_BULLETIN)


@pytest.mark.parametrize("url", ["https://announce.ntust.edu.tw/manual", "HTTP://example.com/a"])
def test_an_http_source_url_is_kept(url) -> None:
    assert _CreateSubmission(source_url=url, **_BULLETIN).source_url == url


def test_the_default_source_url_passes() -> None:
    assert _CreateSubmission(**_BULLETIN).source_url.startswith("https://")
