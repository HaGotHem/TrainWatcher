"""Periodic surveillance of followed trains and DM dispatch."""

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Protocol

from monatrain.alerts import decide
from monatrain.follow import nearest_alternative
from monatrain.messages import format_alert, format_missing
from monatrain.models import TrainInfo
from monatrain.repo import Follow, Repo
from monatrain.sncf.client import SncfError
from monatrain.timeutil import PARIS, now_paris, runs_on

log = logging.getLogger(__name__)

_MISSING_STREAK_FOR_SUGGESTION = 2

type GroupKey = tuple[str, str, date, str]


class Notifier(Protocol):
    async def send(
        self, user_id: str, text: str, *, suggestion: tuple[int, TrainInfo] | None = None
    ) -> bool:
        """Send a DM. Return False if the DM is refused."""
        ...


def _base(d: date, dep_time: str) -> datetime:
    return datetime.combine(d, time.fromisoformat(dep_time), tzinfo=PARIS)


class Watcher:
    def __init__(
        self,
        repo: Repo,
        source,
        notifier: Notifier,
        *,
        poll_interval_s: int = 120,
        window_before_min: int = 60,
    ) -> None:
        self._repo = repo
        self._source = source
        self._notifier = notifier
        self._poll_interval = timedelta(seconds=poll_interval_s)
        self._window_before = timedelta(minutes=window_before_min)
        self._last_poll: dict[GroupKey, datetime] = {}
        # (follow id, service date) with at least one successful journeys call.
        self._polled_ok: set[tuple[int, date]] = set()

    async def tick(self, now: datetime) -> None:
        now = now.astimezone(PARIS)
        days = (now.date(), now.date() - timedelta(days=1))
        follows = self._repo.all_follows()
        self._forget_old(days[1])

        groups: dict[GroupKey, list[Follow]] = {}
        for follow in follows:
            for d in days:
                if not runs_on(follow.days, d):
                    continue
                base = _base(d, follow.dep_time)
                last_delay = max(self._repo.get_daily(follow.id, d).last_delay or 0, 0)
                if base - self._window_before <= now <= base + timedelta(minutes=last_delay):
                    groups.setdefault((follow.from_id, follow.to_id, d, follow.dep_time), []).append(follow)

        for key, subscribers in groups.items():
            last = self._last_poll.get(key)
            if last is not None and now - last < self._poll_interval:
                continue
            self._last_poll[key] = now
            await self._poll_group(key, subscribers)

        await self._close_windows(follows, days, now)

    async def run_forever(self, interval_s: int = 60) -> None:
        last_purge: date | None = None
        while True:
            try:
                now = now_paris()
                await self.tick(now)
                if last_purge != now.date():
                    self._repo.cache_purge(now - timedelta(hours=24))
                    last_purge = now.date()
            except Exception:
                log.exception("Échec d'un cycle du watcher")
            await asyncio.sleep(interval_s)

    def _forget_old(self, oldest_day: date) -> None:
        self._last_poll = {k: v for k, v in self._last_poll.items() if k[2] >= oldest_day}
        self._polled_ok = {k for k in self._polled_ok if k[1] >= oldest_day}

    async def _poll_group(self, key: GroupKey, subscribers: list[Follow]) -> None:
        from_id, to_id, d, dep_time = key
        base = _base(d, dep_time)
        try:
            fetched = await self._source.journeys(from_id, to_id, base - timedelta(minutes=1), count=4)
        except SncfError as exc:
            log.warning("Interrogation SNCF impossible pour %s -> %s : %s", from_id, to_id, exc)
            return

        for follow in subscribers:
            self._polled_ok.add((follow.id, d))
        train = next((t for t in fetched.items if t.base_departure == base), None)
        if train is None:
            return

        for follow in subscribers:
            try:
                await self._handle_found(follow, d, train)
            except Exception:
                log.exception("Échec du traitement du suivi %s", follow.id)

    async def _handle_found(self, follow: Follow, d: date, train: TrainInfo) -> None:
        self._repo.set_found(follow.id, d, True)
        self._repo.set_last_delay(follow.id, d, train.delay_min)
        kind = decide(self._repo.get_daily(follow.id, d), train)
        if kind is None:
            return
        if await self._notifier.send(follow.user_id, format_alert(kind, train)):
            self._repo.set_notified(follow.id, d, kind.value, train.delay_min)
        else:
            self._repo.set_dm_ok(follow.user_id, False)

    async def _close_windows(self, follows: list[Follow], days: tuple[date, date], now: datetime) -> None:
        suggested: set[int] = set()
        for follow in follows:
            for d in days:
                if not runs_on(follow.days, d) or _base(d, follow.dep_time) >= now:
                    continue
                if self._repo.get_daily(follow.id, d).found is None and (follow.id, d) in self._polled_ok:
                    self._repo.set_found(follow.id, d, False)
                if follow.id in suggested or follow.suggestion_sent:
                    continue
                if self._repo.get_daily(follow.id, d).found is not False:
                    continue
                if self._repo.missing_streak(follow.id) < _MISSING_STREAK_FOR_SUGGESTION:
                    continue
                try:
                    await self._suggest(follow, d)
                except SncfError as exc:
                    log.warning("Train de remplacement introuvable pour le suivi %s : %s", follow.id, exc)
                    continue
                except Exception:
                    log.exception("Échec de la suggestion pour le suivi %s", follow.id)
                suggested.add(follow.id)

    async def _suggest(self, follow: Follow, d: date) -> None:
        alt = await nearest_alternative(self._source, follow, d)
        suggestion = (follow.id, alt) if alt is not None else None
        if not await self._notifier.send(follow.user_id, format_missing(follow, alt), suggestion=suggestion):
            self._repo.set_dm_ok(follow.user_id, False)
        self._repo.mark_suggestion_sent(follow.id)
