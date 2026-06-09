"""Pydantic request/response models for the /v3 auth endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Platform = Literal[
    "ios", "ipados", "macos", "windows", "watchos", "wearos", "android"
]


class DeviceInfo(BaseModel):
    client_device_id: str = Field(min_length=1, max_length=128)
    platform: Platform
    device_name: str | None = Field(default=None, max_length=128)
    app_version: str | None = Field(default=None, max_length=32)
    os_version: str | None = Field(default=None, max_length=32)


class LoginRequest(BaseModel):
    student_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    moodle_token: str = Field(min_length=1, max_length=256)
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
