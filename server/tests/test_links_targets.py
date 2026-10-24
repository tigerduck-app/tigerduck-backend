"""What a link may be named and where it may redirect to."""

from __future__ import annotations

import pytest

from server.links.targets import InvalidTarget, is_valid_name, validate_target

ALLOWED = ["ntust.edu.tw"]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.academic.ntust.edu.tw/p/404-1048-78935.php",
        "https://ntust.edu.tw",
        "https://WWW.NTUST.EDU.TW:443/a?b=c#d",
    ],
)
def test_an_https_url_on_an_allowed_host_is_kept(url: str) -> None:
    assert validate_target(url, ALLOWED) == url


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://www.ntust.edu.tw/", "https"),
        ("ftp://www.ntust.edu.tw/", "https"),
        ("https://user:pass@www.ntust.edu.tw/", "user name"),
        ("https://evil.example@www.ntust.edu.tw/", "user name"),
        ("https://www.ntust.edu.tw/a\nb", "control"),
        ("https://www.ntust.edu.tw/\x00", "control"),
        ("https://www.ntust.edu.tw/a b", "whitespace"),
        ("https://www.ntust.edu.tw/　", "whitespace"),
        ("https:///path", "no host"),
        ("https://evil.example/", "not an allowed host"),
        ("https://ntust.edu.tw.evil.example/", "not an allowed host"),
        ("https://evilntust.edu.tw/", "not an allowed host"),
        ("https://evil.example\\.ntust.edu.tw/", "plain host name"),
        ("https://%65vil.ntust.edu.tw/", "plain host name"),
        ("https://[::1]/", "plain host name"),
        ("https://[broken/", "cannot be parsed"),
        ("https://www.ntust.edu.tw:8443/", "port other than 443"),
        ("https://www.ntust.edu.tw:80/", "port other than 443"),
        ("https://www.ntust.edu.tw:99999/", "port"),
        ("https://www.ntust.edu.tw:0/", "port"),
    ],
)
def test_a_target_that_breaks_a_rule_is_refused(url: str, reason: str) -> None:
    with pytest.raises(InvalidTarget, match=reason):
        validate_target(url, ALLOWED)


def test_the_allowed_hosts_come_from_the_setting() -> None:
    assert validate_target("https://library.example/", ["library.example"])
    with pytest.raises(InvalidTarget):
        validate_target("https://www.ntust.edu.tw/", ["library.example"])


@pytest.mark.parametrize("name", ["ntust-calendar", "a", "2026", "a" * 64])
def test_lower_case_letters_digits_and_hyphens_make_a_name(name: str) -> None:
    assert is_valid_name(name)


@pytest.mark.parametrize(
    "name", ["", "a\n", "NTUST", "ntust_calendar", "ntust.calendar", "a/b", "a" * 65, "é"]
)
def test_anything_else_is_not_a_name(name: str) -> None:
    assert not is_valid_name(name)
