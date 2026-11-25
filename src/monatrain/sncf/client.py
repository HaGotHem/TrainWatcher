"""Async SNCF (Navitia) client with a SQLite-backed cache and quota accounting."""

import logging
from collections.abc import Callable
from datetime import datetime, time, timedelta
from urllib.parse import urlencode

import httpx

from monatrain.budget import Consumer, QuotaBudget
from monatrain.models import Fetched, Status, TrainInfo
from monatrain.repo import Repo
from monatrain.sncf.parse import parse_departures, parse_journeys
from monatrain.timeutil import PARIS, format_navitia_dt, now_paris

log = logging.getLogger(__name__)

BASE_URL = "https://api.sncf.com/v1/coverage/sncf"
_TIMEOUT_S = 10.0
_NO_SOLUTION = "no_solution"
_BASE_TTL = timedelta(seconds=300)  # the theoretical timetable hardly moves: cross-check at most every 5 min


class SncfError(Exception):
    pass


class SncfAuthError(SncfError):
    pass


class SncfQuotaError(SncfError):
    pass


class SncfUnavailable(SncfError):
    pass


class SncfClient:
    def __init__(
        self,
        token: str,
        repo: Repo,
        budget: QuotaBudget,
        consumer: Consumer,
        *,
        http: httpx.AsyncClient | None = None,
        base_url: str = BASE_URL,
        fresh_ttl: int = 60,
        clock: Callable[[], datetime] = now_paris,
    ) -> None:
        self._token = token
        self._repo = repo
        self._budget = budget
        self._consumer = consumer
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient(timeout=_TIMEOUT_S)
        self._base_url = base_url
        self._fresh_ttl = timedelta(seconds=fresh_ttl)
        self._clock = clock

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def journeys(
        self, from_id: str, to_id: str, when: datetime, count: int = 8
    ) -> Fetched[list[TrainInfo]]:
        when = when.astimezone(PARIS).replace(second=0, microsecond=0)
        params = {
            "from": from_id,
            "to": to_id,
            "datetime": format_navitia_dt(when),
            "datetime_represents": "departure",
            "data_freshness": "realtime",
            "max_nb_transfers": "0",
            "direct_path": "none",
            "count": str(count),
        }
        family = f"journeys:{from_id}:{to_id}"
        body, fetched_at, stale = await self._get("/journeys", params, family=family)
        trains = parse_journeys(body, from_id, to_id)
        if not stale:
            trains = await self._with_deleted_trains(
                trains, "/journeys", params, family, lambda b: parse_journeys(b, from_id, to_id)
            )
        return Fetched(trains, fetched_at, stale)

    async def departures(self, stop_id: str, count: int = 10) -> Fetched[list[TrainInfo]]:
        params = {"data_freshness": "realtime", "count": str(count)}
        path, family = f"/stop_areas/{stop_id}/departures", f"departures:{stop_id}"
        body, fetched_at, stale = await self._get(path, params, family=family)
        trains = parse_departures(body, stop_id)
        if not stale:
            trains = await self._with_deleted_trains(
                trains, path, params, family, lambda b: parse_departures(b, stop_id)
            )
        return Fetched(trains, fetched_at, stale)

    async def _with_deleted_trains(
        self,
        trains: list[TrainInfo],
        path: str,
        params: dict[str, str],
        family: str,
        parse: Callable[[dict], list[TrainInfo]],
    ) -> list[TrainInfo]:
        """Add the cancelled trains the realtime graph no longer returns, read from the base schedule.

        A trip deleted for the day disappears from `data_freshness=realtime` answers but is still
        in `base_schedule` ones, flagged by its disruption. Any failure leaves `trains` untouched.
        """
        try:
            body, _, _ = await self._get(
                path,
                {**params, "data_freshness": "base_schedule"},
                family=f"base:{family}",
                ttl=_BASE_TTL,
                serve_stale=False,
            )
            base = parse(body)
        except SncfError as exc:
            log.warning("Recoupement base_schedule impossible : %s", exc)
            return trains
        known = {(t.train_number, t.base_departure) for t in trains}
        deleted = [
            t for t in base if t.status is Status.CANCELLED and (t.train_number, t.base_departure) not in known
        ]
        return sorted(trains + deleted, key=lambda t: t.base_departure)

    async def _get(
        self,
        path: str,
        params: dict[str, str],
        *,
        family: str,
        ttl: timedelta | None = None,
        serve_stale: bool = True,
    ) -> tuple[dict, datetime, bool]:
        key = f"{path}?{urlencode(sorted(params.items()))}"
        now = self._clock()

        cached = self._repo.cache_get(key)
        if cached is not None and now - cached[1] < (ttl or self._fresh_ttl):
            return cached[0], cached[1], False

        stale_or_raise = self._stale_or_raise if serve_stale else _raise
        if not self._budget.allow(self._consumer, now):
            return stale_or_raise(key, family, SncfQuotaError("Quota SNCF épuisé ou bloqué"))

        # Every request actually sent counts against the quota, whatever its outcome.
        self._budget.record(self._consumer, now)
        try:
            response = await self._http.get(
                self._base_url + path, params=params, auth=(self._token, ""), timeout=_TIMEOUT_S
            )
        except httpx.RequestError as exc:
            return stale_or_raise(
                key, family, SncfUnavailable(f"API SNCF injoignable ({type(exc).__name__})")
            )

        status = response.status_code
        if status == 401:
            raise SncfAuthError("Authentification SNCF refusée (HTTP 401)")
        if status == 429:
            self._budget.block_until(_next_paris_midnight(now))
            return stale_or_raise(key, family, SncfQuotaError("Quota SNCF dépassé (HTTP 429)"))
        if status >= 500:
            return stale_or_raise(key, family, SncfUnavailable(f"API SNCF indisponible (HTTP {status})"))

        try:
            body = response.json()
        except ValueError:  # includes JSONDecodeError and UnicodeDecodeError
            body = None

        if status >= 400:
            error_id = _navitia_error_id(body)
            if status == 404 and error_id == _NO_SOLUTION:
                # No train at all (night, strike): a valid, empty answer; cached like a 200.
                self._repo.cache_put(key, body, now, family=family)
                return body, now, False
            raise SncfError(f"Requête SNCF refusée (HTTP {status}, {error_id or 'erreur inconnue'})")

        if not isinstance(body, dict):
            return stale_or_raise(key, family, SncfUnavailable("Réponse SNCF invalide"))

        self._repo.cache_put(key, body, now, family=family)
        return body, now, False

    def _stale_or_raise(self, key: str, family: str, error: SncfError) -> tuple[dict, datetime, bool]:
        """Serve the exact cached entry, else the newest of its family, else raise `error`."""
        cached = self._repo.cache_get(key) or self._repo.cache_latest(family)
        if cached is None:
            raise error
        return cached[0], cached[1], True


def _raise(key: str, family: str, error: SncfError) -> tuple[dict, datetime, bool]:
    raise error


def _navitia_error_id(body: object) -> str | None:
    error = body.get("error") if isinstance(body, dict) else None
    error_id = error.get("id") if isinstance(error, dict) else None
    return error_id if isinstance(error_id, str) else None


def _next_paris_midnight(now: datetime) -> datetime:
    tomorrow = now.astimezone(PARIS).date() + timedelta(days=1)
    return datetime.combine(tomorrow, time.min, tzinfo=PARIS)
