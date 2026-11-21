"""The question the LLM is asked about a course announcement and how its
answer is read, over an offline transport. Every announcement and answer
here is invented."""

from __future__ import annotations

import traceback
from datetime import UTC, datetime

import pytest

from server.bulletins.llm.base import LLMError
from server.course_announcements.extraction import (
    RULES,
    SCHEMA,
    extract,
)
from server.tests.conftest import (
    ScriptedLLM,
    extracted_change,
    extraction_answer,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Friday 2026-11-13, 17:00 in Taipei.
POSTED = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
TEXT = "停課通知\n\n下週四（11/19）停課一次，請同學自行閱讀第五章。"


def _change(**fields) -> dict:
    return extracted_change(**{"evidence": "下週四（11/19）停課一次", **fields})


async def _extract(llm: ScriptedLLM, text: str = TEXT) -> dict:
    return await extract(
        llm, text=text, posted_at=POSTED, term_code="1151", course_codes=["CS1001301", "CS1001302"]
    )


async def test_a_cancellation_is_asked_with_the_text_post_time_and_codes():
    answer = extraction_answer(labels=["class_cancel"], course_changes=[_change()])
    llm = ScriptedLLM(answer)

    assert await _extract(llm) == answer

    (request,) = llm.requests
    assert request["model"] == "test-model"
    assert request["chat_template_kwargs"] == {"enable_thinking": False}
    assert request["response_format"]["json_schema"] == {
        "name": "course_announcement",
        "strict": True,
        "schema": SCHEMA,
    }
    system, user = request["messages"]
    assert system == {"role": "system", "content": RULES}
    assert user["content"] == (
        f"text:\n{TEXT}\n\ncreated: 2026-11-13T17:00:00+08:00\n"
        "created_weekday: 星期五\ncourse_codes: 1151CS1001301, 1151CS1001302"
    )


async def test_the_rules_hold_no_mask_and_ask_for_the_days_held_as_usual():
    assert "遮罩" not in RULES and "[學號]" not in RULES
    assert "held_as_usual" in RULES
    held = SCHEMA["properties"]["held_as_usual"]["items"]
    assert held["required"] == ["date", "evidence"]
    assert "held_as_usual" in SCHEMA["required"]


async def test_a_room_change_keeps_its_room_and_periods():
    room = _change(
        kind="room", room="IB-302", periods=["3", "4"], evidence="11/19 第 3、4 節改到 IB-302"
    )

    result = await _extract(
        ScriptedLLM(extraction_answer(labels=["class_change"], course_changes=[room]))
    )

    assert result["course_changes"] == [room]


async def test_a_notice_held_as_usual_lists_the_day_and_no_change():
    held = {"date": "2026-11-16", "evidence": "下週一照常上課"}

    result = await _extract(
        ScriptedLLM(extraction_answer(labels=["class_change"], held_as_usual=[held])),
        text="上課通知\n\n颱風已離開，下週一照常上課。",
    )

    assert (result["course_changes"], result["held_as_usual"]) == ([], [held])


async def test_an_exam_without_a_date_is_kept():
    exam = {
        "kind": "midterm",
        "name": "期中考",
        "date": None,
        "start_time": None,
        "end_time": None,
        "room": None,
        "scope": "第一到第四章",
        "evidence": "期中考範圍為第一到第四章",
    }

    result = await _extract(ScriptedLLM(extraction_answer(labels=["exam_info"], exams=[exam])))

    assert result["exams"] == [exam]


@pytest.mark.parametrize(
    "answer",
    [
        extraction_answer(course_changes=[_change(kind="moved")]),
        extraction_answer(course_changes=[_change(start_date="2026-02-30", end_date="2026-02-30")]),
        extraction_answer(course_changes=[{**_change(), "author": "someone"}]),
        {key: value for key, value in extraction_answer().items() if key != "held_as_usual"},
        extraction_answer(certain="yes"),
        extraction_answer(course_changes=[_change(excluded_weekdays=[8])]),
        extraction_answer(course_changes=[_change(kind="start", start_time="08:00\n")]),
        "下週四（11/19）停課一次",
    ],
    ids=[
        "kind", "impossible-date", "extra-field", "missing-key", "type", "weekday",
        "final-newline", "not-json",
    ],
)
async def test_an_invalid_answer_is_retried_then_refused_without_its_text(answer):
    llm = ScriptedLLM(answer)

    with pytest.raises(LLMError) as raised:
        await _extract(llm)

    assert raised.value.code in ("invalid_answer", "invalid_json")
    assert len(llm.requests) == 3
    printed = "".join(traceback.format_exception(raised.value)) + repr(raised.value)
    assert "11/19" not in printed and "停課" not in printed
