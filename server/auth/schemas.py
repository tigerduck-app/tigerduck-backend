"""Pydantic request/response models for the /v3 auth endpoints."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

Platform = Literal[
    "ios", "ipados", "macos", "windows", "watchos", "wearos", "android", "web",
]

# The operator-facing form factors. Kept in step with
# `server.push.custom_push_targeting.TargetClass` and the `device_class`
# literal on the anonymous registration — all three describe the same set,
# and a value that only one of them knows about is a device nobody can send
# to.
DeviceClass = Literal["iphone", "ipad", "mac", "android", "android_tablet"]


class DeviceInfo(BaseModel):
    client_device_id: str = Field(min_length=1, max_length=128)
    platform: Platform
    # Form factor for operator targeting. `platform` already separates
    # ios / ipados / macos, but Android reports one value for phones and
    # tablets alike, so the distinction has to ride here. Optional: a client
    # that does not send it leaves the column empty and targeting falls back
    # to matching on `platform`, which is what every pre-column row does.
    device_class: DeviceClass | None = None
    app_version: str | None = Field(default=None, max_length=32)
    os_version: str | None = Field(default=None, max_length=32)


class LoginRequest(BaseModel):
    student_id: str = Field(min_length=1, max_length=64)
    password: str = Field(default="", max_length=256)
    moodle_token: str | None = Field(default=None, max_length=256)
    moodle_private_token: str | None = Field(default=None, max_length=256)
    device_info: DeviceInfo


class UserOut(BaseModel):
    id: str
    student_id: str | None
    display_name: str | None


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    user: UserOut
    device_id: str


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=256)


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int


class UpdateCredentialsRequest(BaseModel):
    moodle_token: str = Field(min_length=1, max_length=512)
    moodle_private_token: str | None = Field(default=None, max_length=512)


class UpdateCredentialsResponse(BaseModel):
    updated: bool


class PushTokenIn(BaseModel):
    provider: Literal["apns", "fcm"]
    token_kind: Literal["standard", "push_to_start", "live_activity_update"]
    token_value: str = Field(min_length=1, max_length=512)
    bundle_id: str | None = Field(default=None, max_length=128)
    topic: str | None = Field(default=None, max_length=160)
    environment: Literal["development", "production"] | None = None
    scope_key: str = Field(default="", max_length=160)


class DeviceRegisterV3Request(DeviceInfo):
    push_token: PushTokenIn | None = None
    cloud_sync_enabled: bool | None = None


class DeviceRegisterV3Response(BaseModel):
    device_id: str
    push_token_id: int | None


class DeviceItem(BaseModel):
    id: str
    client_device_id: str
    platform: str
    app_version: str | None
    os_version: str | None
    last_seen_at: str | None
    created_at: str


class DeviceListV3Response(BaseModel):
    items: list[DeviceItem]


class DevicePreferencesV3Request(BaseModel):
    server_push_enabled: bool | None = None
    sync_courses: bool | None = None
    sync_course_colors: bool | None = None
    sync_course_names: bool | None = None
    sync_assignments: bool | None = None
    cloud_sync_enabled: bool | None = None


class DevicePreferencesV3Response(BaseModel):
    device_id: str
    server_push_enabled: bool
    sync_courses: bool
    sync_course_colors: bool
    sync_course_names: bool
    sync_assignments: bool
    cloud_sync_enabled: bool


class ScheduleScenario(str, Enum):
    class_preparing = "classPreparing"
    in_class = "inClass"
    assignment_urgent = "assignmentUrgent"


class ScheduleEventV3(BaseModel):
    source_id: str = Field(min_length=1, max_length=128, pattern=r"^[^:]+$")
    scenario: ScheduleScenario
    fire_at: datetime
    snapshot: dict[str, Any] = Field(default_factory=dict)


class ScheduleSyncV3Request(BaseModel):
    events: list[ScheduleEventV3] = Field(max_length=200)


class ScheduleSyncV3Response(BaseModel):
    pending: int
    replaced: int


class LiveActivityRegisterV3Request(BaseModel):
    activity_id: str = Field(min_length=1, max_length=160)
    source_id: str = Field(min_length=1, max_length=128)
    update_token_hex: str = Field(min_length=1, max_length=512)
    countdown_target: datetime
    snapshot: dict[str, Any] = Field(default_factory=dict)
    bundle_id: str = Field(default="org.ntust.app.TigerDuck", max_length=128)
    environment: str | None = Field(default=None, pattern=r"^(development|production)$")


class LiveActivityRegisterV3Response(BaseModel):
    token_id: int
    end_job_id: int | None = None
