"""The data panel lists the names a course was renamed to. The backend keeps
a cleared one as "", so the apps hear of the clear, and the panel shows it
as no name at all."""
from __future__ import annotations

from app.routes.moodle.inspection import _custom_names


def test_a_cleared_name_is_not_a_custom_name() -> None:
    assert _custom_names('{"zh": "", "en": "OS"}') == {"en": "OS"}
    assert _custom_names({"zh": "", "en": ""}) == {}
    assert _custom_names(None) is None
