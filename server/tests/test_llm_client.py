"""The shared JSON-schema LLM client, over a fake transport.

Every prompt and answer here is invented.
"""

from __future__ import annotations

import json
import traceback

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from server.bulletins.llm import openai_compat
from server.bulletins.llm.base import LLMError
from server.bulletins.llm.openai_compat import (
    OpenAICompatibleClient,
    OpenAICompatibleProvider,
)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["held"],
    "properties": {"held": {"type": "boolean"}},
}
PROMPT = "Prompt text: Chen Example asked whether Friday's lab is held."
ANSWER = "Answer text: Chen Example, the lab is held"


def _answer(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _client(
    responses: list[httpx.Response], **kwargs
) -> tuple[OpenAICompatibleClient, list[httpx.Request], list[float]]:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = OpenAICompatibleClient(
        base_url="http://mock/v1/",
        api_key="sk-test",
        model="test-model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=sleep,
        **kwargs,
    )
    return client, requests, sleeps


def _messages() -> list[dict[str, str]]:
    return [{"role": "user", "content": PROMPT}]


def _rendered(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc)) + repr(exc)


async def test_schema_and_parameters_reach_the_request() -> None:
    client, requests, _ = _client([_answer('{"held": true}')], temperature=0.2)

    result = await client.complete_json(
        messages=_messages(), schema_name="HeldCheck", schema=SCHEMA
    )

    assert result == {"held": True}
    (request,) = requests
    assert request.url == "http://mock/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert json.loads(request.content) == {
        "model": "test-model",
        "temperature": 0.2,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "HeldCheck", "strict": True, "schema": SCHEMA},
        },
        "messages": _messages(),
    }


async def test_invalid_json_is_retried_twice_with_backoff_then_raises() -> None:
    client, requests, sleeps = _client([_answer("not json {{{")] * 3)

    with pytest.raises(LLMError) as raised:
        await client.complete_json(
            messages=_messages(), schema_name="HeldCheck", schema=SCHEMA
        )

    assert (raised.value.code, raised.value.status) == ("invalid_json", None)
    assert len(requests) == 3
    assert sleeps == [0.5, 1.0]


@pytest.mark.parametrize(
    "content", ["", "[]", '"text"', "[" * 100_000], ids=["empty", "array", "string", "deep"]
)
def test_an_answer_that_is_no_json_object_is_invalid_json(content: str) -> None:
    with pytest.raises(LLMError) as raised:
        openai_compat.parse_json_object(content)
    assert raised.value.code == "invalid_json"


async def test_an_answer_the_parse_refuses_is_retried() -> None:
    client, requests, _ = _client([_answer('{"held": "maybe"}'), _answer('{"held": false}')])

    def parse(content: str) -> bool:
        held = openai_compat.parse_json_object(content)["held"]
        if not isinstance(held, bool):
            raise LLMError("invalid_answer")
        return held

    result = await client.complete_json(
        messages=_messages(), schema_name="HeldCheck", schema=SCHEMA, parse=parse
    )

    assert result is False
    assert len(requests) == 2


async def test_http_error_carries_its_status() -> None:
    client, _, _ = _client([httpx.Response(503)], max_retries=0)

    with pytest.raises(LLMError) as raised:
        await client.complete_json(
            messages=_messages(), schema_name="HeldCheck", schema=SCHEMA
        )

    assert (raised.value.code, raised.value.status) == ("http_status", 503)
    assert str(raised.value) == "http_status (HTTP 503)"


async def test_no_prompt_or_answer_in_logs_or_the_error(monkeypatch) -> None:
    client, _, _ = _client(
        [
            httpx.Response(500, text=f"echo: {PROMPT}"),
            _answer(ANSWER),
            httpx.Response(200, json={"error": ANSWER}),
        ]
    )
    # The module's logger may be cached with the processors of an earlier
    # app, which capture_logs does not reach; a new one binds inside it.
    monkeypatch.setattr(openai_compat, "logger", structlog.get_logger(openai_compat.__name__))

    with capture_logs() as logs, pytest.raises(LLMError) as raised:
        await client.complete_json(
            messages=_messages(), schema_name="HeldCheck", schema=SCHEMA
        )

    assert [(log["error"], log["status"]) for log in logs] == [
        ("http_status", 500),
        ("invalid_json", None),
    ]
    assert raised.value.code == "invalid_response"
    for text in (str(logs), _rendered(raised.value)):
        assert "Chen Example" not in text


async def test_bulletin_answer_and_title_stay_out_of_logs_and_errors(monkeypatch) -> None:
    answer = {
        "canonical_org": "Chen Example's office",
        "content_tags": [],
        "title": "t",
        "summary": "s",
        "body_clean": "b",
        "importance": "normal",
    }
    responses = [_answer(json.dumps(answer))]
    provider = OpenAICompatibleProvider(
        base_url="http://mock/v1",
        api_key="sk-test",
        model="test-model",
        max_retries=0,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: responses.pop(0))
        ),
    )
    monkeypatch.setattr(openai_compat, "logger", structlog.get_logger(openai_compat.__name__))
    monkeypatch.setattr(openai_compat, "_MAX_BODY_CHARS", 10)

    # A name, not the literal, in the call: the rendered traceback quotes it.
    title = "Chen Example wins a prize"

    with capture_logs() as logs, pytest.raises(LLMError) as raised:
        await provider.classify(title=title, raw_publisher="p", body_md="b" * 20)

    assert logs == [
        {
            "event": "bulletins.llm.body_truncated",
            "original_len": 20,
            "kept_len": 10,
            "log_level": "warning",
        }
    ]
    assert raised.value.code == "invalid_answer"
    assert "Chen Example" not in _rendered(raised.value)
