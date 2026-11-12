"""Task 7: mobile web site (monatrain.web.app.create_app).

Interface pinned here:
- `create_app(settings=None, *, source=None, repo=None, http=None, clock=None) -> FastAPI`.
  `clock: Callable[[], datetime] | None` returns the current aware Europe/Paris datetime; it is the
  "now" of every page (default search time, today's statuses, add_follow, stale age). Default:
  `timeutil.now_paris`. Each app owns its own per-IP limiter (30 hits / 600 s).
- The logged-in user is `request.session["uid"]` (Starlette SessionMiddleware, cookie "session",
  secret `settings.session_secret`). Tests sign that cookie themselves with itsdangerous.
- `source` has `journeys(from_id, to_id, when, count=8)` and `departures(stop_id, count=10)`, both
  returning `Fetched[list[TrainInfo]]`.
- Form fields of `POST /suivis`: `de`, `vers`, `heure` (HH:MM), `jours` (repeatable: either one
  preset `semaine` / `tous` / `week-end`, or day names `lun`..`dim`, one per checked box).
  `POST /suivis/{id}/remplacer` takes the field `heure`.
- `/gare?id=<stop>&vers=<direction>`: `vers` is the direction (terminus) text of the trains.

Assertions are on visible French text (HTML-unescaped) and status codes, not on HTML structure.
"""

import base64
import html
import json
import logging
import re
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import itsdangerous
import pytest
from fastapi.testclient import TestClient

from monatrain.config import Settings
from monatrain.models import Fetched, Status, TrainInfo
from monatrain.sncf.client import SncfAuthError, SncfUnavailable
from monatrain.timeutil import PARIS, WEEKDAYS
from monatrain.web.app import create_app

MONACO = "stop_area:SNCF:87756403"
NICE = "stop_area:SNCF:87756056"
CANNES = "stop_area:SNCF:87757625"
UNKNOWN = "stop_area:SNCF:87000000"

FRI = date(2026, 11, 6)  # the conftest clock starts on Friday 2026-11-06 08:00 Paris
THU = date(2026, 11, 5)

USER = "111"
OTHER_USER = "222"
SECRET = "test-secret"

MSG_RATE = "Trop de recherches, réessaie dans quelques minutes."
MSG_DOWN = "Horaires SNCF momentanément indisponibles."
MSG_NO_DM = "Le bot ne peut pas t'envoyer de DM"


def at(d: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=PARIS)


def train(
    base: datetime,
    number: str = "86053",
    *,
    delay: int = 0,
    status: Status = Status.ON_TIME,
    from_id: str = MONACO,
    to_id: str | None = NICE,
    direction: str = "Nice-Ville",
    reason: str | None = None,
) -> TrainInfo:
    dep = base + timedelta(minutes=delay)
    return TrainInfo(
        from_id=from_id,
        to_id=to_id,
        base_departure=base,
        departure=dep,
        base_arrival=base + timedelta(minutes=20) if to_id else None,
        arrival=dep + timedelta(minutes=20) if to_id else None,
        train_number=number,
        line="TER",
        direction=direction,
        physical_mode="TER / Intercités",
        status=status,
        delay_min=delay,
        reason=reason,
        realtime=True,
    )


class FakeSource:
    """Stands in for SncfClient. Returns the same canned lists whatever is asked (like a stale
    cache entry of another query would), and records every call."""

    def __init__(
        self,
        journeys: list[TrainInfo] | None = None,
        departures: list[TrainInfo] | None = None,
        *,
        stale: bool = False,
        fetched_at: datetime | None = None,
        error: Exception | None = None,
    ) -> None:
        self.journey_items = list(journeys or [])
        self.departure_items = list(departures or [])
        self.stale = stale
        self.fetched_at = fetched_at or at(FRI, "08:00")
        self.error = error
        self.calls: list[tuple] = []

    async def journeys(self, from_id: str, to_id: str, when: datetime, count: int = 8) -> Fetched[list[TrainInfo]]:
        self.calls.append(("journeys", from_id, to_id, when, count))
        if self.error is not None:
            raise self.error
        return Fetched(list(self.journey_items), self.fetched_at, self.stale)

    async def departures(self, stop_id: str, count: int = 10) -> Fetched[list[TrainInfo]]:
        self.calls.append(("departures", stop_id, count))
        if self.error is not None:
            raise self.error
        return Fetched(list(self.departure_items), self.fetched_at, self.stale)


def make_settings() -> Settings:
    return Settings(
        sncf_token="x",
        discord_bot_token=None,
        discord_client_id=None,
        discord_client_secret=None,
        session_secret=SECRET,
        public_url="http://testserver",
        db_path=":memory:",
    )


@pytest.fixture
def make_web(repo, clock):
    """make_web(source) -> TestClient over a fresh app sharing the test's repo and clock."""
    clients: list[TestClient] = []

    def factory(source: FakeSource | None = None) -> TestClient:
        app = create_app(make_settings(), source=source or FakeSource(), repo=repo, clock=clock)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


def login(client: TestClient, uid: str) -> None:
    """Set a session cookie exactly as Starlette's SessionMiddleware signs it."""
    signer = itsdangerous.TimestampSigner(SECRET)
    data = signer.sign(base64.b64encode(json.dumps({"uid": uid}).encode("utf-8")))
    client.cookies.set("session", data.decode("utf-8"))


def user(repo, uid: str = USER, *, dm_ok: bool = True) -> str:
    repo.upsert_user(uid, f"user{uid}", None)
    if not dm_ok:
        repo.set_dm_ok(uid, False)
    return uid


def text(resp) -> str:
    """Response body with HTML entities decoded (Jinja escapes apostrophes)."""
    return html.unescape(resp.text)


def links(resp) -> list[tuple[str, dict[str, list[str]]]]:
    """Every href of the page as (path, query dict)."""
    out = []
    for href in re.findall(r"""href\s*=\s*["']([^"']*)["']""", resp.text):
        parts = urlsplit(html.unescape(href))
        out.append((parts.path, parse_qs(parts.query)))
    return out


# --- home ----------------------------------------------------------------------------------------


def test_home_redirects_when_logged_out(make_web):
    client = make_web()

    resp = client.get("/", follow_redirects=False)

    assert resp.status_code == 303
    assert urlsplit(resp.headers["location"]).path == "/trajet"


def test_home_lists_today_trains_with_badges(make_web, repo):
    uid = user(repo)
    # now = Fri 08:00: "live" window is [07:30, 11:00].
    repo.add_follow(uid, MONACO, NICE, "07:00", WEEKDAYS, "86001")  # past
    repo.add_follow(uid, MONACO, NICE, "08:04", WEEKDAYS, "86053")  # delayed +8
    repo.add_follow(uid, MONACO, NICE, "08:20", WEEKDAYS, "86055")  # on time
    repo.add_follow(uid, MONACO, NICE, "08:34", WEEKDAYS, "86057")  # cancelled
    repo.add_follow(uid, MONACO, NICE, "08:50", WEEKDAYS, "86059")  # replaced by bus
    missing = repo.add_follow(uid, MONACO, NICE, "09:04", WEEKDAYS, "86061")  # missing
    repo.add_follow(uid, MONACO, NICE, "18:00", WEEKDAYS, "86101")  # upcoming
    repo.set_found(missing.id, FRI, False)
    source = FakeSource(
        [
            train(at(FRI, "08:04"), "86053", delay=8, status=Status.DELAYED, reason="Panne de signalisation"),
            train(at(FRI, "08:20"), "86055"),
            train(at(FRI, "08:34"), "86057", status=Status.CANCELLED),
            train(at(FRI, "08:50"), "86059", status=Status.REPLACED_BY_BUS),
        ]
    )
    client = make_web(source)
    login(client, uid)

    resp = client.get("/")

    assert resp.status_code == 200
    page = text(resp)
    for badge in ("À l'heure", "+8 min", "Supprimé", "Remplacé par car", "Introuvable", "Prévu", "Parti"):
        assert badge in page, badge
    assert "08:04" in page and "08:12" in page  # scheduled (struck) and expected times
    assert "Panne de signalisation" in page  # the reason is shown
    assert "Données d'il y a" not in page
    assert MSG_NO_DM not in page
    # the quick search is prefilled with the route of the first followed train
    assert MONACO in resp.text and NICE in resp.text


def test_home_dm_banner_when_bot_cannot_dm(make_web, repo):
    uid = user(repo, dm_ok=False)
    client = make_web()
    login(client, uid)

    resp = client.get("/")

    assert resp.status_code == 200
    assert MSG_NO_DM in text(resp)


# --- trajet --------------------------------------------------------------------------------------


def test_trajet_shows_trains_and_swap_link(make_web):
    source = FakeSource(
        [
            train(at(THU, "08:10"), "86900"),  # another day (stale entry): not listed
            train(at(FRI, "07:30"), "86051"),  # before the requested time: not listed
            train(at(FRI, "08:00"), "86052"),
            train(at(FRI, "08:04"), "86053", delay=8, status=Status.DELAYED),
            train(at(FRI, "08:40"), "86055"),
        ]
    )
    client = make_web(source)

    resp = client.get("/trajet", params={"de": MONACO, "vers": NICE, "heure": "08:00"})

    assert resp.status_code == 200
    page = text(resp)
    assert "Monaco Monte-Carlo" in page and "Nice-Ville" in page
    assert "08:04" in page and "08:12" in page and "+8 min" in page
    assert "08:40" in page and "À l'heure" in page
    assert "86051" not in page and "86900" not in page
    assert any(c[0] == "journeys" and c[1:3] == (MONACO, NICE) for c in source.calls)
    swap = [q for path, q in links(resp) if path == "/trajet" and q.get("de") == [NICE] and q.get("vers") == [MONACO]]
    assert swap, "missing the ⇅ link that swaps de and vers"


def test_trajet_defaults_to_current_time(make_web):
    source = FakeSource([train(at(FRI, "07:30"), "86051"), train(at(FRI, "08:04"), "86053")])
    client = make_web(source)

    resp = client.get("/trajet", params={"de": MONACO, "vers": NICE})

    assert resp.status_code == 200
    page = text(resp)
    assert "08:04" in page
    assert "86051" not in page


def test_trajet_without_stations_shows_selectors_without_api_call(make_web):
    source = FakeSource()
    client = make_web(source)

    resp = client.get("/trajet")

    assert resp.status_code == 200
    assert "Monaco Monte-Carlo" in text(resp)  # the station selectors list the stations
    assert source.calls == []


def test_healthz(make_web):
    client = make_web()

    resp = client.get("/healthz")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# --- gare ----------------------------------------------------------------------------------------


def _nice_departures() -> list[TrainInfo]:
    return [
        train(at(FRI, "08:05"), "86501", from_id=NICE, to_id=None, direction="Vintimille"),
        train(at(FRI, "08:10"), "86502", from_id=NICE, to_id=None, direction="Cannes"),
        train(at(FRI, "08:15"), "86503", from_id=NICE, to_id=None, direction="Grasse"),
        train(at(FRI, "08:25"), "86504", from_id=NICE, to_id=None, direction="Vintimille"),
    ]


def test_gare_filters_by_direction(make_web):
    source = FakeSource(departures=_nice_departures())
    client = make_web(source)

    resp = client.get("/gare", params={"id": NICE})

    assert resp.status_code == 200
    page = text(resp)
    assert "Nice-Ville" in page
    for hhmm in ("08:05", "08:10", "08:15", "08:25"):
        assert hhmm in page
    assert any(c[0] == "departures" and c[1] == NICE for c in source.calls)
    # one quick filter link per terminus present in the answer
    filters = {q["vers"][0] for path, q in links(resp) if path == "/gare" and q.get("id") == [NICE] and "vers" in q}
    assert {"Vintimille", "Cannes", "Grasse"} <= filters

    resp = client.get("/gare", params={"id": NICE, "vers": "Vintimille"})

    assert resp.status_code == 200
    page = text(resp)
    assert "08:05" in page and "08:25" in page
    assert "08:10" not in page and "08:15" not in page
    assert "86502" not in page and "86503" not in page


# --- following trains ----------------------------------------------------------------------------


def test_suivre_form_requires_login_and_has_time_input(make_web, repo):
    client = make_web()

    resp = client.get("/suivre", follow_redirects=False)
    assert resp.status_code in (302, 303, 401, 403)

    login(client, user(repo))
    resp = client.get("/suivre")
    assert resp.status_code == 200
    assert re.search(r"""type\s*=\s*["']?time""", resp.text)


def test_follow_post_success_and_error_message(make_web, repo):
    uid = user(repo)
    source = FakeSource([train(at(FRI, "07:52"), "86051"), train(at(FRI, "08:04"), "86053")])
    client = make_web(source)
    login(client, uid)

    resp = client.post(
        "/suivis",
        data={"de": MONACO, "vers": NICE, "heure": "08:05", "jours": "semaine"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert urlsplit(resp.headers["location"]).path == "/"
    [saved] = repo.list_follows(uid)
    assert (saved.from_id, saved.to_id, saved.dep_time, saved.days, saved.train_number) == (
        MONACO, NICE, "08:04", WEEKDAYS, "86053",
    )
    home = client.get("/")
    assert home.status_code == 200
    assert "Enregistré : 08:04 (train 86053), lun-ven" in text(home)

    # FollowError: the form is shown again with the French message, nothing saved
    resp = client.post("/suivis", data={"de": MONACO, "vers": NICE, "heure": "8h", "jours": "semaine"})
    assert resp.status_code in (200, 400)
    assert "Heure invalide, utilise le format HH:MM." in text(resp)

    resp = client.post("/suivis", data={"de": UNKNOWN, "vers": NICE, "heure": "08:04", "jours": "semaine"})
    assert resp.status_code in (200, 400)
    assert "Gare inconnue." in text(resp)

    resp = client.post("/suivis", data={"de": MONACO, "vers": NICE, "heure": "08:04", "jours": "semaine"})
    assert resp.status_code in (200, 400)
    assert "Tu suis déjà ce train." in text(resp)

    assert len(repo.list_follows(uid)) == 1


def test_follow_post_with_day_checkboxes(make_web, repo):
    uid = user(repo)
    client = make_web(FakeSource([train(at(FRI, "08:04"), "86053")]))
    login(client, uid)

    resp = client.post(
        "/suivis",
        data={"de": MONACO, "vers": NICE, "heure": "08:04", "jours": ["lun", "mer", "ven"]},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    [saved] = repo.list_follows(uid)
    assert saved.days == 1 | 4 | 16


def test_follow_post_requires_login(make_web, repo):
    user(repo)
    client = make_web(FakeSource([train(at(FRI, "08:04"), "86053")]))

    resp = client.post(
        "/suivis",
        data={"de": MONACO, "vers": NICE, "heure": "08:04", "jours": "semaine"},
        follow_redirects=False,
    )

    assert resp.status_code in (302, 303, 401, 403)
    assert repo.all_follows() == []


def test_delete_only_own_follow(make_web, repo):
    mine = repo.add_follow(user(repo, USER), MONACO, NICE, "08:04", WEEKDAYS, "86053")
    theirs = repo.add_follow(user(repo, OTHER_USER), MONACO, NICE, "08:04", WEEKDAYS, "86053")
    client = make_web()
    login(client, USER)

    client.post(f"/suivis/{theirs.id}/supprimer")
    assert [f.id for f in repo.list_follows(OTHER_USER)] == [theirs.id]

    resp = client.post(f"/suivis/{mine.id}/supprimer")
    assert resp.status_code < 400
    assert repo.list_follows(USER) == []
    assert [f.id for f in repo.list_follows(OTHER_USER)] == [theirs.id]


def test_replace_follow_time(make_web, repo):
    mine = repo.add_follow(user(repo, USER), MONACO, NICE, "08:04", WEEKDAYS, "86053")
    theirs = repo.add_follow(user(repo, OTHER_USER), MONACO, NICE, "08:04", WEEKDAYS, "86053")
    source = FakeSource([train(at(FRI, "08:04"), "86053"), train(at(FRI, "08:20"), "86055")])
    client = make_web(source)
    login(client, USER)

    resp = client.post(f"/suivis/{mine.id}/remplacer", data={"heure": "08:20"})
    assert resp.status_code < 400
    [updated] = repo.list_follows(USER)
    assert (updated.id, updated.dep_time, updated.train_number) == (mine.id, "08:20", "86055")

    client.post(f"/suivis/{theirs.id}/remplacer", data={"heure": "08:20"})
    [untouched] = repo.list_follows(OTHER_USER)
    assert (untouched.dep_time, untouched.train_number) == ("08:04", "86053")


# --- per-IP rate limit ---------------------------------------------------------------------------


def test_rate_limit_31st_request_429_by_cf_ip(make_web):
    client = make_web(FakeSource([train(at(FRI, "08:04"), "86053")], _nice_departures()))
    ip_a = {"CF-Connecting-IP": "203.0.113.7"}
    params = {"de": MONACO, "vers": NICE, "heure": "08:00"}

    # one counter per IP across /trajet, /gare and /fragments/*
    for _ in range(10):
        assert client.get("/trajet", params=params, headers=ip_a).status_code == 200
    for _ in range(10):
        assert client.get("/gare", params={"id": NICE}, headers=ip_a).status_code == 200
    for _ in range(10):
        assert client.get("/fragments/trajet", params=params, headers=ip_a).status_code == 200

    resp = client.get("/trajet", params=params, headers=ip_a)
    assert resp.status_code == 429
    assert MSG_RATE in text(resp)
    assert client.get("/gare", params={"id": NICE}, headers=ip_a).status_code == 429

    # another CF-Connecting-IP behind the same proxy is not affected
    assert client.get("/trajet", params=params, headers={"CF-Connecting-IP": "198.51.100.9"}).status_code == 200
    # pages that do not call the API are not limited
    assert client.get("/healthz", headers=ip_a).status_code == 200


def test_rate_limit_falls_back_to_client_host(make_web):
    client = make_web(FakeSource([train(at(FRI, "08:04"), "86053")]))
    params = {"de": MONACO, "vers": NICE, "heure": "08:00"}

    for _ in range(30):
        assert client.get("/trajet", params=params).status_code == 200
    resp = client.get("/trajet", params=params)

    assert resp.status_code == 429
    assert MSG_RATE in text(resp)


def test_rate_limit_applies_to_follow_post(make_web, repo):
    uid = user(repo)
    client = make_web(FakeSource([train(at(FRI, "08:04"), "86053")]))
    login(client, uid)
    ip = {"CF-Connecting-IP": "203.0.113.8"}

    for _ in range(30):
        assert client.get("/fragments/trajet", params={"de": MONACO, "vers": NICE, "heure": "08:00"}, headers=ip).status_code == 200
    resp = client.post(
        "/suivis",
        data={"de": MONACO, "vers": NICE, "heure": "08:04", "jours": "semaine"},
        headers=ip,
        follow_redirects=False,
    )

    assert resp.status_code == 429
    assert MSG_RATE in text(resp)
    assert repo.list_follows(uid) == []


# --- stale data / SNCF down ----------------------------------------------------------------------


def test_stale_banner(make_web):
    stale = FakeSource([train(at(FRI, "08:04"), "86053")], stale=True, fetched_at=at(FRI, "07:53"))
    client = make_web(stale)

    resp = client.get("/trajet", params={"de": MONACO, "vers": NICE, "heure": "08:00"})

    assert resp.status_code == 200
    assert "Données d'il y a 7 min" in text(resp)
    assert "08:04" in text(resp)

    fresh = make_web(FakeSource([train(at(FRI, "08:04"), "86053")], fetched_at=at(FRI, "07:53")))
    resp = fresh.get("/trajet", params={"de": MONACO, "vers": NICE, "heure": "08:00"})
    assert "Données d'il y a" not in text(resp)


def test_sncf_down_503_french(make_web):
    client = make_web(FakeSource(error=SncfUnavailable("boom")))

    resp = client.get("/trajet", params={"de": MONACO, "vers": NICE, "heure": "08:00"})
    assert resp.status_code == 503
    assert MSG_DOWN in text(resp)
    assert "boom" not in resp.text

    resp = client.get("/gare", params={"id": NICE})
    assert resp.status_code == 503
    assert MSG_DOWN in text(resp)


def test_sncf_auth_error_503_french_and_logged(make_web, caplog):
    client = make_web(FakeSource(error=SncfAuthError("401 token refusé")))

    with caplog.at_level(logging.ERROR):
        resp = client.get("/trajet", params={"de": MONACO, "vers": NICE, "heure": "08:00"})

    assert resp.status_code == 503
    assert MSG_DOWN in text(resp)
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


# --- fragments -----------------------------------------------------------------------------------


def _is_partial(resp) -> bool:
    body = resp.text.lower()
    return "<html" not in body and "<body" not in body and "<head" not in body


def test_fragment_returns_partial_only(make_web, repo):
    source = FakeSource([train(at(FRI, "08:04"), "86053", delay=8, status=Status.DELAYED)], _nice_departures())
    client = make_web(source)

    resp = client.get("/fragments/trajet", params={"de": MONACO, "vers": NICE, "heure": "08:00"})
    assert resp.status_code == 200
    assert _is_partial(resp)
    assert "+8 min" in text(resp) and "08:12" in text(resp)

    resp = client.get("/fragments/gare", params={"id": NICE, "vers": "Grasse"})
    assert resp.status_code == 200
    assert _is_partial(resp)
    assert "08:15" in text(resp) and "08:05" not in text(resp)

    uid = user(repo)
    repo.add_follow(uid, MONACO, NICE, "08:04", WEEKDAYS, "86053")
    login(client, uid)
    resp = client.get("/fragments/mes-trains")
    assert resp.status_code == 200
    assert _is_partial(resp)
    assert "+8 min" in text(resp)


# --- layout --------------------------------------------------------------------------------------


def test_pages_are_french_mobile_layout(make_web):
    client = make_web()

    resp = client.get("/trajet")

    assert re.search(r"""<html[^>]*\blang\s*=\s*["']fr["']""", resp.text)
    assert re.search(r"""<meta[^>]*name\s*=\s*["']viewport["']""", resp.text)
    page = text(resp)
    for tab in ("Accueil", "Trajet", "Gare"):
        assert tab in page
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/manifest.webmanifest").status_code == 200
