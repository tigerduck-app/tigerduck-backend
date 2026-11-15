"""The question the LLM is asked about a course announcement and how its
answer is read, over an offline transport. Every announcement and answer
here is invented."""

from __future__ import annotations

import traceback
from datetime import UTC, datetime

import httpx
import pytest

from server.bulletins.llm.base import LLMError
from server.config import Settings
from server.course_announcements.extraction import (
    RULES,
    SCHEMA,
    build_extraction_llm,
    extract,
)
from server.tests.conftest import PRODUCTION_SECRETS, ScriptedLLM

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Friday 2026-11-13, 17:00 in Taipei.
POSTED = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
TEXT = "停課通知\n\n下週四（11/19）停課一次，請同學自行閱讀第五章。"


def _change(**fields) -> dict:
    return {
        "kind": "cancelled",
        "mode": "day",
        "start_date": "2026-11-19",
        "end_date": "2026-11-19",
        "excluded_weekdays": [],
        "periods": None,
        "room": None,
        "start_time": None,
        "end_time": None,
        "note_zh": None,
        "evidence": "下週四（11/19）停課一次",
        **fields,
    }


def _answer(**fields) -> dict:
    return {
        "labels": [],
        "course_changes": [],
        "held_as_usual": [],
        "makeup_classes": [],
        "exams": [],
        "certain": True,
        **fields,
    }


async def _extract(llm: ScriptedLLM, text: str = TEXT) -> dict:
    return await extract(
        llm, text=text, posted_at=POSTED, term_code="1151", course_codes=["CS1001301", "CS1001302"]
    )


async def test_a_cancellation_is_asked_with_the_text_post_time_and_codes():
    answer = _answer(labels=["class_cancel"], course_changes=[_change()])
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

    result = await _extract(ScriptedLLM(_answer(labels=["class_change"], course_changes=[room])))

    assert result["course_changes"] == [room]


async def test_a_notice_held_as_usual_lists_the_day_and_no_change():
    held = {"date": "2026-11-16", "evidence": "下週一照常上課"}

    result = await _extract(
        ScriptedLLM(_answer(labels=["class_change"], held_as_usual=[held])),
        text="上課通知\n\n颱風已離開，下週一照常上課。",
    )

    assert (result["course_changes"], result["held_as_usual"]) == ([], [held])


async def test_an_exam_without_a_date_is_kept():
    exam = {
        "kind": "midterm",
        "date": None,
        "start_time": None,
        "end_time": None,
        "room": None,
        "scope": "第一到第四章",
        "evidence": "期中考範圍為第一到第四章",
    }

    result = await _extract(ScriptedLLM(_answer(labels=["exam_info"], exams=[exam])))

    assert result["exams"] == [exam]


@pytest.mark.parametrize(
    "answer",
    [
        _answer(course_changes=[_change(kind="moved")]),
        _answer(course_changes=[_change(start_date="2026-02-30", end_date="2026-02-30")]),
        _answer(course_changes=[{**_change(), "author": "someone"}]),
        {key: value for key, value in _answer().items() if key != "held_as_usual"},
        _answer(certain="yes"),
        _answer(course_changes=[_change(excluded_weekdays=[8])]),
        "下週四（11/19）停課一次",
    ],
    ids=["kind", "impossible-date", "extra-field", "missing-key", "type", "weekday", "not-json"],
)
async def test_an_invalid_answer_is_retried_then_refused_without_its_text(answer):
    llm = ScriptedLLM(answer)

    with pytest.raises(LLMError) as raised:
        await _extract(llm)

    assert raised.value.code in ("invalid_answer", "invalid_json")
    assert len(llm.requests) == 3
    printed = "".join(traceback.format_exception(raised.value)) + repr(raised.value)
    assert "11/19" not in printed and "停課" not in printed


async def test_extraction_takes_the_bulletin_llm_unless_it_has_its_own():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    async def ask(settings: Settings) -> tuple[str, str]:
        llm = build_extraction_llm(
            settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        await llm.complete_json(messages=[], schema_name="probe", schema={})
        return str(sent[-1].url), llm.model

    shared = Settings(
        _env_file=None,
        llm_base_url="http://bulletins.test:40001/v1",
        llm_model="bulletin-model",
        **PRODUCTION_SECRETS,
    )
    own = shared.model_copy(
        update={
            "course_announcement_llm_base_url": "http://extraction.test:40003/v1",
            "course_announcement_llm_model": "extraction-model",
        }
    )

    assert await ask(shared) == ("http://bulletins.test:40001/v1/chat/completions", "bulletin-model")
    assert await ask(own) == ("http://extraction.test:40003/v1/chat/completions", "extraction-model")


async def test_the_extraction_llm_settings_read_their_variables(monkeypatch):
    monkeypatch.setenv("TIGERDUCK_COURSE_ANNOUNCEMENT_LLM_BASE_URL", "http://extraction.test/v1")
    monkeypatch.setenv("TIGERDUCK_COURSE_ANNOUNCEMENT_LLM_MODEL", "extraction-model")

    settings = Settings(_env_file=None, **PRODUCTION_SECRETS)

    assert settings.course_announcement_llm_base_url == "http://extraction.test/v1"
    assert settings.course_announcement_llm_model == "extraction-model"
