import pytest
from pydantic import ValidationError

from server.schemas import DeviceRegisterRequest


BASE = {
    "user_id": "u1",
    "device_id": "d1",
    "pts_token_hex": "abc",
}


def test_apple_iphone_accepted():
    req = DeviceRegisterRequest(
        **BASE, platform="apple", device_class="iphone"
    )
    assert req.device_class == "iphone"
    assert req.server_push_enabled is True


def test_apple_ipad_accepted():
    req = DeviceRegisterRequest(**BASE, platform="apple", device_class="ipad")
    assert req.device_class == "ipad"


def test_apple_mac_accepted():
    req = DeviceRegisterRequest(**BASE, platform="apple", device_class="mac")
    assert req.device_class == "mac"


def test_android_accepted():
    req = DeviceRegisterRequest(
        **BASE, platform="android", device_class="android"
    )
    assert req.device_class == "android"


def test_empty_device_class_accepted_for_back_compat():
    req = DeviceRegisterRequest(**BASE, platform="apple")
    assert req.device_class == ""


def test_apple_rejects_android_class():
    with pytest.raises(ValidationError):
        DeviceRegisterRequest(**BASE, platform="apple", device_class="android")


def test_android_rejects_iphone_class():
    with pytest.raises(ValidationError):
        DeviceRegisterRequest(
            **BASE, platform="android", device_class="iphone"
        )


def test_server_push_enabled_default_true():
    req = DeviceRegisterRequest(**BASE, platform="apple")
    assert req.server_push_enabled is True


def test_server_push_enabled_explicit_false():
    req = DeviceRegisterRequest(
        **BASE, platform="apple", server_push_enabled=False
    )
    assert req.server_push_enabled is False
