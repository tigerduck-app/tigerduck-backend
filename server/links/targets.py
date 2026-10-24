"""Which names and redirect targets a link may have.

A target is checked when it is saved, again before it is served, and on
every redirect hop the checker follows.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from urllib.parse import urlsplit

NAME_PATTERN = r"^[a-z0-9-]{1,64}$"
_NAME = re.compile(NAME_PATTERN)

# Letters, digits and hyphens only. The apps' URL parsers read a backslash as
# a slash, so `https://evil.example\.ntust.edu.tw` would take them to
# evil.example although urlsplit reports a host under ntust.edu.tw.
_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_HOSTNAME = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")


class InvalidTarget(ValueError):
    """The target may not be saved, served or followed; the message says why."""


def is_valid_name(name: str) -> bool:
    return _NAME.fullmatch(name) is not None


def host_allowed(host: str, allowed_hosts: Iterable[str]) -> bool:
    """`host` is one of `allowed_hosts` or a subdomain of one."""
    host = host.lower()
    return any(
        host == allowed or host.endswith("." + allowed)
        for allowed in (a.lower() for a in allowed_hosts)
    )


def validate_target(url: str, allowed_hosts: Iterable[str]) -> str:
    """`url` unchanged, or InvalidTarget."""
    # Before urlsplit, which silently drops tabs and newlines.
    if any(c.isspace() or unicodedata.category(c) == "Cc" for c in url):
        raise InvalidTarget("the URL holds whitespace or a control character")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise InvalidTarget("the URL cannot be parsed") from None
    if parts.scheme != "https":
        raise InvalidTarget("the URL must use https")
    if "@" in parts.netloc:
        raise InvalidTarget("the URL must not hold a user name or password")
    host = parts.hostname
    if not host:
        raise InvalidTarget("the URL has no host")
    if not _HOSTNAME.fullmatch(host):
        raise InvalidTarget(f"the host {host!r} is not a plain host name")
    try:
        port = parts.port
    except ValueError:
        port = 0
    if port not in (None, 443):
        raise InvalidTarget("the URL must not name a port other than 443")
    if not host_allowed(host, allowed_hosts):
        raise InvalidTarget(f"the host {host} is not an allowed host")
    return url
