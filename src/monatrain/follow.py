"""Shared "follow a train" services, used by both the web site and the Discord bot."""

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from monatrain.models import TrainInfo
from monatrain.repo import DuplicateFollowError, Follow, FollowLimitError, Repo
from monatrain.sncf.client import SncfError
from monatrain.stations import get_station
from monatrain.timeutil import PARIS, next_service_date, runs_on

log = logging.getLogger(__name__)

WINDOW = timedelta(minutes=15)
LIVE_BEFORE = timedelta(minutes=30)
LIVE_AFTER = timedelta(hours=3)


class FollowError(ValueError):
    """A user-facing (French) reason why a train cannot be followed."""


@dataclass
class TodayItem:
    follow: Follow
    state: Literal["upcoming", "live", "past", "missing"]
    train: TrainInfo | None
    stale: bool


def _at(on: date, hhmm: str) -> datetime:
    return datetime.combine(on, time.fromisoformat(hhmm), tzinfo=PARIS)


def _closest(trains: list[TrainInfo], target: datetime) -> TrainInfo | None:
    """The train scheduled within 15 min of target (inclusive); ties go to the earliest scheduled."""
    candidates = [t for t in trains if abs(t.base_departure - target) <= WINDOW]
    return min(candidates, key=lambda t: (abs(t.base_departure - target), t.base_departure), default=None)


async def list_trains_around(
    source, from_id: str, to_id: str, hhmm: str, on: date, count: int = 8
) -> list[TrainInfo]:
    start = _at(on, hhmm) - WINDOW
    fetched = await source.journeys(from_id, to_id, start, count)
    # A stale answer may hold trains of an earlier query: keep only those from the window onwards.
    return [t for t in fetched.items if t.base_departure >= start]


async def resolve_train(source, from_id: str, to_id: str, hhmm: str, days: int, now: datetime) -> TrainInfo:
    if from_id == to_id or get_station(from_id) is None or get_station(to_id) is None:
        raise FollowError("Les gares de départ et d'arrivée doivent être différentes.")

    service_date = next_service_date(days, hhmm, now)
    trains = await list_trains_around(source, from_id, to_id, hhmm, service_date)
    found = _closest(trains, _at(service_date, hhmm))
    if found is None:
        raise FollowError(f"Aucun train direct trouvé autour de {hhmm}.")
    return found


async def add_follow(
    repo: Repo, source, user_id: str, from_id: str, to_id: str, hhmm: str, days: int, now: datetime
) -> Follow:
    found = await resolve_train(source, from_id, to_id, hhmm, days, now)
    dep_time = found.base_departure.astimezone(PARIS).strftime("%H:%M")
    try:
        return repo.add_follow(user_id, from_id, to_id, dep_time, days, found.train_number)
    except FollowLimitError as exc:
        raise FollowError("Tu suis déjà 10 trains.") from exc
    except DuplicateFollowError as exc:
        raise FollowError("Tu suis déjà ce train.") from exc


async def today_statuses(repo: Repo, source, user_id: str, now: datetime) -> list[TodayItem]:
    now = now.astimezone(PARIS)
    today = now.date()
    follows = sorted(
        (f for f in repo.list_follows(user_id) if runs_on(f.days, today)),
        key=lambda f: (f.dep_time, f.id),
    )

    live_by_route: dict[tuple[str, str], list[Follow]] = {}
    states: dict[int, str] = {}
    for follow in follows:
        scheduled = _at(today, follow.dep_time)
        if scheduled < now - LIVE_BEFORE:
            states[follow.id] = "past"
        elif scheduled > now + LIVE_AFTER:
            states[follow.id] = "upcoming"
        else:
            states[follow.id] = "live"
            live_by_route.setdefault((follow.from_id, follow.to_id), []).append(follow)

    # One journeys call per route, starting at its earliest live train (follows are sorted by time).
    answers: dict[tuple[str, str], tuple[list[TrainInfo], bool] | None] = {}
    for (from_id, to_id), route_follows in live_by_route.items():
        try:
            fetched = await source.journeys(from_id, to_id, _at(today, route_follows[0].dep_time))
        except SncfError as exc:
            log.warning("Statut du jour indisponible pour %s -> %s : %s", from_id, to_id, exc)
            answers[(from_id, to_id)] = None
        else:
            answers[(from_id, to_id)] = (fetched.items, fetched.stale)

    items: list[TodayItem] = []
    for follow in follows:
        state = states[follow.id]
        if state != "live":
            items.append(TodayItem(follow, state, None, False))
            continue
        answer = answers[(follow.from_id, follow.to_id)]
        if answer is None:  # the route could not be queried: degrade to "live", unknown
            items.append(TodayItem(follow, "live", None, True))
            continue
        trains, stale = answer
        scheduled = _at(today, follow.dep_time)
        match = next((t for t in trains if t.base_departure == scheduled), None)
        if match is None and repo.get_daily(follow.id, today).found is False:
            state = "missing"
        items.append(TodayItem(follow, state, match, stale))
    return items


async def nearest_alternative(source, follow: Follow, on: date) -> TrainInfo | None:
    current = _at(on, follow.dep_time)
    trains = await list_trains_around(source, follow.from_id, follow.to_id, follow.dep_time, on)
    return _closest([t for t in trains if t.base_departure != current], current)
