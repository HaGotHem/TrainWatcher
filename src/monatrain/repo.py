"""Data access layer over SQLite."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime

from monatrain.timeutil import PARIS

MAX_FOLLOWS = 10


class FollowLimitError(Exception):
    pass


class DuplicateFollowError(Exception):
    pass


@dataclass(frozen=True)
class Follow:
    id: int
    user_id: str
    from_id: str
    to_id: str
    dep_time: str
    days: int
    train_number: str
    suggestion_sent: bool


@dataclass(frozen=True)
class DailyState:
    found: bool | None
    last_delay: int | None
    notified_status: str | None
    notified_delay: int | None


def _to_follow(row: sqlite3.Row) -> Follow:
    return Follow(
        id=row["id"],
        user_id=row["user_id"],
        from_id=row["from_id"],
        to_id=row["to_id"],
        dep_time=row["dep_time"],
        days=row["days"],
        train_number=row["train_number"],
        suggestion_sent=bool(row["suggestion_sent"]),
    )


def _is_unique_violation(exc: sqlite3.IntegrityError) -> bool:
    return exc.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE"


def _ts(dt: datetime) -> str:
    """Fixed-width UTC timestamp, so that string order is chronological order."""
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def _from_ts(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(PARIS)


class Repo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # --- users ---------------------------------------------------------------------------------

    def upsert_user(self, discord_id: str, username: str, avatar: str | None) -> None:
        self._conn.execute(
            "INSERT INTO users (discord_id, username, avatar) VALUES (?, ?, ?) "
            "ON CONFLICT(discord_id) DO UPDATE SET username = excluded.username, avatar = excluded.avatar",
            (discord_id, username, avatar),
        )

    def get_user(self, discord_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM users WHERE discord_id = ?", (discord_id,)).fetchone()
        return dict(row) if row else None

    def set_dm_ok(self, discord_id: str, ok: bool) -> None:
        self._conn.execute("UPDATE users SET dm_ok = ? WHERE discord_id = ?", (int(ok), discord_id))

    # --- followed trains -----------------------------------------------------------------------

    def add_follow(
        self, user_id: str, from_id: str, to_id: str, dep_time: str, days: int, train_number: str
    ) -> Follow:
        try:
            # The limit is checked inside the INSERT so that check and insert are one statement.
            cur = self._conn.execute(
                "INSERT INTO followed (user_id, from_id, to_id, dep_time, days, train_number) "
                "SELECT ?, ?, ?, ?, ?, ? WHERE (SELECT COUNT(*) FROM followed WHERE user_id = ?) < ?",
                (user_id, from_id, to_id, dep_time, days, train_number, user_id, MAX_FOLLOWS),
            )
        except sqlite3.IntegrityError as exc:
            if _is_unique_violation(exc):
                raise DuplicateFollowError(f"{from_id} -> {to_id} à {dep_time} est déjà suivi") from exc
            raise
        if cur.rowcount == 0:
            raise FollowLimitError(f"Limite de {MAX_FOLLOWS} trains suivis atteinte")
        row = self._conn.execute("SELECT * FROM followed WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _to_follow(row)

    def list_follows(self, user_id: str) -> list[Follow]:
        rows = self._conn.execute("SELECT * FROM followed WHERE user_id = ? ORDER BY id", (user_id,))
        return [_to_follow(r) for r in rows]

    def all_follows(self) -> list[Follow]:
        return [_to_follow(r) for r in self._conn.execute("SELECT * FROM followed ORDER BY id")]

    def delete_follow(self, user_id: str, follow_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM followed WHERE id = ? AND user_id = ?", (follow_id, user_id))
        return cur.rowcount > 0

    def replace_dep_time(self, user_id: str, follow_id: int, dep_time: str, train_number: str) -> bool:
        try:
            cur = self._conn.execute(
                "UPDATE followed SET dep_time = ?, train_number = ?, suggestion_sent = 0 "
                "WHERE id = ? AND user_id = ?",
                (dep_time, train_number, follow_id, user_id),
            )
        except sqlite3.IntegrityError as exc:
            if _is_unique_violation(exc):
                raise DuplicateFollowError(f"Un train est déjà suivi à {dep_time}") from exc
            raise
        return cur.rowcount > 0

    def mark_suggestion_sent(self, follow_id: int) -> None:
        self._conn.execute("UPDATE followed SET suggestion_sent = 1 WHERE id = ?", (follow_id,))

    # --- daily state ---------------------------------------------------------------------------

    def get_daily(self, follow_id: int, d: date) -> DailyState:
        row = self._conn.execute(
            "SELECT found, last_delay, notified_status, notified_delay FROM daily_state "
            "WHERE followed_id = ? AND service_date = ?",
            (follow_id, d.isoformat()),
        ).fetchone()
        if row is None:
            return DailyState(None, None, None, None)
        found = None if row["found"] is None else bool(row["found"])
        return DailyState(found, row["last_delay"], row["notified_status"], row["notified_delay"])

    def set_found(self, follow_id: int, d: date, found: bool) -> None:
        self._conn.execute(
            "INSERT INTO daily_state (followed_id, service_date, found) VALUES (?, ?, ?) "
            "ON CONFLICT(followed_id, service_date) DO UPDATE SET "
            "found = CASE WHEN found = 1 THEN 1 ELSE excluded.found END",
            (follow_id, d.isoformat(), int(found)),
        )

    def set_last_delay(self, follow_id: int, d: date, delay: int) -> None:
        self._conn.execute(
            "INSERT INTO daily_state (followed_id, service_date, last_delay) VALUES (?, ?, ?) "
            "ON CONFLICT(followed_id, service_date) DO UPDATE SET last_delay = excluded.last_delay",
            (follow_id, d.isoformat(), delay),
        )

    def set_notified(self, follow_id: int, d: date, status: str, delay: int) -> None:
        self._conn.execute(
            "INSERT INTO daily_state (followed_id, service_date, notified_status, notified_delay) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(followed_id, service_date) DO UPDATE SET "
            "notified_status = excluded.notified_status, notified_delay = excluded.notified_delay",
            (follow_id, d.isoformat(), status, delay),
        )

    def missing_streak(self, follow_id: int) -> int:
        """Number of most recent consecutive days with found = 0 (days with found NULL are ignored)."""
        rows = self._conn.execute(
            "SELECT found FROM daily_state WHERE followed_id = ? AND found IS NOT NULL "
            "ORDER BY service_date DESC",
            (follow_id,),
        )
        streak = 0
        for row in rows:
            if row["found"]:
                break
            streak += 1
        return streak

    # --- quota ---------------------------------------------------------------------------------

    def quota_count(self, day: date, consumer: str | None = None) -> int:
        if consumer is None:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM quota WHERE day = ?", (day.isoformat(),)
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM quota WHERE day = ? AND consumer = ?",
                (day.isoformat(), consumer),
            ).fetchone()
        return row[0]

    def quota_month(self, year: int, month: int) -> int:
        first = date(year, month, 1)
        following = date(year + month // 12, month % 12 + 1, 1)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM quota WHERE day >= ? AND day < ?",
            (first.isoformat(), following.isoformat()),
        ).fetchone()
        return row[0]

    def quota_incr(self, day: date, consumer: str) -> None:
        self._conn.execute(
            "INSERT INTO quota (day, consumer, count) VALUES (?, ?, 1) "
            "ON CONFLICT(day, consumer) DO UPDATE SET count = count + 1",
            (day.isoformat(), consumer),
        )

    # --- cache ---------------------------------------------------------------------------------

    def cache_get(self, key: str) -> tuple[dict, datetime] | None:
        row = self._conn.execute("SELECT body, fetched_at FROM api_cache WHERE key = ?", (key,)).fetchone()
        return (json.loads(row["body"]), _from_ts(row["fetched_at"])) if row else None

    def cache_put(self, key: str, body: dict, fetched_at: datetime, family: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO api_cache (key, fetched_at, body, family) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET fetched_at = excluded.fetched_at, body = excluded.body, "
            "family = excluded.family",
            (key, _ts(fetched_at), json.dumps(body), family),
        )

    def cache_latest(self, family: str) -> tuple[dict, datetime] | None:
        row = self._conn.execute(
            "SELECT body, fetched_at FROM api_cache WHERE family = ? ORDER BY fetched_at DESC LIMIT 1",
            (family,),
        ).fetchone()
        return (json.loads(row["body"]), _from_ts(row["fetched_at"])) if row else None

    def cache_purge(self, older_than: datetime) -> None:
        self._conn.execute("DELETE FROM api_cache WHERE fetched_at < ?", (_ts(older_than),))

    # --- kv ------------------------------------------------------------------------------------

    def kv_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
