"""The rules a link's checked page must pass: fixed types, no user regex."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from selectolax.lexbor import LexborHTMLParser


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class StatusRule(_Rule):
    type: Literal["status"]
    equals: int = Field(ge=100, le=599)


class FinalHostRule(_Rule):
    type: Literal["final_host"]
    in_: list[Annotated[str, Field(min_length=1, max_length=253)]] = Field(
        alias="in", min_length=1, max_length=20
    )


class ContainsRule(_Rule):
    type: Literal["contains"]
    text: str = Field(min_length=1, max_length=200)


class HrefSuffixCountRule(_Rule):
    type: Literal["href_suffix_count"]
    suffix: str = Field(min_length=1, max_length=50)
    min: int = Field(ge=1, le=1000)


Rule = Annotated[
    StatusRule | FinalHostRule | ContainsRule | HrefSuffixCountRule,
    Field(discriminator="type"),
]
Rules = TypeAdapter(list[Rule])

def dump(rules: list) -> list[dict]:
    """The JSON stored in `links.expectations`, in the shapes the API takes."""
    return [rule.model_dump(by_alias=True) for rule in rules]


@dataclass(frozen=True)
class Page:
    status: int
    final_url: str
    body: str


def first_failure(page: Page, expectations: list[dict]) -> str | None:
    """The first rule `page` fails and what was found instead, or None."""
    try:
        rules = Rules.validate_python(expectations)
    except ValidationError:
        return "expectations: the stored rules are not valid"
    for rule in rules:
        failure = _check(rule, page)
        if failure is not None:
            return f"{rule.type}: {failure}"
    return None


def _check(rule, page: Page) -> str | None:
    if isinstance(rule, StatusRule):
        if page.status != rule.equals:
            return f"expected {rule.equals}, found {page.status}"
    elif isinstance(rule, FinalHostRule):
        host = urlsplit(page.final_url).hostname or ""
        if host not in {h.lower() for h in rule.in_}:
            return f"expected {', '.join(rule.in_)}, found {host}"
    elif isinstance(rule, ContainsRule):
        if rule.text not in page.body:
            return f"{rule.text!r} not found"
    elif isinstance(rule, HrefSuffixCountRule):
        count = href_suffix_count(page.body, rule.suffix)
        if count < rule.min:
            return f"expected at least {rule.min} links ending {rule.suffix}, found {count}"
    return None


def href_suffix_count(html: str, suffix: str) -> int:
    """Links whose href ends with `suffix`, case-insensitively, as the apps
    pick the calendar's .ics links off the page."""
    suffix = suffix.lower()
    return sum(
        1
        for node in LexborHTMLParser(html).css("a[href]")
        if (node.attributes.get("href") or "").lower().endswith(suffix)
    )
