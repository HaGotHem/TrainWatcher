"""Task 3: QuotaBudget (5 000/day total, 150 000/month, web capped at 3 000/day, 429 block)."""

import inspect
from datetime import UTC, date, datetime, timedelta

from monatrain.budget import QuotaBudget
from monatrain.timeutil import PARIS

NEXT_MIDNIGHT = datetime(2026, 11, 7, 0, 0, tzinfo=PARIS)


def test_default_limits():
    params = inspect.signature(QuotaBudget).parameters
    assert params["daily_limit"].default == 5000
    assert params["monthly_limit"].default == 150000
    assert params["web_limit"].default == 3000


def test_fresh_budget_allows_both(repo, fixed_now):
    b = QuotaBudget(repo)
    assert b.allow("web", fixed_now)
    assert b.allow("watcher", fixed_now)


def test_record_increments_repo_counter_per_consumer(repo, fixed_now):
    b = QuotaBudget(repo)
    b.record("web", fixed_now)
    b.record("web", fixed_now)
    b.record("watcher", fixed_now)
    day = fixed_now.date()
    assert repo.quota_count(day, "web") == 2
    assert repo.quota_count(day, "watcher") == 1
    assert repo.quota_count(day) == 3


def test_record_uses_the_paris_day(repo):
    b = QuotaBudget(repo)
    # 23:30 UTC on the 6th is 00:30 on the 7th in Paris (UTC+1 in November).
    b.record("watcher", datetime(2026, 11, 6, 23, 30, tzinfo=UTC))
    assert repo.quota_count(date(2026, 11, 7)) == 1
    assert repo.quota_count(date(2026, 11, 6)) == 0


def test_budget_web_cut_at_3000_watcher_continues(repo, fixed_now):
    b = QuotaBudget(repo)
    for _ in range(2999):
        b.record("web", fixed_now)
    assert b.allow("web", fixed_now)
    b.record("web", fixed_now)
    assert not b.allow("web", fixed_now) and b.allow("watcher", fixed_now)


def test_web_limit_counts_only_web_requests(repo, fixed_now):
    b = QuotaBudget(repo)
    for _ in range(2999):
        b.record("web", fixed_now)
    for _ in range(1000):
        b.record("watcher", fixed_now)
    # web: 2 999 < 3 000, total: 3 999 < 5 000
    assert b.allow("web", fixed_now)


def test_budget_daily_total_5000_blocks_all(repo, fixed_now):
    b = QuotaBudget(repo)
    for _ in range(2000):
        b.record("web", fixed_now)
    for _ in range(2999):
        b.record("watcher", fixed_now)
    assert b.allow("watcher", fixed_now)
    assert b.allow("web", fixed_now)
    b.record("watcher", fixed_now)
    assert repo.quota_count(fixed_now.date()) == 5000
    assert not b.allow("watcher", fixed_now)
    assert not b.allow("web", fixed_now)


def test_daily_counters_reset_at_paris_midnight(repo, fixed_now):
    b = QuotaBudget(repo)
    for _ in range(5000):
        b.record("web", fixed_now)
    late = datetime(2026, 11, 6, 23, 59, 59, tzinfo=PARIS)
    assert not b.allow("web", late)
    assert not b.allow("watcher", late)
    assert b.allow("web", NEXT_MIDNIGHT)
    assert b.allow("watcher", NEXT_MIDNIGHT)
    # Same instant expressed in UTC.
    assert b.allow("watcher", NEXT_MIDNIGHT.astimezone(UTC))


def test_monthly_limit_blocks(repo):
    b = QuotaBudget(repo, monthly_limit=30)
    for day in (2, 3, 4):
        for _ in range(10):
            b.record("watcher", datetime(2026, 11, day, 12, 0, tzinfo=PARIS))
    now = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)
    assert repo.quota_count(now.date()) == 0
    assert not b.allow("watcher", now)
    assert not b.allow("web", now)
    # A new month starts from zero.
    assert b.allow("watcher", datetime(2026, 12, 1, 0, 0, tzinfo=PARIS))


def test_monthly_limit_ignores_previous_month(repo):
    b = QuotaBudget(repo, monthly_limit=30)
    for _ in range(30):
        b.record("watcher", datetime(2026, 10, 31, 12, 0, tzinfo=PARIS))
    assert b.allow("watcher", datetime(2026, 11, 1, 0, 0, tzinfo=PARIS))


def test_custom_daily_and_web_limits(repo, fixed_now):
    b = QuotaBudget(repo, daily_limit=10, web_limit=4)
    for _ in range(4):
        b.record("web", fixed_now)
    assert not b.allow("web", fixed_now)
    assert b.allow("watcher", fixed_now)
    for _ in range(6):
        b.record("watcher", fixed_now)
    assert not b.allow("watcher", fixed_now)


def test_budget_block_until_after_429(repo, fixed_now):
    b = QuotaBudget(repo)
    b.block_until(NEXT_MIDNIGHT)
    assert repo.kv_get("quota_blocked_until") is not None
    assert not b.allow("watcher", fixed_now)
    assert not b.allow("web", fixed_now)
    assert not b.allow("watcher", NEXT_MIDNIGHT - timedelta(seconds=1))
    assert b.allow("watcher", NEXT_MIDNIGHT)
    assert b.allow("web", NEXT_MIDNIGHT + timedelta(minutes=5))


def test_block_is_persisted_for_other_instances(repo, fixed_now):
    QuotaBudget(repo).block_until(NEXT_MIDNIGHT)
    other = QuotaBudget(repo)
    assert not other.allow("watcher", fixed_now)
    assert other.allow("watcher", NEXT_MIDNIGHT)
