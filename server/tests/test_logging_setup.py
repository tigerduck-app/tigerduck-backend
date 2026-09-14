"""Logging configuration, and the one thing it must never emit.

The Moodle webservice takes its access token as a `wstoken` query
parameter. httpx logs every request at INFO as
`HTTP Request: GET <full url with query> "HTTP/1.1 200 OK"`, so any log
level of INFO or below writes a live Moodle credential to stdout on every
sync -- for every user, forever, in whatever log aggregator collects it.

The Moodle client code itself is careful: it truncates error strings and
never interpolates request parameters. All of that is undone by a library
logger, which is exactly the kind of leak code review does not catch, so
it is pinned here instead.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from server.config import Settings
from server.logging_setup import configure

_TOKEN = "tok_regression_canary_do_not_log"


@pytest.fixture
def _restore_log_levels():
    """configure() mutates process-global logger state; put it back."""
    names = ("httpx", "httpcore")
    saved = {n: logging.getLogger(n).level for n in names}
    root = logging.getLogger().level
    yield
    for n, level in saved.items():
        logging.getLogger(n).setLevel(level)
    logging.getLogger().setLevel(root)


@pytest.mark.parametrize("log_level", ["DEBUG", "INFO"])
def test_httpx_never_logs_the_request_url(log_level, caplog, _restore_log_levels):
    """Even at the most verbose level an operator can select.

    Asserted against `caplog`, not captured stdout. pytest's logging
    plugin attaches its own handler to the root logger at level 0, so a
    record that is emitted at all lands there whether or not it also
    reaches the stream handler `configure` installs. Written against
    stdout, this test passes even with the leak reintroduced -- verified
    by doing exactly that.

    `caplog.set_level(0)` below matters for the same reason: without it
    the plugin's own capture level, not the httpx logger's, decides what
    is recorded, and the test would again pass for the wrong reason.
    """
    configure(Settings(log_level=log_level, env="development"))
    caplog.set_level(0)
    caplog.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        client.get(
            "https://moodle.example.test/webservice/rest/server.php",
            params={"wstoken": _TOKEN, "wsfunction": "core_webservice_get_site_info"},
        )

    assert _TOKEN not in caplog.text, (
        f"the Moodle token was logged at log_level={log_level}; "
        "check that logging_setup.configure still pins the httpx logger"
    )


def test_httpx_warnings_still_reach_the_log(caplog, _restore_log_levels):
    """The fix must silence the request line, not the whole library.

    Pinning httpx and httpcore to WARNING is only acceptable while genuine
    problems still surface; if someone later raises either to ERROR or
    disables the logger, this fails.

    The level `configure` leaves on each logger is asserted directly,
    outside any `caplog.at_level`: that context manager sets the named
    logger's own level, so a check run inside it would see its level
    rather than the one `configure` chose, and pass with the pin at ERROR.

    Then a warning is emitted with only the root capture level lowered, so
    the httpx logger's own level still decides whether it is emitted, and
    it is seen through `caplog` only if it propagates. `caplog` rather than
    captured stdout: pytest's logging plugin installs its own root handler,
    so a propagated record is visible there and never reaches the stream
    handler `configure` set up.
    """
    configure(Settings(log_level="INFO", env="development"))

    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING, name

    caplog.set_level(0)
    caplog.clear()
    logging.getLogger("httpx").warning("connection pool is full")

    assert "connection pool is full" in caplog.text
