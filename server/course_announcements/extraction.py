"""What the LLM is asked about one course announcement, and how its answer
is read: the course changes, the days held as usual, the make-up classes and
the exams it states, each with a span quoted from the text.

The rules and schema are the benchmarked ones with the days held as usual
added, and the text is the unmasked one the portal shows, against which
the evidence is checked.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from server.academic_calendar.terms import SCHOOL_TZ
from server.bulletins.llm.base import LLMError
from server.bulletins.llm.openai_compat import OpenAICompatibleClient, parse_json_object
from server.course_announcements.checks import ZH_WEEKDAYS
from server.db import storable_text

RULES = """# 課程公告抽取規則

輸入是一則 Moodle 課程公告：標題與 Markdown 內文、發布時間（Asia/Taipei，含星期）、課號。輸出符合 `schema.json` 的 JSON。

## labels

只判斷這 8 個標籤：`exam_info`、`exam_change`、`exam_seating`、`exam_review`、`exam_prep`、`grade_release`、`class_cancel`、`class_change`。定義見 `labeling_guide.md`。都不符合就是空陣列。

## course_changes：這門課既有上課的變動

- `cancelled`：停課。`room`：改教室，`room` 填新教室。`start` / `end`：上課時間提早或延後，填新的 `HH:mm`。`note`：其他變動，例如改線上、改非同步，`note_zh` 寫 40 字內的中文說明。
- 每個受影響的日期一筆，`mode` 為 `day`，`end_date` 等於 `start_date`。原文明寫一段連續期間（「12/1 到 12/15 停課」）才用 `range`。`excluded_weekdays` 除非原文寫明，一律空陣列。
- `periods`：原文寫了節次才填（NTUST 節次字串，如 "3"、"A"），否則 null。沒寫的欄位一律 null。
- 補課不放這裡，放 `makeup_classes`。照常上課不放這裡，放 `held_as_usual`。

## held_as_usual：照常上課的日期

- 原文明寫某天照常上課（例如「下週一照常上課」「11/18 不停課，照常上課」）時，每個日期一筆，填 `date`。
- 沒寫明照常上課的日期不要填。

## 日期

- 以發布時間換算。「下週四」是發布日下一個日曆週（週一開始）的星期四；「本週五」是發布日所在週的星期五。
- 只寫月日（「12/3」）時，選離發布日最近的年份。
- 原文同時寫日期與星期而兩者不符時，用寫明的日期，`certain` 設 false。
- 換算不出日期（例如只寫「本週的課」、沒寫星期）就不輸出這筆事實，`certain` 設 false。

## makeup_classes 與 exams

- `makeup_classes`：另外加開的補課，填日期、時間或節次、教室。
- `exams`：有寫考試的公告。`kind` 是 `midterm`（期中）、`final`（期末）、`quiz`（小考）或 `other`。沒寫日期時 `date` 為 null。`scope` 是 40 字內的考試範圍，沒寫就 null。

## evidence

每筆事實的 `evidence` 是原文中**逐字**出現的片段，取能支持該事實（日期與變動）的最短一段，不改字、不補字、不翻譯。

## 不要抽取

- 條件式或未定的內容：「若颱風停班則停課」「預計下週改線上」。這種公告 `certain` 設 false。
- 作業、成績、問卷等與上課時間地點無關的內容：陣列留空。
- 已經發生的日期照樣抽取，是否過期由後端判斷。

## certain

只有在每一筆事實都是原文明寫、日期換算唯一、沒有條件或含糊的情況下才是 true。沒有任何事實、而公告確實與停課調課考試無關時也是 true。

只輸出符合 schema 的 JSON，不要輸出其他文字。"""

_DATE = {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}
_HHMM = {"type": ["string", "null"], "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"}
_TEXT = {"type": ["string", "null"]}
_PERIODS = {"type": ["array", "null"], "items": {"type": "string"}}
_EVIDENCE = {"type": "string"}


def _facts(**properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        },
    }


SCHEMA_NAME = "course_announcement"
LABELS = (
    "exam_info",
    "exam_change",
    "exam_seating",
    "exam_review",
    "exam_prep",
    "grade_release",
    "class_cancel",
    "class_change",
)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "labels",
        "course_changes",
        "held_as_usual",
        "makeup_classes",
        "exams",
        "certain",
    ],
    "properties": {
        "labels": {"type": "array", "items": {"enum": list(LABELS)}},
        "course_changes": _facts(
            kind={"enum": ["cancelled", "room", "start", "end", "note"]},
            mode={"enum": ["day", "range"]},
            start_date=_DATE,
            end_date=_DATE,
            excluded_weekdays={
                "type": "array",
                "items": {"type": "integer", "minimum": 1, "maximum": 7},
            },
            periods=_PERIODS,
            room=_TEXT,
            start_time=_HHMM,
            end_time=_HHMM,
            note_zh=_TEXT,
            evidence=_EVIDENCE,
        ),
        "held_as_usual": _facts(date=_DATE, evidence=_EVIDENCE),
        "makeup_classes": _facts(
            date=_DATE,
            start_time=_TEXT,
            end_time=_TEXT,
            periods=_PERIODS,
            room=_TEXT,
            evidence=_EVIDENCE,
        ),
        "exams": _facts(
            kind={"enum": ["midterm", "final", "quiz", "other"]},
            date={**_DATE, "type": ["string", "null"]},
            start_time=_TEXT,
            end_time=_TEXT,
            room=_TEXT,
            scope=_TEXT,
            evidence=_EVIDENCE,
        ),
        "certain": {"type": "boolean"},
    },
}

# Thinking off, as benchmarked, and an answer cut where the benchmark cut it.
REQUEST_OPTIONS = {"chat_template_kwargs": {"enable_thinking": False}, "max_tokens": 1536}


def build_messages(
    *, text: str, posted_at: datetime, term_code: str, course_codes: Sequence[str]
) -> list[dict[str, str]]:
    posted = posted_at.astimezone(SCHOOL_TZ)
    codes = ", ".join(f"{term_code}{code}" for code in course_codes)
    return [
        {"role": "system", "content": RULES},
        {
            "role": "user",
            "content": (
                f"text:\n{text}\n\ncreated: {posted.isoformat()}\n"
                f"created_weekday: 星期{ZH_WEEKDAYS[posted.weekday()]}\n"
                f"course_codes: {codes}"
            ),
        },
    ]


async def extract(
    llm: OpenAICompatibleClient,
    *,
    text: str,
    posted_at: datetime,
    term_code: str,
    course_codes: Sequence[str],
) -> dict[str, Any]:
    """The facts the LLM reads in `text`, as `SCHEMA` shapes them. Raises
    `LLMError`, which holds no text, when no answer conforms."""
    return await llm.complete_json(
        messages=build_messages(
            text=text, posted_at=posted_at, term_code=term_code, course_codes=course_codes
        ),
        schema_name=SCHEMA_NAME,
        schema=SCHEMA,
        parse=parse_extraction,
        extra_body=REQUEST_OPTIONS,
    )


def parse_extraction(content: str) -> dict[str, Any]:
    """The answer, if it conforms to `SCHEMA` and its dates exist, with
    every string storable. A server that does not enforce the schema can
    answer anything."""
    answer = _storable(parse_json_object(content))
    if not _conforms(answer, SCHEMA) or not all(
        _is_day(fact[field])
        for key, field in (
            ("course_changes", "start_date"),
            ("course_changes", "end_date"),
            ("held_as_usual", "date"),
            ("makeup_classes", "date"),
            ("exams", "date"),
        )
        for fact in answer[key]
        if fact[field] is not None
    ):
        raise LLMError("invalid_answer")
    return answer


def _storable(value: Any) -> Any:
    if isinstance(value, str):
        return storable_text(value)
    if isinstance(value, list):
        return [_storable(item) for item in value]
    if isinstance(value, dict):
        return {key: _storable(item) for key, item in value.items()}
    return value


def _is_day(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _conforms(value: Any, schema: dict[str, Any]) -> bool:
    """`value` against the keywords `SCHEMA` uses."""
    if "enum" in schema and not any(
        value == option and type(value) is type(option) for option in schema["enum"]
    ):
        return False
    types = schema.get("type", [])
    if isinstance(types, str):
        types = [types]
    if types and not any(_is_type(value, name) for name in types):
        return False
    if isinstance(value, str) and "pattern" in schema:
        # Every pattern is anchored; a search would let `$` pass a final newline.
        return re.fullmatch(schema["pattern"], value) is not None
    if _is_type(value, "integer"):
        return schema.get("minimum", value) <= value <= schema.get("maximum", value)
    if isinstance(value, list):
        return all(_conforms(item, schema.get("items", {})) for item in value)
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and not set(value) <= set(properties):
            return False
        return set(schema.get("required", [])) <= set(value) and all(
            _conforms(item, properties[key]) for key, item in value.items() if key in properties
        )
    return True


def _is_type(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(
        value,
        {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}[name],
    )
