"""Periodic surveillance of followed trains and DM dispatch."""

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
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
_SUGGESTION_RETRY = timedelta(minutes=30)

type GroupKey = tuple[str, str, date, str]


class Notifier(Protocol):
    async def send(
        self, user_id: str, text: str, *, suggestion: tuple[int, TrainInfo] | None = None
    ) -> bool:
        """Send a DM. Return False if the DM is refused."""
        ...


def _base(d: date, dep_time: str) -> datetime:
    """Scheduled departure on service day d, as an aware UTC datetime (safe to compare across DST changes)."""
    return datetime.combine(d, time.fromisoformat(dep_time), tzinfo=PARIS).astimezone(UTC)


class _StaleAnswer(Exception):
    """The source answered from stale cache: not a usable observation."""


class _StaleProbe:
    """Wraps a source and raises _StaleAnswer when it answers from stale data."""

    def __init__(self, source) -> None:
        self._source = source

    async def journeys(self, *args, **kwargs):
        fetched = await self._source.journeys(*args, **kwargs)
        if fetched.stale:
            raise _StaleAnswer
        return fetched


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
        # (follow id, dep_time, service date) with at least one successful, fresh journeys call.
        # dep_time is part of every key so that a replaced follow never inherits the old train's state.
        self._polled_ok: set[tuple[int, str, date]] = set()
        # (follow id, dep_time) -> earliest time of the next suggestion attempt, after a failed one.
        self._suggest_retry_at: dict[tuple[int, str], datetime] = {}

    async def tick(self, now: datetime) -> None:
        today = now.astimezone(PARIS).date()
        now = now.astimezone(UTC)
        days = (today - timedelta(days=1), today, today + timedelta(days=1))
        follows = self._repo.all_follows()
        self._forget_old(days[0])

        groups: dict[GroupKey, list[Follow]] = {}
        for follow in follows:
            try:
                for d in days:
                    if not runs_on(follow.days, d):
                        continue
                    base = _base(d, follow.dep_time)
                    last_delay = max(self._repo.get_daily(follow.id, d).last_delay or 0, 0)
                    if base - self._window_before <= now <= base + timedelta(minutes=last_delay):
                        groups.setdefault((follow.from_id, follow.to_id, d, follow.dep_time), []).append(follow)
            except Exception:
                log.exception("Suivi %s ignoré", follow.id)

        for key, subscribers in groups.items():
            last = self._last_poll.get(key)
            if last is not None and now - last < self._poll_interval:
                continue
            self._last_poll[key] = now
            try:
                await self._poll_group(key, subscribers)
            except Exception:
                log.exception("Interrogation du groupe %s en échec", key)

        for follow in follows:
            try:
                await self._close_windows(follow, days, now)
            except Exception:
                log.exception("Fin de fenêtre du suivi %s en échec", follow.id)

    async def run_forever(self, interval_s: int = 60) -> None:
        last_purge: date | None = None
        while True:
            now = now_paris()
            try:
                await self.tick(now)
            except Exception:
                log.exception("Échec d'un cycle du watcher")
            try:
                if last_purge != now.date():
                    self._repo.cache_purge(now - timedelta(hours=24))
                    last_purge = now.date()
            except Exception:
                log.exception("Échec de la purge du cache")
            await asyncio.sleep(interval_s)

    def _forget_old(self, oldest_day: date) -> None:
        self._last_poll = {k: v for k, v in self._last_poll.items() if k[2] >= oldest_day}
        self._polled_ok = {k for k in self._polled_ok if k[2] >= oldest_day}

    async def _poll_group(self, key: GroupKey, subscribers: list[Follow]) -> None:
        from_id, to_id, d, dep_time = key
        base = _base(d, dep_time)
        try:
            when = (base - timedelta(minutes=1)).astimezone(PARIS)
            fetched = await self._source.journeys(from_id, to_id, when, count=4)
        except SncfError as exc:
            log.warning("Interrogation SNCF impossible pour %s -> %s : %s", from_id, to_id, exc)
            return
        if fetched.stale:
            log.warning("Réponse SNCF périmée pour %s -> %s : ignorée", from_id, to_id)
            return

        for follow in subscribers:
            self._polled_ok.add((follow.id, follow.dep_time, d))
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
            self._repo.set_dm_ok(follow.user_id, True)
        else:
            self._repo.set_dm_ok(follow.user_id, False)

    async def _close_windows(self, follow: Follow, days: tuple[date, ...], now: datetime) -> None:
        missing_day: date | None = None
        for d in days:  # oldest first: the latest missing day ends up in missing_day
            if not runs_on(follow.days, d) or _base(d, follow.dep_time) >= now:
                continue
            if self._repo.get_daily(follow.id, d).found is None and (follow.id, follow.dep_time, d) in self._polled_ok:
                self._repo.set_found(follow.id, d, False)
            if self._repo.get_daily(follow.id, d).found is False:
                missing_day = d
        if missing_day is None or follow.suggestion_sent:
            return
        if self._repo.missing_streak(follow.id) < _MISSING_STREAK_FOR_SUGGESTION:
            return
        retry_at = self._suggest_retry_at.get((follow.id, follow.dep_time))
        if retry_at is None or now >= retry_at:
            await self._suggest(follow, missing_day, now)

    async def _suggest(self, follow: Follow, d: date, now: datetime) -> None:
        try:
            alt = await nearest_alternative(_StaleProbe(self._source), follow, d)
        except (SncfError, _StaleAnswer) as exc:
            log.warning("Train de remplacement indisponible pour le suivi %s : %r", follow.id, exc)
            self._suggest_retry_at[(follow.id, follow.dep_time)] = now + _SUGGESTION_RETRY
            return
        except Exception:
            log.exception("Échec de la recherche de remplacement pour le suivi %s", follow.id)
            self._repo.mark_suggestion_sent(follow.id)
            return
        try:
            suggestion = (follow.id, alt) if alt is not None else None
            sent = await self._notifier.send(follow.user_id, format_missing(follow, alt), suggestion=suggestion)
            self._repo.set_dm_ok(follow.user_id, sent)
        except Exception:
            log.exception("Échec de l'envoi de la suggestion pour le suivi %s", follow.id)
        self._repo.mark_suggestion_sent(follow.id)
