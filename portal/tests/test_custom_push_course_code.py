"""A course code that is blank once trimmed is refused rather than read as no
course, which would widen the push to every student."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.routes.custom_push import _PreviewRequest, _SendRequest

_SEND = {"title": "t", "body": "b", "keeps_record": False, "force_ring": False}


@pytest.mark.parametrize("code", ["", "   "])
@pytest.mark.parametrize("model", [_PreviewRequest, _SendRequest], ids=["preview", "send"])
def test_a_blank_course_code_is_refused(model, code) -> None:
    with pytest.raises(ValidationError):
        model(target_classes=["iphone"], course_code=code, **_SEND)


def test_a_course_code_is_trimmed_and_uppercased() -> None:
    assert _SendRequest(target_classes=["iphone"], course_code=" cs2001301 ", **_SEND).course_code == "CS2001301"
    assert _SendRequest(target_classes=["iphone"], **_SEND).course_code is None
