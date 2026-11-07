"""Daily and monthly quota budget for the SNCF API."""

from datetime import datetime
from typing import Literal

from monatrain.repo import Repo
from monatrain.timeutil import PARIS

Consumer = Literal["web", "watcher"]

BLOCKED_UNTIL_KEY = "quota_blocked_until"


class QuotaBudget:
    def __init__(
        self,
        repo: Repo,
        *,
        daily_limit: int = 5000,
        monthly_limit: int = 150000,
        web_limit: int = 3000,
    ) -> None:
        self._repo = repo
        self._daily_limit = daily_limit
        self._monthly_limit = monthly_limit
        self._web_limit = web_limit

    def allow(self, consumer: Consumer, now: datetime) -> bool:
        local = now.astimezone(PARIS)
        blocked_until = self._repo.kv_get(BLOCKED_UNTIL_KEY)
        if blocked_until is not None and local < datetime.fromisoformat(blocked_until):
            return False

        day = local.date()
        if self._repo.quota_count(day) >= self._daily_limit:
            return False
        if self._repo.quota_month(day.year, day.month) >= self._monthly_limit:
            return False
        return consumer != "web" or self._repo.quota_count(day, "web") < self._web_limit

    def record(self, consumer: Consumer, now: datetime) -> None:
        self._repo.quota_incr(now.astimezone(PARIS).date(), consumer)

    def block_until(self, dt: datetime) -> None:
        self._repo.kv_set(BLOCKED_UNTIL_KEY, dt.astimezone(PARIS).isoformat())
