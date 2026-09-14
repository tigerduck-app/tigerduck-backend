"""structlog configuration — pretty console in dev, JSON in production."""

from __future__ import annotations

import logging
import sys

import structlog

from server.config import Settings


def configure(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    # httpx logs every request at INFO as `HTTP Request: GET <full url> ...`,
    # and the Moodle webservice takes its token as a `wstoken` query
    # parameter, so at INFO every sync writes a usable Moodle credential to
    # stdout in plaintext. The client code is careful never to log the token
    # itself -- it truncates error strings and omits request parameters --
    # and all of that is undone by a library logger nobody looked at.
    #
    # WARNING keeps httpx's real failures while dropping the request line.
    # If you ever need the request log back for debugging, redact the query
    # string rather than lowering this.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings.env == "development":
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
