"""Parsing the version string a client reports at registration.

iOS sends CFBundleShortVersionString ("2.0.2"); Android sends
BuildConfig.VERSION_NAME, which the F-Droid flavor suffixes
("2.0.2-fdroid", app/build.gradle.kts:145). Neither is guaranteed --
rows predating the column, and anything a modified client sends, land
here too.
"""

import pytest

from server.push.client_versions import (
    BACKEND_REMINDERS_MIN_VERSION,
    parse_app_version,
    schedules_reminders_locally,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2.0.2", (2, 0, 2)),
        ("2.1.0", (2, 1, 0)),
        ("2.0.2-fdroid", (2, 0, 2)),      # F-Droid suffix
        ("2.1.0-fdroid", (2, 1, 0)),
        ("3.0", (3, 0)),
        ("10.2.1", (10, 2, 1)),
        ("2.1.0+build7", (2, 1, 0)),
        ("  2.1.0  ", (2, 1, 0)),
        (None, None),
        ("", None),
        ("not-a-version", None),
        ("v2.1.0", None),                  # deliberately strict: no v-prefix today
    ],
)
def test_parse_app_version(raw, expected):
    assert parse_app_version(raw) == expected


def test_ordering_is_numeric_not_lexicographic():
    # "2.10.0" < "2.9.0" as strings. This is the whole reason the
    # comparison cannot live in SQL against a VARCHAR column.
    assert parse_app_version("2.10.0") > parse_app_version("2.9.0")


@pytest.mark.parametrize(
    "platform,version,expected",
    [
        ("ios", "2.0.2", True),            # still schedules locally
        ("ipados", "2.0.2", True),
        ("ios", "2.1.0", False),           # backend takes over
        ("ios", "2.2.0", False),
        ("ios", None, True),               # fail closed
        ("ios", "garbage", True),          # fail closed
        ("android", "2.1.0", True),        # Android ALWAYS schedules locally
        ("android", "9.9.9", True),
        ("macos", "2.1.0", True),          # takes no notifications at all
        ("watchos", "2.1.0", True),        # a platform added later fails closed
    ],
)
def test_schedules_reminders_locally(platform, version, expected):
    assert schedules_reminders_locally(platform, version) is expected


def test_min_version_is_the_release_that_removes_the_local_scheduler():
    assert BACKEND_REMINDERS_MIN_VERSION == (2, 1, 0)
