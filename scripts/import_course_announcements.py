"""Import course announcement history from a local export, or queue the
current term's announcements for extraction again after a model change.

    docker compose exec -T backend \
      python scripts/import_course_announcements.py - < history.jsonl
    docker compose exec backend \
      python scripts/import_course_announcements.py --reextract

The whole export is read and checked before anything is stored, and refused
if any record holds a field outside its format, such as an author's
(`server/course_announcements/history.py`). Safe to run again and while the
poll runs. Prints counts only.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

# Lets the script run from the backend directory, as scripts/backfill_bulletins.py does.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from server.config import Settings  # noqa: E402
from server.course_announcements.classifier import HttpClassifier  # noqa: E402
from server.course_announcements.history import (  # noqa: E402
    ExportRefused,
    import_history,
    queue_reextraction,
    read_export,
)
from server.db import build_engine, build_session_factory  # noqa: E402


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    if args.export is not None and not settings.announcement_classifier_url:
        print("! TIGERDUCK_ANNOUNCEMENT_CLASSIFIER_URL is not set")
        return 2
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    try:
        if args.reextract:
            try:
                queued = await queue_reextraction(session_factory, datetime.now(UTC))
            except Exception as exc:
                print(f"! re-extract failed, nothing queued: {type(exc).__name__}")
                return 1
            print(f"queued for extraction {queued}")
            return 0
        try:
            courses = read_export(args.export)
        except ExportRefused as exc:
            print(f"! export refused, nothing stored: {exc}")
            return 2
        try:
            result = await import_history(
                session_factory, HttpClassifier(settings.announcement_classifier_url), courses
            )
        except Exception as exc:
            # The type alone: a database error's message holds the statement's parameters.
            print(f"! import failed: {type(exc).__name__}; run it again to go on")
            return 1
        print(
            f"courses {len(courses)}, skipped without a term {result.skipped_courses}, "
            f"stored {result.stored}, classified {result.classified}"
        )
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "export",
        nargs="?",
        type=argparse.FileType("rb"),
        help="the export's JSON lines, or - for stdin",
    )
    parser.add_argument(
        "--reextract",
        action="store_true",
        help="queue the current term's announcements the route rule sends; reads no export",
    )
    args = parser.parse_args()
    if (args.export is None) == (not args.reextract):
        parser.error("give either an export or --reextract")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
