"""An unset TIGERDUCK_ENV is production, as it is for ./start.sh, and
production refuses an empty secret or one still at its .env.example value."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.config import BACKEND_DIR, Settings
from server.tests.conftest import PRODUCTION_SECRETS

SECRET_ENV_NAMES = (
    "TIGERDUCK_API_SHARED_SECRET",
    "TIGERDUCK_AUTH_JWT_SECRET",
    "TIGERDUCK_AUTH_REFRESH_HMAC_KEY",
    "TIGERDUCK_CREDENTIAL_KEYS",
    "TIGERDUCK_CREDENTIAL_ACTIVE_KEY_ID",
    "TIGERDUCK_DATABASE_URL",
)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("TIGERDUCK_ENV", raising=False)
    for name in SECRET_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_an_unset_env_is_production():
    assert Settings(_env_file=None, **PRODUCTION_SECRETS).env == "production"


def test_an_unset_env_refuses_to_start_without_a_shared_secret():
    with pytest.raises(ValidationError, match="TIGERDUCK_ENV=development"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("field", "name"),
    [
        ("api_shared_secret", "TIGERDUCK_API_SHARED_SECRET"),
        ("auth_jwt_secret", "TIGERDUCK_AUTH_JWT_SECRET"),
        ("auth_refresh_hmac_key", "TIGERDUCK_AUTH_REFRESH_HMAC_KEY"),
        ("credential_keys", "TIGERDUCK_CREDENTIAL_KEYS"),
    ],
)
def test_production_refuses_an_empty_secret_by_name(field, name):
    empty = {} if field == "credential_keys" else ""
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, **{**PRODUCTION_SECRETS, field: empty})
    message = str(caught.value)
    assert name in message
    assert [n for n in SECRET_ENV_NAMES if n in message] == [name]


def test_production_refuses_every_secret_left_at_its_env_example_value():
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=BACKEND_DIR / ".env.example", env="production")
    message = str(caught.value)
    for name in SECRET_ENV_NAMES:
        if name != "TIGERDUCK_CREDENTIAL_ACTIVE_KEY_ID":
            assert name in message


def test_development_still_starts_from_env_example():
    settings = Settings(_env_file=BACKEND_DIR / ".env.example")
    assert settings.env == "development"


def test_the_refusal_prints_no_setting_values():
    secrets = {**PRODUCTION_SECRETS, "api_shared_secret": "", "auth_jwt_secret": "jwt-value-x"}
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, database_url="postgresql://u:db-value-x@h/d", **secrets)
    assert "value-x" not in str(caught.value)


def test_course_announcement_settings_default_to_off():
    settings = Settings(_env_file=None, **PRODUCTION_SECRETS)
    assert settings.announcement_classifier_url == ""
    assert settings.course_announcement_requests_per_minute == 20
    assert settings.course_announcements_auto_apply is False


def test_course_announcement_settings_read_their_variables(monkeypatch):
    monkeypatch.setenv("TIGERDUCK_ANNOUNCEMENT_CLASSIFIER_URL", "http://classifier.test:40002")
    monkeypatch.setenv("TIGERDUCK_COURSE_ANNOUNCEMENT_REQUESTS_PER_MINUTE", "7")
    monkeypatch.setenv("TIGERDUCK_COURSE_ANNOUNCEMENTS_AUTO_APPLY", "true")
    settings = Settings(_env_file=None, **PRODUCTION_SECRETS)
    assert settings.announcement_classifier_url == "http://classifier.test:40002"
    assert settings.course_announcement_requests_per_minute == 7
    assert settings.course_announcements_auto_apply is True


def test_env_example_keeps_automatic_apply_off():
    assert Settings(_env_file=BACKEND_DIR / ".env.example").course_announcements_auto_apply is False
