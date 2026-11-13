from __future__ import annotations

import time
from datetime import datetime, timedelta, tzinfo
from datetime import timezone as fixed_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .cursors import decode_cursor, encode_cursor
from .database import Library
from .errors import InvalidInput
from .searching import LANGUAGE_GROUPS


def local_zone(timezone: str, utc_offset: int) -> tzinfo:
    if not -840 <= utc_offset <= 840 or len(timezone) > 128:
        raise InvalidInput("Invalid time zone")
    if not timezone:
        return fixed_timezone(timedelta(minutes=utc_offset))
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise InvalidInput("Invalid time zone") from None


def local_day(timestamp: int, zone: tzinfo) -> str:
    return datetime.fromtimestamp(timestamp, zone).date().isoformat()


class Activity:
    def __init__(self, library: Library):
        self.library = library

    def recent(self, *, after: str = "", limit: int = 50, utc_offset: int = 0, timezone: str = "") -> dict:
        """Latest event for each book and local calendar day, newest first."""
        if not 1 <= limit <= 100:
            raise InvalidInput("Invalid history page")
        zone = local_zone(timezone, utc_offset)
        cursor = decode_cursor(after, int, int) if after else None
        with self.library.connection() as db:
            db.execute("BEGIN")
            db.create_function("local_day", 1, lambda value: local_day(value, zone), deterministic=True)
            try:
                rows = self._daily(db, cursor, limit, zone)
            except (OverflowError, OSError, ValueError):
                raise InvalidInput("Invalid history page") from None
        last = rows[limit - 1] if len(rows) > limit else None
        return {
            "items": rows[:limit],
            "after": encode_cursor(last["created_at"], last["id"]) if last else None,
        }

    @staticmethod
    def _daily(db, cursor, limit: int, zone: tzinfo) -> list[dict]:
        """Latest event per book and local day, ranking only a window of whole days."""
        anchor = (
            cursor[0]
            if cursor
            else db.execute("SELECT max(created_at) FROM reading_event WHERE is_reading=1").fetchone()[0]
        )
        if anchor is None:
            return []
        oldest = db.execute("SELECT min(created_at) FROM reading_event WHERE is_reading=1").fetchone()[0]
        if oldest is None:
            return []
        upper_day = datetime.fromtimestamp(anchor, zone).replace(hour=0, minute=0, second=0, microsecond=0)
        upper_day += timedelta(days=1)
        upper = int(upper_day.timestamp())
        days = 7
        while True:
            lower = int((upper_day - timedelta(days=days)).timestamp())
            conditions, params = ["rank=1"], [lower, upper]
            if cursor:
                conditions.append("(created_at,id)<(?,?)")
                params.extend(cursor)
            rows = [
                dict(row)
                for row in db.execute(
                    "WITH ranked AS (SELECT *,row_number() OVER (PARTITION BY book_id,"
                    "local_day(created_at) ORDER BY created_at DESC,id DESC) AS rank "
                    "FROM reading_event WHERE created_at>=? AND created_at<? AND is_reading=1) "
                    "SELECT id,book_id,created_at,page,completed,kind FROM ranked "
                    f"WHERE {' AND '.join(conditions)} ORDER BY created_at DESC,id DESC LIMIT ?",
                    (*params, limit + 1),
                )
            ]
            if len(rows) > limit or oldest >= lower:
                return rows
            days *= 4

    def statistics(self, *, days: int = 30, utc_offset: int = 0, timezone: str = "") -> dict:
        if not 1 <= days <= 366 or not -840 <= utc_offset <= 840:
            raise InvalidInput("Invalid statistics range")
        zone = local_zone(timezone, utc_offset)
        today = datetime.fromtimestamp(time.time(), zone).replace(hour=0, minute=0, second=0, microsecond=0)
        first = today - timedelta(days=days - 1)
        since = int(first.timestamp())
        with self.library.connection() as db:
            db.execute("BEGIN")
            db.create_function("local_day", 1, lambda value: local_day(value, zone), deterministic=True)
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
                    "SELECT local_day(created_at) AS day, count(DISTINCT book_id) AS books, "
                    "count(*) AS updates FROM reading_event WHERE created_at>=? "
                    "AND kind!='reset' AND is_reading=1 "
                    "GROUP BY day ORDER BY day",
                    (since,),
                )
            ]
            period = dict(
                db.execute(
                    "SELECT count(DISTINCT CASE WHEN is_reading=1 THEN book_id END) AS books, "
                    "coalesce(sum(is_reading),0) AS updates, "
                    "count(DISTINCT CASE WHEN kind='completed' THEN book_id END) AS completed "
                    "FROM reading_event WHERE created_at>=? AND kind!='reset'",
                    (since,),
                ).fetchone()
            )
            languages_by_key: dict[str, int] = {}
            for row in db.execute("SELECT language_key,count(*) FROM book GROUP BY language_key"):
                key = row[0] or "unknown"
                key = next((name for name, group in LANGUAGE_GROUPS.items() if key in group), key)
                languages_by_key[key] = languages_by_key.get(key, 0) + row[1]
            languages = [
                {"language": key, "books": count}
                for key, count in sorted(languages_by_key.items(), key=lambda item: (-item[1], item[0]))[:20]
            ]
        active_days = len(daily)
        by_day = {row["day"]: row for row in daily}
        first_date = first.date()
        daily = [
            by_day.get(day, {"day": day, "books": 0, "updates": 0})
            for day in ((first_date + timedelta(days=i)).isoformat() for i in range(days))
        ]
        return {
            "library": totals,
            "period": {**period, "days": days, "active_days": active_days},
            "daily": daily,
            "languages": languages,
        }
