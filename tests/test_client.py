"""Task 3: async SNCF client with SQLite-backed cache, quota budget and error mapping.

HTTP goes through `httpx.MockTransport` (see `make_client` in conftest.py); response bodies are
the real fixtures captured in Task 2. Time comes from the injected `clock` fixture.
"""

import base64
import copy
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from monatrain.models import Fetched, Status
from monatrain.sncf.client import (
    BASE_URL,
    SncfAuthError,
    SncfError,
    SncfQuotaError,
    SncfUnavailable,
)
from monatrain.sncf.parse import parse_departures, parse_journeys
from monatrain.timeutil import PARIS

FIXTURES = Path(__file__).parent / "fixtures"
MONACO = "stop_area:SNCF:87756403"
NICE = "stop_area:SNCF:87756056"
NEXT_MIDNIGHT = datetime(2026, 11, 7, 0, 0, tzinfo=PARIS)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


JOURNEYS = load("journeys_monaco_nice.json")
DEPARTURES_MONACO = load("departures_monaco.json")


class FakeServer:
    """MockTransport handler: records every request and answers with the current `reply`.

    `reply` is either an httpx.Response factory `(request) -> Response`, or an exception
    instance to raise (e.g. a timeout).

    Ruling R25: the client also sends a `data_freshness=base_schedule` cross-check request.
    `requests` records only the other (realtime) requests, `base_requests` the base_schedule
    ones; both get the same `reply` (the fixtures here hold no cancelled train, so the
    cross-check adds nothing to the results).
    """

    def __init__(self, body: dict | None = None, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.base_requests: list[httpx.Request] = []
        self.set(body if body is not None else JOURNEYS, status)

    def set(self, body: dict, status: int = 200) -> None:
        self.reply = lambda request: httpx.Response(status, json=body)

    def set_raw(self, content: bytes, status: int = 200) -> None:
        self.reply = lambda request: httpx.Response(
            status, content=content, headers={"content-type": "application/json"}
        )

    def set_status(self, status: int) -> None:
        self.reply = lambda request: httpx.Response(status, json={"message": f"HTTP {status}"})

    def set_timeout(self) -> None:
        def raise_timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        self.reply = raise_timeout

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.params.get("data_freshness") == "base_schedule":
            self.base_requests.append(request)
        else:
            self.requests.append(request)
        return self.reply(request)


def expected_journeys() -> list:
    return parse_journeys(JOURNEYS, MONACO, NICE)


# --- module surface ----------------------------------------------------------------------------


def test_base_url():
    assert BASE_URL == "https://api.sncf.com/v1/coverage/sncf"


def test_exception_hierarchy():
    for exc in (SncfAuthError, SncfQuotaError, SncfUnavailable):
        assert issubclass(exc, SncfError)
    assert issubclass(SncfError, Exception)


# --- request shape -----------------------------------------------------------------------------


async def test_basic_auth_and_params(make_client, sncf_token, fixed_now):
    server = FakeServer(JOURNEYS)
    client = make_client(server)
    result = await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS))

    assert len(server.requests) == 1
    req = server.requests[0]
    assert req.method == "GET"
    assert req.url.host == "api.sncf.com"
    assert req.url.path == "/v1/coverage/sncf/journeys"

    expected_auth = "Basic " + base64.b64encode(f"{sncf_token}:".encode()).decode()
    assert req.headers["Authorization"] == expected_auth

    params = req.url.params
    assert params["from"] == MONACO
    assert params["to"] == NICE
    assert params["datetime"] == "20261106T080000"
    assert params["datetime_represents"] == "departure"
    assert params["data_freshness"] == "realtime"
    assert params["max_nb_transfers"] == "0"
    assert params["direct_path"] == "none"
    assert params["count"] == "8"

    assert isinstance(result, Fetched)
    assert result.items == expected_journeys()
    assert result.stale is False
    assert result.fetched_at == fixed_now


async def test_journeys_datetime_param_is_paris_local_time(make_client):
    server = FakeServer(JOURNEYS)
    client = make_client(server)
    # 07:00 UTC == 08:00 Paris in November.
    await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 7, 0, tzinfo=UTC))
    assert server.requests[0].url.params["datetime"] == "20261106T080000"


async def test_journeys_count_param(make_client):
    server = FakeServer(JOURNEYS)
    client = make_client(server)
    await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS), count=3)
    assert server.requests[0].url.params["count"] == "3"


async def test_departures_request_and_result(make_client, sncf_token, fixed_now):
    server = FakeServer(DEPARTURES_MONACO)
    client = make_client(server)
    result = await client.departures(MONACO)

    assert len(server.requests) == 1
    req = server.requests[0]
    assert req.url.host == "api.sncf.com"
    assert req.url.path == f"/v1/coverage/sncf/stop_areas/{MONACO}/departures"
    assert req.headers["Authorization"] == "Basic " + base64.b64encode(f"{sncf_token}:".encode()).decode()
    assert req.url.params["data_freshness"] == "realtime"
    assert req.url.params["count"] == "10"

    assert result.items == parse_departures(DEPARTURES_MONACO, MONACO)
    assert result.stale is False
    assert result.fetched_at == fixed_now


async def test_departures_count_param(make_client):
    server = FakeServer(DEPARTURES_MONACO)
    client = make_client(server)
    await client.departures(MONACO, count=4)
    assert server.requests[0].url.params["count"] == "4"


# --- cache -------------------------------------------------------------------------------------


async def test_cache_hit_within_60s_makes_one_request(make_client, repo, clock, fixed_now):
    server = FakeServer(JOURNEYS)
    client = make_client(server)
    when = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)

    first = await client.journeys(MONACO, NICE, when)
    clock.advance(seconds=30)
    second = await client.journeys(MONACO, NICE, when)

    assert len(server.requests) == 1
    assert len(server.base_requests) == 1  # R25: the base_schedule cross-check, cached 300 s
    assert repo.quota_count(fixed_now.date()) == 2
    assert second.items == first.items == expected_journeys()
    assert second.stale is False
    assert second.fetched_at == fixed_now


async def test_cache_key_truncates_when_to_the_minute(make_client, repo, fixed_now):
    server = FakeServer(JOURNEYS)
    client = make_client(server)
    await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, 10, tzinfo=PARIS))
    await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, 50, tzinfo=PARIS))
    assert len(server.requests) == 1

    await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 1, 0, tzinfo=PARIS))
    assert len(server.requests) == 2
    assert len(server.base_requests) == 2  # R25: one cross-check per distinct query
    assert repo.quota_count(fixed_now.date()) == 4


async def test_cache_distinguishes_routes(make_client):
    server = FakeServer(JOURNEYS)
    client = make_client(server)
    when = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)
    await client.journeys(MONACO, NICE, when)
    await client.journeys(NICE, MONACO, when)
    assert len(server.requests) == 2


async def test_cache_older_than_60s_is_refetched(make_client, repo, clock, fixed_now):
    server = FakeServer(JOURNEYS)
    client = make_client(server)
    when = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)

    await client.journeys(MONACO, NICE, when)
    clock.advance(seconds=61)
    result = await client.journeys(MONACO, NICE, when)

    assert len(server.requests) == 2
    assert len(server.base_requests) == 1  # R25: the cross-check is still fresh (300 s)
    assert repo.quota_count(fixed_now.date()) == 3
    assert result.stale is False
    assert result.fetched_at == fixed_now + timedelta(seconds=61)


async def test_cache_is_shared_through_sqlite(make_client):
    server = FakeServer(JOURNEYS)
    when = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)
    await make_client(server).journeys(MONACO, NICE, when)
    result = await make_client(server).journeys(MONACO, NICE, when)
    assert len(server.requests) == 1
    assert result.items == expected_journeys()


async def test_departures_cache_hit_within_the_same_minute(make_client, repo, clock, fixed_now):
    server = FakeServer(DEPARTURES_MONACO)
    client = make_client(server)
    clock.advance(seconds=5)
    await client.departures(MONACO)
    clock.advance(seconds=35)
    result = await client.departures(MONACO)
    assert len(server.requests) == 1
    assert repo.quota_count(fixed_now.date()) == 2  # R25: + 1 base_schedule cross-check
    assert result.items == parse_departures(DEPARTURES_MONACO, MONACO)
    assert result.stale is False


# --- quota accounting --------------------------------------------------------------------------


async def test_quota_counter_increments_per_consumer(make_client, repo, fixed_now):
    when = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)
    web_server = FakeServer(JOURNEYS)
    await make_client(web_server, consumer="web").journeys(MONACO, NICE, when)

    watcher_server = FakeServer(DEPARTURES_MONACO)
    await make_client(watcher_server, consumer="watcher").departures(MONACO)

    day = fixed_now.date()
    # R25: each call = 1 realtime + 1 base_schedule request, both counted for the caller.
    assert repo.quota_count(day, "web") == 2
    assert repo.quota_count(day, "watcher") == 2
    assert repo.quota_count(day) == 4


async def test_budget_refused_without_cache_raises_quota_error(make_client, budget, repo, fixed_now):
    for _ in range(3000):
        budget.record("web", fixed_now)
    server = FakeServer(JOURNEYS)
    client = make_client(server, consumer="web")

    with pytest.raises(SncfQuotaError):
        await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS))
    assert server.requests == []
    assert repo.quota_count(fixed_now.date(), "web") == 3000


async def test_budget_refused_serves_stale_cache(make_client, budget, repo, clock, fixed_now):
    server = FakeServer(JOURNEYS)
    client = make_client(server, consumer="web")
    when = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)
    await client.journeys(MONACO, NICE, when)

    for _ in range(3000):
        budget.record("web", fixed_now)
    clock.advance(minutes=2)
    result = await client.journeys(MONACO, NICE, when)

    assert len(server.requests) == 1
    assert result.stale is True
    assert result.items == expected_journeys()
    assert result.fetched_at == fixed_now


async def test_web_cut_does_not_stop_watcher(make_client, budget):
    for _ in range(3000):
        budget.record("web", datetime(2026, 11, 6, 7, 0, tzinfo=PARIS))
    server = FakeServer(DEPARTURES_MONACO)
    result = await make_client(server, consumer="watcher").departures(MONACO)
    assert len(server.requests) == 1
    assert result.stale is False


# --- errors ------------------------------------------------------------------------------------


async def test_unavailable_serves_stale(make_client, repo, clock, fixed_now):
    server = FakeServer(JOURNEYS)
    client = make_client(server)
    when = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)
    await client.journeys(MONACO, NICE, when)

    clock.advance(minutes=5)
    server.set_status(503)
    result = await client.journeys(MONACO, NICE, when)

    assert len(server.requests) == 2
    assert isinstance(result, Fetched)
    assert result.stale is True
    assert result.items == expected_journeys()
    assert result.fetched_at == fixed_now


async def test_503_without_cache_raises_unavailable(make_client):
    server = FakeServer(JOURNEYS)
    server.set_status(503)
    with pytest.raises(SncfUnavailable):
        await make_client(server).journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS))


async def test_500_without_cache_raises_unavailable(make_client):
    server = FakeServer(DEPARTURES_MONACO)
    server.set_status(500)
    with pytest.raises(SncfUnavailable):
        await make_client(server).departures(MONACO)


async def test_timeout_without_cache_raises_unavailable(make_client):
    server = FakeServer(JOURNEYS)
    server.set_timeout()
    with pytest.raises(SncfUnavailable):
        await make_client(server).journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS))


async def test_timeout_serves_stale(make_client, clock, fixed_now):
    server = FakeServer(JOURNEYS)
    client = make_client(server)
    when = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)
    await client.journeys(MONACO, NICE, when)

    clock.advance(minutes=3)
    server.set_timeout()
    result = await client.journeys(MONACO, NICE, when)
    assert result.stale is True
    assert result.items == expected_journeys()
    assert result.fetched_at == fixed_now


async def test_invalid_json_raises_unavailable(make_client):
    server = FakeServer(JOURNEYS)
    server.set_raw(b"<html>Bad gateway</html>", status=200)
    with pytest.raises(SncfUnavailable):
        await make_client(server).journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS))


async def test_429_without_cache_raises_and_blocks(make_client, budget, repo, clock, fixed_now, sncf_token, caplog):
    caplog.set_level(logging.DEBUG)
    server = FakeServer(JOURNEYS)
    server.set_status(429)
    client = make_client(server)
    when = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)

    with pytest.raises(SncfQuotaError) as excinfo:
        await client.journeys(MONACO, NICE, when)
    assert sncf_token not in str(excinfo.value)
    assert sncf_token not in caplog.text

    # Blocked until the next Paris midnight, for both consumers.
    assert repo.kv_get("quota_blocked_until") is not None
    assert not budget.allow("web", fixed_now)
    assert not budget.allow("watcher", fixed_now)
    assert not budget.allow("watcher", NEXT_MIDNIGHT - timedelta(seconds=1))
    assert budget.allow("watcher", NEXT_MIDNIGHT)
    assert budget.allow("web", NEXT_MIDNIGHT)

    # While blocked, no further HTTP request is sent.
    server.set(JOURNEYS)
    clock.advance(minutes=10)
    with pytest.raises(SncfQuotaError):
        await client.journeys(MONACO, NICE, when)
    assert len(server.requests) == 1


async def test_429_with_cache_serves_stale_and_blocks(make_client, budget, clock, fixed_now):
    server = FakeServer(JOURNEYS)
    client = make_client(server)
    when = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)
    await client.journeys(MONACO, NICE, when)

    clock.advance(minutes=2)
    server.set_status(429)
    result = await client.journeys(MONACO, NICE, when)
    assert result.stale is True
    assert result.items == expected_journeys()
    assert result.fetched_at == fixed_now
    assert not budget.allow("watcher", clock())


async def test_401_raises_auth_error_without_token_in_message(make_client, sncf_token, caplog):
    caplog.set_level(logging.DEBUG)
    server = FakeServer(JOURNEYS)
    server.set_status(401)
    client = make_client(server)

    with pytest.raises(SncfAuthError) as excinfo:
        await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS))

    encoded = base64.b64encode(f"{sncf_token}:".encode()).decode()
    exc = excinfo.value
    for text in (str(exc), repr(exc), caplog.text):
        assert sncf_token not in text
        assert encoded not in text
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        assert sncf_token not in str(cause)
        assert encoded not in str(cause)


async def test_401_raises_even_with_stale_cache(make_client, clock):
    server = FakeServer(JOURNEYS)
    client = make_client(server)
    when = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)
    await client.journeys(MONACO, NICE, when)

    clock.advance(minutes=2)
    server.set_status(401)
    with pytest.raises(SncfAuthError):
        await client.journeys(MONACO, NICE, when)


async def test_401_is_not_a_quota_or_unavailable_error(make_client):
    server = FakeServer(DEPARTURES_MONACO)
    server.set_status(401)
    with pytest.raises(SncfError) as excinfo:
        await make_client(server).departures(MONACO)
    assert isinstance(excinfo.value, SncfAuthError)
    assert not isinstance(excinfo.value, (SncfQuotaError, SncfUnavailable))


# --- controller ruling R4: cache keys, and stale fallback by family -----------------------------


async def test_departures_unavailable_serves_stale_from_an_earlier_minute(make_client, clock, fixed_now):
    server = FakeServer(DEPARTURES_MONACO)
    client = make_client(server)
    await client.departures(MONACO)  # 08:00

    clock.advance(minutes=3)  # 08:03
    server.set_status(503)
    result = await client.departures(MONACO)

    assert len(server.requests) == 2
    assert result.stale is True
    assert result.items == parse_departures(DEPARTURES_MONACO, MONACO)
    assert result.fetched_at == fixed_now


async def test_departures_cache_uses_ttl_not_minute(make_client, repo, clock, fixed_now):
    server = FakeServer(DEPARTURES_MONACO)
    client = make_client(server)
    await client.departures(MONACO)  # 08:00:00
    clock.advance(seconds=30)  # 08:00:30
    await client.departures(MONACO)
    assert len(server.requests) == 1

    clock.advance(seconds=35)  # 08:01:05, 65 s after the fetch
    result = await client.departures(MONACO)
    assert len(server.requests) == 2
    assert result.stale is False
    assert result.fetched_at == fixed_now + timedelta(seconds=65)
    assert repo.quota_count(fixed_now.date()) == 3  # R25: + 1 cross-check, still fresh at 65 s


async def test_departures_cache_hit_across_a_minute_boundary(make_client, clock):
    server = FakeServer(DEPARTURES_MONACO)
    client = make_client(server)
    clock.advance(seconds=50)  # 08:00:50
    await client.departures(MONACO)
    clock.advance(seconds=30)  # 08:01:20: 30 s later, but in another minute
    result = await client.departures(MONACO)
    assert len(server.requests) == 1
    assert result.stale is False


async def test_departures_cache_is_per_stop(make_client):
    server = FakeServer(DEPARTURES_MONACO)
    client = make_client(server)
    await client.departures(MONACO)
    await client.departures(NICE)
    assert len(server.requests) == 2


async def test_journeys_budget_refused_falls_back_to_latest_of_same_route(make_client, budget, clock, fixed_now):
    server = FakeServer(JOURNEYS)
    client = make_client(server, consumer="web")
    await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS))

    for _ in range(3000):
        budget.record("web", fixed_now)
    clock.advance(minutes=2)
    result = await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 5, tzinfo=PARIS))

    assert len(server.requests) == 1
    assert result.stale is True
    assert result.items == expected_journeys()
    assert result.fetched_at == fixed_now


async def test_journeys_fallback_family_is_per_route(make_client, budget, fixed_now):
    server = FakeServer(JOURNEYS)
    client = make_client(server, consumer="web")
    await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS))

    for _ in range(3000):
        budget.record("web", fixed_now)
    with pytest.raises(SncfQuotaError):
        await client.journeys(NICE, MONACO, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS))
    assert len(server.requests) == 1


async def test_journeys_fallback_prefers_exact_key_over_newer_family_entry(make_client, budget, clock, fixed_now):
    one_journey = copy.deepcopy(JOURNEYS)
    one_journey["journeys"] = one_journey["journeys"][:1]
    server = FakeServer(JOURNEYS)
    client = make_client(server, consumer="web")

    await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS))  # fetched 08:00, 4 trains
    clock.advance(minutes=1)
    server.set(one_journey)
    await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 5, tzinfo=PARIS))  # fetched 08:01, 1 train
    assert len(server.requests) == 2

    for _ in range(3000):
        budget.record("web", fixed_now)
    clock.advance(minutes=2)

    exact = await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS))
    assert exact.stale is True
    assert exact.items == expected_journeys()
    assert exact.fetched_at == fixed_now

    latest = await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 10, tzinfo=PARIS))
    assert latest.stale is True
    assert latest.items == parse_journeys(one_journey, MONACO, NICE)
    assert latest.fetched_at == fixed_now + timedelta(minutes=1)
    assert len(server.requests) == 2


async def test_journeys_503_falls_back_to_latest_of_same_route(make_client, clock, fixed_now):
    server = FakeServer(JOURNEYS)
    client = make_client(server)
    await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS))

    clock.advance(minutes=5)
    server.set_status(503)
    result = await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 5, tzinfo=PARIS))
    assert result.stale is True
    assert result.items == expected_journeys()
    assert result.fetched_at == fixed_now


# --- controller ruling R5: every request sent counts against the quota --------------------------


async def test_every_sent_request_counts_whatever_its_status(make_client, repo, fixed_now):
    server = FakeServer(JOURNEYS)
    client = make_client(server, consumer="web")
    day = fixed_now.date()

    server.set_status(503)
    with pytest.raises(SncfUnavailable):
        await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS))
    assert repo.quota_count(day, "web") == 1

    server.set_status(401)
    with pytest.raises(SncfAuthError):
        await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 1, tzinfo=PARIS))
    assert repo.quota_count(day, "web") == 2

    server.set_status(429)
    with pytest.raises(SncfQuotaError):
        await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 2, tzinfo=PARIS))
    assert repo.quota_count(day, "web") == 3
    assert repo.quota_count(day) == 3
    assert len(server.requests) == 3


async def test_watcher_failed_request_counts_for_watcher(make_client, repo, fixed_now):
    server = FakeServer(DEPARTURES_MONACO)
    server.set_status(500)
    with pytest.raises(SncfUnavailable):
        await make_client(server, consumer="watcher").departures(MONACO)
    assert repo.quota_count(fixed_now.date(), "watcher") == 1
    assert repo.quota_count(fixed_now.date(), "web") == 0


# --- controller ruling R7: Navitia 404 no_solution is an empty answer, other 4xx raise SncfError ---


def _navitia_error(error_id: str) -> dict:
    return {"error": {"id": error_id, "message": f"navitia error {error_id}"}}


async def test_journeys_404_no_solution_is_an_empty_answer(make_client, repo, clock, fixed_now):
    server = FakeServer(_navitia_error("no_solution"), status=404)
    client = make_client(server)
    when = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)

    result = await client.journeys(MONACO, NICE, when)
    assert result == Fetched(items=[], fetched_at=fixed_now, stale=False)

    # Cached like a 200: a second identical call within 60 s sends nothing.
    clock.advance(seconds=30)
    again = await client.journeys(MONACO, NICE, when)
    assert again == Fetched(items=[], fetched_at=fixed_now, stale=False)
    assert len(server.requests) == 1
    assert repo.quota_count(fixed_now.date()) == 2  # R25: + 1 base_schedule cross-check


async def test_departures_404_no_solution_is_an_empty_answer(make_client, repo, clock, fixed_now):
    server = FakeServer(_navitia_error("no_solution"), status=404)
    client = make_client(server)

    result = await client.departures(MONACO)
    assert result == Fetched(items=[], fetched_at=fixed_now, stale=False)

    clock.advance(seconds=30)
    again = await client.departures(MONACO)
    assert again.items == [] and again.stale is False
    assert len(server.requests) == 1
    assert repo.quota_count(fixed_now.date()) == 2  # R25: + 1 base_schedule cross-check


async def test_404_no_solution_is_cached_in_the_route_family(make_client, budget, clock, fixed_now):
    server = FakeServer(_navitia_error("no_solution"), status=404)
    client = make_client(server, consumer="web")
    await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS))

    for _ in range(3000):
        budget.record("web", fixed_now)
    clock.advance(minutes=2)
    result = await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 5, tzinfo=PARIS))
    assert result == Fetched(items=[], fetched_at=fixed_now, stale=True)
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    ("status", "error_id"),
    [(404, "unknown_object"), (400, "bad_format")],
)
async def test_other_4xx_raise_plain_sncf_error_with_navitia_id(make_client, sncf_token, status, error_id):
    server = FakeServer(_navitia_error(error_id), status=status)
    client = make_client(server)

    with pytest.raises(SncfError) as excinfo:
        await client.journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS))
    exc = excinfo.value
    assert not isinstance(exc, (SncfAuthError, SncfQuotaError, SncfUnavailable))
    assert error_id in str(exc)
    assert sncf_token not in str(exc)
    assert base64.b64encode(f"{sncf_token}:".encode()).decode() not in str(exc)


async def test_departures_404_unknown_object_raises_sncf_error(make_client):
    server = FakeServer(_navitia_error("unknown_object"), status=404)
    with pytest.raises(SncfError) as excinfo:
        await make_client(server).departures("stop_area:SNCF:00000000")
    assert not isinstance(excinfo.value, (SncfAuthError, SncfQuotaError, SncfUnavailable))
    assert "unknown_object" in str(excinfo.value)


# --- controller ruling R8: undecodable bodies and other httpx errors are SncfUnavailable --------

GARBAGE_BODIES = [b"\xff\xfe\x00garbage", b"\x80\x81garbage\xff"]


@pytest.mark.parametrize("content", GARBAGE_BODIES)
async def test_non_utf8_body_raises_unavailable(make_client, content):
    server = FakeServer(JOURNEYS)
    server.set_raw(content, status=200)
    with pytest.raises(SncfUnavailable):
        await make_client(server).journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS))


@pytest.mark.parametrize("content", GARBAGE_BODIES)
async def test_non_utf8_body_serves_stale(make_client, clock, fixed_now, content):
    server = FakeServer(JOURNEYS)
    client = make_client(server)
    when = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)
    await client.journeys(MONACO, NICE, when)

    clock.advance(minutes=2)
    server.set_raw(content, status=200)
    result = await client.journeys(MONACO, NICE, when)
    assert result.stale is True
    assert result.items == expected_journeys()
    assert result.fetched_at == fixed_now


def _raiser(exc_type: type[httpx.RequestError]):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_type("simulated", request=request)

    return handler


HTTPX_ERRORS = [httpx.DecodingError, httpx.TooManyRedirects]


@pytest.mark.parametrize("exc_type", HTTPX_ERRORS)
async def test_httpx_request_errors_raise_unavailable(make_client, exc_type):
    server = FakeServer(JOURNEYS)
    server.reply = _raiser(exc_type)
    with pytest.raises(SncfUnavailable):
        await make_client(server).journeys(MONACO, NICE, datetime(2026, 11, 6, 8, 0, tzinfo=PARIS))


@pytest.mark.parametrize("exc_type", HTTPX_ERRORS)
async def test_httpx_request_errors_serve_stale(make_client, clock, fixed_now, exc_type):
    server = FakeServer(DEPARTURES_MONACO)
    client = make_client(server)
    await client.departures(MONACO)

    clock.advance(minutes=2)
    server.reply = _raiser(exc_type)
    result = await client.departures(MONACO)
    assert result.stale is True
    assert result.items == parse_departures(DEPARTURES_MONACO, MONACO)
    assert result.fetched_at == fixed_now


# --- ruling R25: base_schedule cross-check for trains deleted from the realtime graph -----------
# Live capture 2026-11-12 ~12:15 (Nice -> Monaco): 86033 (12:26) and 881223 (13:03) are NO_SERVICE in
# base_schedule answers but ABSENT from realtime /journeys and /departures. journeys() and
# departures() therefore also send the same query with data_freshness=base_schedule (cached 300 s,
# counted in the quota like any request); every base_schedule item parsed as CANCELLED whose
# (train_number, base_departure) is absent from the realtime answer is added to the result, and
# the result is sorted by base_departure. A failed cross-check never fails the call.

LIVE_J_RT = load("live_journeys_nice_monaco_realtime.json")
LIVE_J_BASE = load("live_journeys_nice_monaco_base_schedule.json")
LIVE_D_RT = load("live_departures_nice_realtime.json")
LIVE_D_BASE = load("live_departures_nice_base_schedule.json")
LIVE_WHEN = datetime(2026, 11, 12, 12, 15, tzinfo=PARIS)


class FreshnessServer:
    """Answers realtime and base_schedule requests with their own body (or status)."""

    def __init__(self, realtime: dict, base: dict) -> None:
        self.bodies = {"realtime": realtime, "base_schedule": base}
        self.statuses = {"realtime": 200, "base_schedule": 200}
        self.requests: list[httpx.Request] = []

    def of(self, freshness: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.params.get("data_freshness") == freshness]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        freshness = request.url.params.get("data_freshness")
        status = self.statuses[freshness]
        if status != 200:
            return httpx.Response(status, json={"message": f"HTTP {status}"})
        return httpx.Response(200, json=self.bodies[freshness])


def _key(t) -> tuple:
    return (t.train_number, t.base_departure)


def _merged(realtime: list, base: list) -> list:
    seen = {_key(t) for t in realtime}
    added = [t for t in base if t.status is Status.CANCELLED and _key(t) not in seen]
    return sorted(realtime + added, key=lambda t: t.base_departure)


async def test_journeys_adds_cancelled_trains_missing_from_realtime(make_client, repo, fixed_now):
    server = FreshnessServer(LIVE_J_RT, LIVE_J_BASE)
    client = make_client(server)

    result = await client.journeys(NICE, MONACO, LIVE_WHEN)

    realtime = parse_journeys(LIVE_J_RT, NICE, MONACO)
    base = parse_journeys(LIVE_J_BASE, NICE, MONACO)
    assert result.items == _merged(realtime, base)
    assert result.stale is False

    by_number = {t.train_number: t for t in result.items}
    for number, hhmm in (("86033", (12, 26)), ("881223", (13, 3))):
        assert number not in {t.train_number for t in realtime}  # really absent from realtime
        t = by_number[number]
        assert t.status is Status.CANCELLED
        assert t.base_departure == datetime(2026, 11, 12, *hhmm, tzinfo=PARIS)
    assert {t.train_number for t in result.items} >= {t.train_number for t in realtime}
    assert len({_key(t) for t in result.items}) == len(result.items)  # no duplicates
    assert [t.base_departure for t in result.items] == sorted(t.base_departure for t in result.items)
    assert by_number["86037"].status is Status.DELAYED  # realtime wins over base for kept trains

    [rt_req], [base_req] = server.of("realtime"), server.of("base_schedule")
    rt_params, base_params = dict(rt_req.url.params), dict(base_req.url.params)
    assert base_params.pop("data_freshness") == "base_schedule"
    assert rt_params.pop("data_freshness") == "realtime"
    assert base_params == rt_params  # same query otherwise
    assert repo.quota_count(fixed_now.date(), "web") == 2


async def test_departures_adds_cancelled_trains_missing_from_realtime(make_client, repo, fixed_now):
    server = FreshnessServer(LIVE_D_RT, LIVE_D_BASE)
    client = make_client(server)

    result = await client.departures(NICE)

    realtime = parse_departures(LIVE_D_RT, NICE)
    base = parse_departures(LIVE_D_BASE, NICE)
    assert result.items == _merged(realtime, base)
    assert result.stale is False
    [t86033] = [t for t in result.items if t.train_number == "86033"]
    assert t86033.status is Status.CANCELLED
    assert t86033.base_departure == datetime(2026, 11, 12, 12, 26, tzinfo=PARIS)
    assert "86033" not in {t.train_number for t in realtime}
    assert len({_key(t) for t in result.items}) == len(result.items)

    [rt_req], [base_req] = server.of("realtime"), server.of("base_schedule")
    assert base_req.url.path == rt_req.url.path == f"/v1/coverage/sncf/stop_areas/{NICE}/departures"
    assert base_req.url.params["count"] == rt_req.url.params["count"]
    assert repo.quota_count(fixed_now.date(), "web") == 2


async def test_base_schedule_cross_check_cached_300_s(make_client, repo, clock, fixed_now):
    server = FreshnessServer(LIVE_J_RT, LIVE_J_BASE)
    client = make_client(server, consumer="watcher")

    await client.journeys(NICE, MONACO, LIVE_WHEN)
    assert (len(server.of("realtime")), len(server.of("base_schedule"))) == (1, 1)

    clock.advance(seconds=120)
    result = await client.journeys(NICE, MONACO, LIVE_WHEN)
    assert (len(server.of("realtime")), len(server.of("base_schedule"))) == (2, 1)  # only the realtime one
    assert "86033" in {t.train_number for t in result.items}  # still merged from the cached cross-check
    assert repo.quota_count(fixed_now.date(), "watcher") == 3

    clock.advance(seconds=181)  # 301 s after the cross-check
    await client.journeys(NICE, MONACO, LIVE_WHEN)
    assert (len(server.of("realtime")), len(server.of("base_schedule"))) == (3, 2)
    assert repo.quota_count(fixed_now.date(), "watcher") == 5


@pytest.mark.parametrize("method", ["journeys", "departures"])
async def test_base_schedule_failure_returns_realtime_alone(make_client, method, fixed_now):
    if method == "journeys":
        server = FreshnessServer(LIVE_J_RT, LIVE_J_BASE)
        call = lambda c: c.journeys(NICE, MONACO, LIVE_WHEN)  # noqa: E731
        realtime = parse_journeys(LIVE_J_RT, NICE, MONACO)
    else:
        server = FreshnessServer(LIVE_D_RT, LIVE_D_BASE)
        call = lambda c: c.departures(NICE)  # noqa: E731
        realtime = parse_departures(LIVE_D_RT, NICE)
    server.statuses["base_schedule"] = 503

    result = await call(make_client(server))  # no exception

    assert sorted(result.items, key=_key) == sorted(realtime, key=_key)
    assert result.stale is False
    assert result.fetched_at == fixed_now
    assert len(server.of("base_schedule")) == 1


async def test_base_schedule_answer_never_serves_as_realtime_fallback(make_client, clock, fixed_now):
    server = FreshnessServer(LIVE_J_RT, LIVE_J_BASE)
    client = make_client(server)
    await client.journeys(NICE, MONACO, LIVE_WHEN)  # caches the realtime answer, then the base_schedule one

    clock.advance(minutes=2)
    server.statuses["realtime"] = 503
    result = await client.journeys(NICE, MONACO, LIVE_WHEN + timedelta(minutes=5))  # exact key unknown

    assert result.stale is True
    assert result.fetched_at == fixed_now
    numbers = {t.train_number: t for t in result.items}
    assert "22031" in numbers  # a realtime-only train: the fallback is the realtime answer
    assert numbers["86037"].status is Status.DELAYED and numbers["86037"].delay_min == 60
