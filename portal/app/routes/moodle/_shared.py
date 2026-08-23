"""Shared logger for the Moodle admin routes.

Named for the package rather than this module, so it keeps the exact
logger name the single-file version had and any log filtering configured
against `app.routes.moodle` still matches.
"""
import logging

logger = logging.getLogger(__name__.rsplit(".", 1)[0])
