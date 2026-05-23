"""Pydantic request/response models for the bulletin HTTP API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from server.bulletins.taxonomy import CanonicalOrg, ContentTag


class OrgLabel(BaseModel):
    id: CanonicalOrg
    label: str


class TagLabel(BaseModel):
    id: ContentTag
    label: str


class TaxonomyResponse(BaseModel):
    """Self-describing taxonomy so the iOS UI stays aligned with the server
    enum set. `default_tags` is the conservative set the client should
    preload on first launch."""

    orgs: list[OrgLabel]
    tags: list[TagLabel]
    default_tags: list[ContentTag]


class BulletinSummary(BaseModel):
    """Summary row for list pagination.

    `title` is the raw NTUST headline as scraped; `title_clean` is the
    LLM-normalized version (≤24 全形 chars, no decorative prefixes).
    Clients should display `title_clean` and fall back to `title` when
    the LLM hasn't run yet (legacy rows or fresh pending bulletins).
    """

    id: int
    external_id: str
    title: str
    title_clean: str | None
    canonical_org: CanonicalOrg | None
    content_tags: list[ContentTag]
    importance: Literal["low", "normal", "high"] | None
    summary: str | None
    source_url: str
    posted_at: datetime | None
    is_deleted: bool
    # Free-form tag from the bulletins table — "ntust_general" for scraped
    # rows, "manual" for portal-injected ones. The iOS app ignores this
    # field; the portal uses it to label rows in the admin list.
    source: str


class BulletinDetail(BulletinSummary):
    """Full payload including the cleaned body."""

    body_clean: str | None
    body_md: str | None
    raw_publisher: str | None


class BulletinListResponse(BaseModel):
    items: list[BulletinSummary]
    next_cursor: int | None


class SubscriptionRule(BaseModel):
    id: int | None = None   # None on create, set on response
    name: str | None = Field(default=None, max_length=64)
    orgs: list[CanonicalOrg] = Field(default_factory=list)
    tags: list[ContentTag] = Field(default_factory=list)
    mode: Literal["AND", "OR"] = "AND"
    enabled: bool = True


class SubscriptionsPutRequest(BaseModel):
    """Idempotent snapshot replacement — the iOS client sends its complete
    rule list every time the settings page is saved."""

    rules: list[SubscriptionRule] = Field(default_factory=list, max_length=32)


class SubscriptionsResponse(BaseModel):
    device_id: str
    rules: list[SubscriptionRule]


# -- Admin / manual injection ----------------------------------------------
#
# The portal's /announcement page uses these to author a bulletin by hand
# (operator broadcast) or patch an LLM-classified row whose copy needs a
# tweak before it goes out. Wire format mirrors the iOS-facing fields so
# the same row that's edited here renders consistently in the app's
# Announcement tab.


class BulletinAdminCreateRequest(BaseModel):
    """Manual bulletin injection.
    Inserted with `processing_state='processed'` and `notified_at=NULL`
    so the dispatcher tick fans it out on its next pass."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    title_clean: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=2000)
    body_clean: str | None = Field(default=None, max_length=20000)
    body_md: str | None = Field(default=None, max_length=20000)
    canonical_org: CanonicalOrg
    content_tags: list[ContentTag] = Field(default_factory=list, max_length=8)
    importance: Literal["low", "normal", "high"] = "normal"
    source_url: str = Field(
        default="https://announce.ntust.edu.tw/manual",
        min_length=1,
        max_length=1000,
    )


class BulletinAdminUpdateRequest(BaseModel):
    """Patch an existing bulletin's display fields.
    Every field is optional — only provided keys are updated. The push
    fan-out doesn't re-fire on edits; this is for fixing up text that
    has already been (or will be) sent. The change is visible in the
    iOS Announcement tab on its next list refresh."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    title_clean: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=2000)
    body_clean: str | None = Field(default=None, max_length=20000)
    body_md: str | None = Field(default=None, max_length=20000)
    canonical_org: CanonicalOrg | None = None
    content_tags: list[ContentTag] | None = Field(default=None, max_length=8)
    importance: Literal["low", "normal", "high"] | None = None
