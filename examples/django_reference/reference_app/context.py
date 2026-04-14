"""Template context processor — adds sidebar badge counts to every page.

Adds `nav_counts` dict with pending file / failed file / pending exception totals.
Keeps the overhead to a single small SQL query per page.
"""
from __future__ import annotations

from sqlalchemy import func, select

from openreversefeed.db.models import CorrectionQueue, SourceFile
from reference_app.ofr_bridge import get_session_factory


def nav_counts(request):
    try:
        Session = get_session_factory()
        with Session() as session:
            rows = session.execute(
                select(SourceFile.status, func.count()).group_by(SourceFile.status)
            ).all()
            file_counts = {row[0]: row[1] for row in rows}
            exc_count = session.execute(
                select(func.count())
                .select_from(CorrectionQueue)
                .where(CorrectionQueue.status == "pending")
            ).scalar() or 0
        return {
            "nav_counts": {
                "files_pending": file_counts.get("pending", 0) + file_counts.get("processing", 0),
                "files_failed": file_counts.get("failed", 0),
                "exceptions_pending": exc_count,
            }
        }
    except Exception:
        # Don't break the page if the DB is momentarily unreachable.
        return {"nav_counts": {"files_pending": 0, "files_failed": 0, "exceptions_pending": 0}}
