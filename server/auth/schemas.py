"""Pydantic request/response models for the /v3 auth endpoints."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Platform = Literal[
    "ios", "ipados", "macos", "windows", "watchos", "wearos", "android", "web",
]

# The operator-facing form factors. Kept in step with
# `server.push.custom_push_targeting.TargetClass` and the `device_class`
# literal on the anonymous registration — all three describe the same set,
# and a value that only one of them knows about is a device nobody can send
# to.
DeviceClass = Literal["iphone", "ipad", "mac", "android", "android_tablet"]


# Which form factors a platform can honestly report. Custom-push targeting
# matches on `device_class` alone once one is stored, so a mismatched pair
# would put a device in the wrong audience and drop it from the right one.
_APPLE_CLASSES = frozenset({"iphone", "ipad", "mac"})
_ANDROID_CLASSES = frozenset({"android", "android_tablet"})
_CLASSES_FOR_PLATFORM: dict[str, frozenset[str]] = {
    "ios": _APPLE_CLASSES,
    "ipados": _APPLE_CLASSES,
    "macos": _APPLE_CLASSES,
    "apple": _APPLE_CLASSES,
    "android": _ANDROID_CLASSES,
}


def check_device_class(platform: str, device_class: str | None) -> None:
    """Raise ValueError unless `device_class` is one `platform` can report."""
    if device_class is None:
        return
    if device_class not in _CLASSES_FOR_PLATFORM.get(platform, frozenset()):
        raise ValueError(f"device_class {device_class!r} does not fit platform {platform!r}")


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

    @model_validator(mode="after")
    def device_class_fits_platform(self) -> "DeviceInfo":
        check_device_class(self.platform, self.device_class)
        return self


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

    @model_validator(mode="after")
    def live_activity_tokens_are_apns(self) -> "PushTokenIn":
        # Live Activities are ActivityKit's; an FCM token of either kind
        # would be selected for a schedule job and handed to FCM with a
        # per-activity collapse key it cannot honour.
        if self.provider == "fcm" and self.token_kind != "standard":
            raise ValueError("fcm tokens are standard only")
        return self


class DeviceRegisterV3Request(DeviceInfo):
    push_token: PushTokenIn | None = None
    cloud_sync_enabled: bool | None = None
    # BCP-47 tag, e.g. "zh-Hant-TW". Reported unconditionally on every
    # registration call (not gated behind a preference toggle) — see
    # UserDevice.locale for why. Optional so older clients keep working.
    # max_length mirrors UserDevice.locale's String(35) column.
    locale: str | None = Field(default=None, max_length=35)


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
    # Lets the device push a changed system language between register calls
    # rather than waiting for the next app launch. See UserDevice.locale.
    # max_length mirrors UserDevice.locale's String(35) column.
    locale: str | None = Field(default=None, max_length=35)


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
    # One occurrence, not one course: the client's timetable slot id is
    # "{course_no}_{yyyyMMdd}_{period}" and an assignment's is its own id, so a
    # weekly class posts a fresh source_id each week. That is what lets
    # `ux_push_jobs_dedupe_active` keep a sent start job on record without
    # blocking next week's, and what makes `"{scenario}::{source_id}"` name
    # exactly one Live Activity.
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
