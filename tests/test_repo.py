"""Task 3: SQLite connection/schema (db.py) and the Repo data-access layer (repo.py)."""

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from monatrain.db import connect, init_schema
from monatrain.repo import DailyState, DuplicateFollowError, Follow, FollowLimitError, Repo
from monatrain.timeutil import PARIS

MONACO = "stop_area:SNCF:87756403"
NICE = "stop_area:SNCF:87756056"
D1 = date(2026, 11, 4)
D2 = date(2026, 11, 5)
D3 = date(2026, 11, 6)


def _user(repo: Repo, discord_id: str = "111") -> str:
    repo.upsert_user(discord_id, f"user{discord_id}", None)
    return discord_id


def _follow(repo: Repo, user_id: str = "111", dep_time: str = "08:04", train_number: str = "86053") -> Follow:
    return repo.add_follow(user_id, MONACO, NICE, dep_time, 31, train_number)


# --- db.py -------------------------------------------------------------------------------------


def test_connect_creates_parent_dir_and_sets_pragmas(tmp_path):
    path = tmp_path / "nested" / "deeper" / "monatrain.db"
    conn = connect(str(path))
    try:
        assert path.parent.is_dir()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_connect_allows_use_from_another_thread(tmp_path):
    import threading

    conn = connect(str(tmp_path / "t.db"))
    errors: list[BaseException] = []

    def work():
        try:
            conn.execute("SELECT 1").fetchone()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=work)
    t.start()
    t.join()
    conn.close()
    assert errors == []


def test_init_schema_is_idempotent_and_creates_tables(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    try:
        init_schema(conn)
        init_schema(conn)
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"users", "followed", "daily_state", "quota", "api_cache", "kv"} <= names
    finally:
        conn.close()


def test_data_survives_reconnect(tmp_path):
    path = str(tmp_path / "p.db")
    conn = connect(path)
    init_schema(conn)
    Repo(conn).kv_set("k", "v")
    conn.close()

    conn = connect(path)
    init_schema(conn)
    try:
        assert Repo(conn).kv_get("k") == "v"
    finally:
        conn.close()


# --- users -------------------------------------------------------------------------------------


def test_upsert_and_get_user(repo):
    assert repo.get_user("111") is None
    repo.upsert_user("111", "thomas", "abc123")
    user = repo.get_user("111")
    assert user["discord_id"] == "111"
    assert user["username"] == "thomas"
    assert user["avatar"] == "abc123"
    assert user["dm_ok"] == 1

    repo.upsert_user("111", "thomas2", None)
    user = repo.get_user("111")
    assert user["username"] == "thomas2"
    assert user["avatar"] is None


def test_set_dm_ok(repo):
    _user(repo)
    repo.set_dm_ok("111", False)
    assert not repo.get_user("111")["dm_ok"]
    repo.set_dm_ok("111", True)
    assert repo.get_user("111")["dm_ok"]


# --- followed trains ---------------------------------------------------------------------------


def test_add_follow_returns_follow(repo):
    _user(repo)
    f = repo.add_follow("111", MONACO, NICE, "08:04", 31, "86053")
    assert isinstance(f, Follow)
    assert isinstance(f.id, int)
    assert (f.user_id, f.from_id, f.to_id, f.dep_time, f.days, f.train_number) == (
        "111",
        MONACO,
        NICE,
        "08:04",
        31,
        "86053",
    )
    assert not f.suggestion_sent
    assert repo.list_follows("111") == [f]


def test_follow_limit_10(repo):
    _user(repo)
    for i in range(10):
        repo.add_follow("111", MONACO, NICE, f"07:{i:02d}", 31, f"8600{i}")
    assert len(repo.list_follows("111")) == 10
    with pytest.raises(FollowLimitError):
        repo.add_follow("111", MONACO, NICE, "07:10", 31, "86010")
    assert len(repo.list_follows("111")) == 10

    # The limit is per user.
    _user(repo, "222")
    repo.add_follow("222", MONACO, NICE, "07:00", 31, "86000")
    assert len(repo.list_follows("222")) == 1


def test_duplicate_follow_raises(repo):
    _user(repo)
    _follow(repo, dep_time="08:04")
    with pytest.raises(DuplicateFollowError):
        repo.add_follow("111", MONACO, NICE, "08:04", 127, "99999")
    assert len(repo.list_follows("111")) == 1

    # Same route at another time, the reverse direction, or another user: all fine.
    repo.add_follow("111", MONACO, NICE, "08:34", 31, "86055")
    repo.add_follow("111", NICE, MONACO, "08:04", 31, "86054")
    _user(repo, "222")
    repo.add_follow("222", MONACO, NICE, "08:04", 31, "86053")
    assert len(repo.list_follows("111")) == 3


def test_list_and_all_follows(repo):
    _user(repo, "111")
    _user(repo, "222")
    a = _follow(repo, "111", "08:04")
    b = _follow(repo, "111", "09:04")
    c = _follow(repo, "222", "08:04")
    assert {f.id for f in repo.list_follows("111")} == {a.id, b.id}
    assert [f.id for f in repo.list_follows("222")] == [c.id]
    assert repo.list_follows("333") == []
    assert {f.id for f in repo.all_follows()} == {a.id, b.id, c.id}


def test_delete_follow_only_owner(repo):
    _user(repo)
    f = _follow(repo)
    assert repo.delete_follow("autre", f.id) is False
    assert [x.id for x in repo.list_follows("111")] == [f.id]

    assert repo.delete_follow("111", f.id) is True
    assert repo.list_follows("111") == []
    assert repo.delete_follow("111", f.id) is False


def test_delete_follow_cascades_daily_state(repo, db):
    _user(repo)
    f = _follow(repo)
    repo.set_found(f.id, D3, True)
    repo.set_last_delay(f.id, D3, 6)
    assert repo.delete_follow("111", f.id) is True
    assert db.execute("SELECT COUNT(*) FROM daily_state").fetchone()[0] == 0


def test_replace_dep_time_resets_suggestion_sent(repo):
    _user(repo)
    f = _follow(repo, dep_time="08:04", train_number="86053")
    repo.mark_suggestion_sent(f.id)
    assert repo.list_follows("111")[0].suggestion_sent

    assert repo.replace_dep_time("111", f.id, "08:15", "86055") is True
    [updated] = repo.list_follows("111")
    assert updated.id == f.id
    assert updated.dep_time == "08:15"
    assert updated.train_number == "86055"
    assert not updated.suggestion_sent


def test_replace_dep_time_only_owner(repo):
    _user(repo)
    f = _follow(repo, dep_time="08:04", train_number="86053")
    assert repo.replace_dep_time("autre", f.id, "08:15", "86055") is False
    [same] = repo.list_follows("111")
    assert (same.dep_time, same.train_number) == ("08:04", "86053")


def test_mark_suggestion_sent(repo):
    _user(repo)
    f = _follow(repo)
    other = _follow(repo, dep_time="09:04")
    repo.mark_suggestion_sent(f.id)
    by_id = {x.id: x for x in repo.all_follows()}
    assert by_id[f.id].suggestion_sent
    assert not by_id[other.id].suggestion_sent


# --- daily state -------------------------------------------------------------------------------


def test_get_daily_defaults_to_all_none(repo):
    _user(repo)
    f = _follow(repo)
    assert repo.get_daily(f.id, D3) == DailyState(
        found=None, last_delay=None, notified_status=None, notified_delay=None
    )


def test_found_true_is_sticky_for_the_day(repo):
    _user(repo)
    f = _follow(repo)
    repo.set_found(f.id, D3, False)
    assert repo.get_daily(f.id, D3).found is False
    repo.set_found(f.id, D3, True)
    assert repo.get_daily(f.id, D3).found is True
    repo.set_found(f.id, D3, False)
    assert repo.get_daily(f.id, D3).found is True

    # Another day is independent.
    assert repo.get_daily(f.id, D2).found is None
    repo.set_found(f.id, D2, False)
    assert repo.get_daily(f.id, D2).found is False
    assert repo.get_daily(f.id, D3).found is True


def test_set_last_delay_round_trips_via_get_daily(repo):
    _user(repo)
    f = _follow(repo)
    repo.set_last_delay(f.id, D3, 7)
    state = repo.get_daily(f.id, D3)
    assert state.last_delay == 7
    assert state.found is None
    repo.set_last_delay(f.id, D3, 12)
    assert repo.get_daily(f.id, D3).last_delay == 12
    assert repo.get_daily(f.id, D2).last_delay is None


def test_set_notified_round_trips_via_get_daily(repo):
    _user(repo)
    f = _follow(repo)
    repo.set_notified(f.id, D3, "delayed", 8)
    state = repo.get_daily(f.id, D3)
    assert (state.notified_status, state.notified_delay) == ("delayed", 8)
    repo.set_notified(f.id, D3, "cancelled", 0)
    state = repo.get_daily(f.id, D3)
    assert (state.notified_status, state.notified_delay) == ("cancelled", 0)


def test_daily_setters_do_not_clobber_each_other(repo):
    _user(repo)
    f = _follow(repo)
    repo.set_found(f.id, D3, True)
    repo.set_last_delay(f.id, D3, 9)
    repo.set_notified(f.id, D3, "delayed", 9)
    assert repo.get_daily(f.id, D3) == DailyState(
        found=True, last_delay=9, notified_status="delayed", notified_delay=9
    )


def test_missing_streak_counts_recent_consecutive(repo):
    _user(repo)
    a = _follow(repo, dep_time="08:04")
    b = _follow(repo, dep_time="09:04")
    c = _follow(repo, dep_time="10:04")

    # found 1, 0, 0 -> 2
    repo.set_found(a.id, D1, True)
    repo.set_found(a.id, D2, False)
    repo.set_found(a.id, D3, False)
    assert repo.missing_streak(a.id) == 2

    # found 0, 1 -> 0
    repo.set_found(b.id, D1, False)
    repo.set_found(b.id, D2, True)
    assert repo.missing_streak(b.id) == 0

    # no rows -> 0
    assert repo.missing_streak(c.id) == 0


def test_missing_streak_orders_by_service_date_not_insertion(repo):
    _user(repo)
    f = _follow(repo)
    repo.set_found(f.id, D3, False)
    repo.set_found(f.id, D1, False)
    repo.set_found(f.id, D2, True)
    # Chronologically: D1 missing, D2 found, D3 missing -> only D3 counts.
    assert repo.missing_streak(f.id) == 1


# --- quota -------------------------------------------------------------------------------------


def test_quota_counts_per_day_and_consumer(repo):
    repo.quota_incr(D3, "web")
    repo.quota_incr(D3, "web")
    repo.quota_incr(D3, "watcher")
    repo.quota_incr(D2, "web")
    assert repo.quota_count(D3, "web") == 2
    assert repo.quota_count(D3, "watcher") == 1
    assert repo.quota_count(D3) == 3
    assert repo.quota_count(D2) == 1
    assert repo.quota_count(D1) == 0
    assert repo.quota_count(D1, "web") == 0


def test_quota_month_sums_only_that_month(repo):
    repo.quota_incr(date(2026, 10, 31), "watcher")
    repo.quota_incr(date(2026, 11, 1), "web")
    repo.quota_incr(date(2026, 11, 1), "watcher")
    repo.quota_incr(date(2026, 11, 30), "web")
    repo.quota_incr(date(2026, 12, 1), "web")
    repo.quota_incr(date(2025, 11, 15), "web")
    assert repo.quota_month(2026, 11) == 3
    assert repo.quota_month(2026, 10) == 1
    assert repo.quota_month(2026, 12) == 1
    assert repo.quota_month(2026, 9) == 0


# --- cache -------------------------------------------------------------------------------------


def test_cache_put_get_round_trip(repo):
    at = datetime(2026, 11, 6, 8, 0, 12, tzinfo=PARIS)
    body = {"journeys": [{"status": "", "n": 1}], "msg": "Réutilisation d'un train"}
    assert repo.cache_get("k1") is None
    repo.cache_put("k1", body, at)
    got = repo.cache_get("k1")
    assert got is not None
    got_body, got_at = got
    assert got_body == body
    assert got_at == at
    assert got_at.tzinfo is not None


def test_cache_put_overwrites(repo):
    t1 = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)
    t2 = t1 + timedelta(minutes=2)
    repo.cache_put("k", {"v": 1}, t1)
    repo.cache_put("k", {"v": 2}, t2)
    assert repo.cache_get("k") == ({"v": 2}, t2)


def test_cache_purge_removes_entries_older_than_cutoff(repo):
    now = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)
    repo.cache_put("old", {"v": "old"}, now - timedelta(hours=25))
    repo.cache_put("recent", {"v": "recent"}, now - timedelta(hours=1))
    repo.cache_purge(now - timedelta(hours=24))
    assert repo.cache_get("old") is None
    assert repo.cache_get("recent") == ({"v": "recent"}, now - timedelta(hours=1))


# --- kv ----------------------------------------------------------------------------------------


def test_kv_get_set(repo):
    assert repo.kv_get("missing") is None
    repo.kv_set("a", "1")
    assert repo.kv_get("a") == "1"
    repo.kv_set("a", "2")
    assert repo.kv_get("a") == "2"


# --- controller rulings R4 (cache families) and R6 (missing_streak ignores NULL) ---------------


def test_cache_latest_returns_newest_entry_of_family(repo):
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)
    fam = f"journeys:{MONACO}:{NICE}"
    other = f"journeys:{NICE}:{MONACO}"
    # Inserted newest-first: the order must come from fetched_at, not insertion.
    repo.cache_put("k-0805", {"v": "08:05"}, t0 + timedelta(minutes=5), family=fam)
    repo.cache_put("k-0800", {"v": "08:00"}, t0, family=fam)
    repo.cache_put("k-other", {"v": "other"}, t0 + timedelta(minutes=9), family=other)
    repo.cache_put("k-nofam", {"v": "nofam"}, t0 + timedelta(minutes=10))

    assert repo.cache_latest(fam) == ({"v": "08:05"}, t0 + timedelta(minutes=5))
    assert repo.cache_latest(other) == ({"v": "other"}, t0 + timedelta(minutes=9))
    assert repo.cache_latest("journeys:none:none") is None
    # Entries are still reachable by their exact key.
    assert repo.cache_get("k-0800") == ({"v": "08:00"}, t0)
    assert repo.cache_get("k-nofam") == ({"v": "nofam"}, t0 + timedelta(minutes=10))


def test_cache_latest_follows_overwrites(repo):
    t0 = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)
    repo.cache_put("a", {"v": 1}, t0, family="fam")
    repo.cache_put("b", {"v": 2}, t0 + timedelta(minutes=1), family="fam")
    repo.cache_put("a", {"v": 3}, t0 + timedelta(minutes=2), family="fam")
    assert repo.cache_latest("fam") == ({"v": 3}, t0 + timedelta(minutes=2))


def test_missing_streak_ignores_null_rows(repo):
    _user(repo)
    a = _follow(repo, dep_time="08:04")
    b = _follow(repo, dep_time="09:04")

    # found 0, NULL, 0 -> 2 (the NULL row is created by set_last_delay alone)
    repo.set_found(a.id, D1, False)
    repo.set_last_delay(a.id, D2, 5)
    repo.set_found(a.id, D3, False)
    assert repo.get_daily(a.id, D2).found is None
    assert repo.missing_streak(a.id) == 2

    # found 0, 0, NULL (most recent) -> 2
    repo.set_found(b.id, D1, False)
    repo.set_found(b.id, D2, False)
    repo.set_notified(b.id, D3, "delayed", 6)
    assert repo.missing_streak(b.id) == 2
