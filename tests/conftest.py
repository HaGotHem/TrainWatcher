"""Shared fixtures (Task 3 onwards).

- `fixed_now`: Friday 2026-11-06 08:00:00, aware Europe/Paris.
- `clock`: a mutable fake clock starting at `fixed_now` (call it to read, `advance(...)` to move it).
- `db`: an in-memory SQLite connection from `connect(":memory:")` with the schema applied.
- `repo`: `Repo(db)`.
- `budget`: `QuotaBudget(repo)` with the default limits.
- `sncf_token`: a fake SNCF token (never the real one).
- `make_client(handler, *, consumer="web", clock=None, budget=None, token=None)`: an `SncfClient`
  whose HTTP goes through `httpx.MockTransport(handler)`. It shares `repo` (so its cache and quota
  counters are visible to the test) and, by default, the `clock` and `budget` fixtures.
"""

from collections.abc import Callable
from datetime import datetime, timedelta

import httpx
import pytest
import pytest_asyncio

from monatrain.timeutil import PARIS

# Task 3 modules are imported inside the fixtures, so that tests of earlier tasks keep
# collecting and passing while these modules do not exist yet.

FAKE_SNCF_TOKEN = "fake-sncf-token-7c41e9b2d0"


class FakeClock:
    """Callable clock returning an aware Paris datetime that the test moves explicitly."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now = self.now + timedelta(**delta)
        return self.now


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 11, 6, 8, 0, 0, tzinfo=PARIS)


@pytest.fixture
def clock(fixed_now: datetime) -> FakeClock:
    return FakeClock(fixed_now)


@pytest.fixture
def db():
    from monatrain.db import connect, init_schema

    conn = connect(":memory:")
    init_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def repo(db):
    from monatrain.repo import Repo

    return Repo(db)


@pytest.fixture
def budget(repo):
    from monatrain.budget import QuotaBudget

    return QuotaBudget(repo)


@pytest.fixture
def sncf_token() -> str:
    return FAKE_SNCF_TOKEN


@pytest_asyncio.fixture
async def make_client(repo, budget, clock: FakeClock, sncf_token: str):
    from monatrain.sncf.client import SncfClient

    http_clients: list[httpx.AsyncClient] = []

    def factory(
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        consumer: str = "web",
        clock: Callable[[], datetime] | None = None,
        budget=None,
        token: str | None = None,
    ):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        http_clients.append(http)
        return SncfClient(
            token if token is not None else sncf_token,
            repo,
            budget if budget is not None else default_budget,
            consumer,
            http=http,
            clock=clock if clock is not None else default_clock,
        )

    default_budget = budget
    default_clock = clock
    yield factory
    for http in http_clients:
        await http.aclose()
