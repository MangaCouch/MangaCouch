from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta

from .database import Library
from .errors import InvalidInput


class Activity:
    def __init__(self, library: Library):
        self.library = library

    def recent(self, *, before: str = "", limit: int = 50, utc_offset: int | None = None) -> dict:
        if not 1 <= limit <= 100 or (utc_offset is not None and not -840 <= utc_offset <= 840):
            raise InvalidInput("Invalid history page")
        cursor = None
        if before:
            if not re.fullmatch(r"[0-9]{1,19}:[0-9]{1,19}", before):
                raise InvalidInput("Invalid history cursor")
            cursor = tuple(int(value) for value in before.split(":"))
            if any(value > 2**63 - 1 for value in cursor) or cursor[1] == 0:
                raise InvalidInput("Invalid history cursor")
        with self.library.connection() as db:
            db.execute("BEGIN")
            if utc_offset is None:
                rows = self._events(db, cursor, limit)
            else:
                rows = self._daily(db, cursor, limit, utc_offset * 60)
        cursor = f"{rows[limit - 1]['created_at']}:{rows[limit - 1]['id']}" if len(rows) > limit else None
        return {"events": rows[:limit], "before": cursor}

    @staticmethod
    def _events(db, cursor, limit: int) -> list[dict]:
        where, params = ("(created_at,id)<(?,?)", list(cursor)) if cursor else ("1", [])
        return [
            dict(row)
            for row in db.execute(
                f"SELECT id,book_id,created_at,page,completed,kind FROM reading_event WHERE {where} "
                "ORDER BY created_at DESC,id DESC LIMIT ?",
                (*params, limit + 1),
            )
        ]

    @staticmethod
    def _daily(db, cursor, limit: int, offset: int) -> list[dict]:
        """Latest event per book and local day, ranking only a window of whole days."""
        anchor = (
            cursor[0] if cursor else db.execute("SELECT max(created_at) FROM reading_event").fetchone()[0]
        )
        if anchor is None:
            return []
        upper = ((anchor + offset) // 86400 + 1) * 86400 - offset
        oldest = db.execute("SELECT min(created_at) FROM reading_event").fetchone()[0]
        days = 7
        while True:
            lower = upper - days * 86400
            conditions, params = ["rank=1"], [f"{offset // 60} minutes", lower, upper]
            if cursor:
                conditions.append("(created_at,id)<(?,?)")
                params.extend(cursor)
            rows = [
                dict(row)
                for row in db.execute(
                    "WITH ranked AS (SELECT *,row_number() OVER (PARTITION BY book_id,"
                    "date(created_at,'unixepoch',?) ORDER BY created_at DESC,id DESC) AS rank "
                    "FROM reading_event WHERE created_at>=? AND created_at<?) "
                    "SELECT id,book_id,created_at,page,completed,kind FROM ranked "
                    f"WHERE {' AND '.join(conditions)} ORDER BY created_at DESC,id DESC LIMIT ?",
                    (*params, limit + 1),
                )
            ]
            if len(rows) > limit or oldest >= lower:
                return rows
            days *= 4

    def statistics(self, *, days: int = 30, utc_offset: int = 0) -> dict:
        if not 1 <= days <= 366 or not -840 <= utc_offset <= 840:
            raise InvalidInput("Invalid statistics range")
        now = int(time.time())
        since = ((now + utc_offset * 60) // 86400 - days + 1) * 86400 - utc_offset * 60
        with self.library.connection() as db:
            db.execute("BEGIN")
            totals = dict(
                db.execute(
                    "SELECT count(*) AS books, "
                    "coalesce(sum(json_extract(b.content_json,'$.page_count')),0) AS pages, "
                    "coalesce(sum(json_extract(b.content_json,'$.byte_size')),0) AS bytes, "
                    "coalesce(sum(json_extract(p.state_json,'$.completed')=1),0) AS completed, "
                    "coalesce(sum(json_extract(p.state_json,'$.completed')=0 AND "
                    "json_extract(p.state_json,'$.resume_page') IS NOT NULL),0) AS in_progress, "
                    "coalesce(sum(json_extract(p.state_json,'$.rating') IS NOT NULL),0) AS rated "
                    "FROM book b JOIN personal p ON p.book_id=b.id"
                ).fetchone()
            )
            daily = [
                dict(row)
                for row in db.execute(
                    "SELECT date(created_at,'unixepoch',?) AS day, count(DISTINCT book_id) AS books, "
                    "count(*) AS updates FROM reading_event WHERE created_at>=? AND kind!='reset' "
                    "GROUP BY day ORDER BY day",
                    (f"{utc_offset} minutes", since),
                )
            ]
            period = dict(
                db.execute(
                    "SELECT count(DISTINCT book_id) AS books, count(*) AS updates, "
                    "count(DISTINCT CASE WHEN kind='completed' THEN book_id END) AS completed "
                    "FROM reading_event WHERE created_at>=? AND kind!='reset'",
                    (since,),
                ).fetchone()
            )
            languages = [
                dict(row)
                for row in db.execute(
                    "SELECT coalesce(nullif(json_extract(metadata_json,'$.language'),''),'unknown') "
                    "AS language, "
                    "count(*) AS books FROM book GROUP BY language ORDER BY books DESC,language LIMIT 20"
                )
            ]
        active_days = len(daily)
        by_day = {row["day"]: row for row in daily}
        first = datetime.fromtimestamp(since + utc_offset * 60, UTC).date()
        daily = [
            by_day.get(day, {"day": day, "books": 0, "updates": 0})
            for day in ((first + timedelta(days=i)).isoformat() for i in range(days))
        ]
        return {
            "library": totals,
            "period": {**period, "days": days, "active_days": active_days},
            "daily": daily,
            "languages": languages,
        }
