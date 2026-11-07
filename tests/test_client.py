"""Task 3: async SNCF client with SQLite-backed cache, quota budget and error mapping.

HTTP goes through `httpx.MockTransport` (see `make_client` in conftest.py); response bodies are
the real fixtures captured in Task 2. Time comes from the injected `clock` fixture.
"""

import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from monatrain.models import Fetched
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
    """

    def __init__(self, body: dict | None = None, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
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
    assert repo.quota_count(fixed_now.date()) == 1
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
    assert repo.quota_count(fixed_now.date()) == 2


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
    assert repo.quota_count(fixed_now.date()) == 2
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
    assert repo.quota_count(fixed_now.date()) == 1
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
    assert repo.quota_count(day, "web") == 1
    assert repo.quota_count(day, "watcher") == 1
    assert repo.quota_count(day) == 2


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
