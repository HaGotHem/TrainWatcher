"""Pages, forms and fragments of the Monatrain web site."""

import re
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from monatrain.display import hhmm, station_name, status_label, today_label
from monatrain.follow import FollowError, TodayItem, add_follow, replace_follow_time, today_statuses
from monatrain.models import Fetched, Status, TrainInfo
from monatrain.stations import STATIONS, get_station
from monatrain.timeutil import DAY_NAMES, PARIS, format_days, parse_days

_HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
# Day presets of the follow form: (label, comma-separated day names); applied by app.js.
DAY_PRESETS = (("Semaine", "lun,mar,mer,jeu,ven"), ("Week-end", "sam,dim"), ("Tous", ",".join(DAY_NAMES)))
GARE_DEPARTURES = 20


class RateLimited(Exception):
    pass


class LoginRequired(Exception):
    pass


def with_query(path: str, **params: str) -> str:
    return f"{path}?{urlencode(params, quote_via=quote)}"


templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals.update(stations=STATIONS, station_name=station_name, with_query=with_query)

router = APIRouter()


# --- view models ---------------------------------------------------------------------------------


@dataclass
class Row:
    """One line of a train list (`_trains.html`)."""

    badge: str
    badge_kind: str  # ok | delay | cancel | neutral
    scheduled: str
    expected: str | None = None  # only set when it differs from `scheduled`
    title: str = ""
    meta: str = ""
    reason: str | None = None
    suivre: str | None = None  # link to the follow form, prefilled


@dataclass
class TrainsView:
    """Everything `_trains.html` needs."""

    rows: list[Row] = field(default_factory=list)
    stale_min: int | None = None
    empty: str = "Aucun train direct trouvé."
    notice: str | None = None  # shown instead of the list (e.g. an unknown station)


_BADGE_KINDS = {Status.CANCELLED: "cancel", Status.REPLACED_BY_BUS: "cancel", Status.DELAYED: "delay"}


def train_row(train: TrainInfo, *, title: str, meta: str, suivre: str | None = None) -> Row:
    badge, kind = status_label(train), _BADGE_KINDS.get(train.status, "ok")
    scheduled, expected = hhmm(train.base_departure), hhmm(train.departure)
    return Row(badge, kind, scheduled, expected if expected != scheduled else None, title, meta, train.reason, suivre)


def today_row(item: TodayItem) -> Row:
    follow, train = item.follow, item.train
    title = f"{station_name(follow.from_id)} → {station_name(follow.to_id)}"
    if train is not None:
        meta = f"Train {train.train_number}" + (f" · arrivée {hhmm(train.arrival)}" if train.arrival else "")
        return train_row(train, title=title, meta=meta)
    badge = today_label(item)
    return Row(badge, "neutral", follow.dep_time, None, title, f"Train {follow.train_number}")


# --- helpers -------------------------------------------------------------------------------------


def current_user(request: Request) -> dict | None:
    uid = request.session.get("uid")
    return request.app.state.repo.get_user(str(uid)) if uid else None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise LoginRequired()
    return user


def _client_ip(request: Request) -> str:
    return request.headers.get("CF-Connecting-IP") or (request.client.host if request.client else "inconnu")


def rate_limit(request: Request) -> None:
    """Searches and actions: one bucket per IP."""
    if not request.app.state.limiter.hit(_client_ip(request)):
        raise RateLimited()


def refresh_rate_limit(request: Request) -> None:
    """Auto-refresh fragments and the home page: a separate, larger bucket per IP."""
    if not request.app.state.refresh_limiter.hit(_client_ip(request)):
        raise RateLimited()


def render(request: Request, name: str, tab: str | None, status: int = 200, **context) -> HTMLResponse:
    context.update(user=current_user(request), tab=tab, settings=request.app.state.settings)
    return templates.TemplateResponse(request, name, context, status_code=status)


def fragment(request: Request, view: TrainsView | None) -> HTMLResponse:
    return templates.TemplateResponse(request, "_trains.html", {"view": view or TrainsView()})


def age_minutes(now: datetime, fetched_at: datetime) -> int:
    return max(0, int((now - fetched_at).total_seconds() // 60))


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def parse_when(heure: str, now: datetime) -> datetime:
    """Today at `heure` (HH:MM); the current time when it is blank or invalid."""
    text = heure if _HHMM.fullmatch(heure) else hhmm(now)
    return datetime.combine(now.date(), time.fromisoformat(text), tzinfo=PARIS)


# --- trajet --------------------------------------------------------------------------------------


async def build_trajet(request: Request, de: str, vers: str, when: datetime) -> TrainsView | None:
    """The train list of a search, or None when no station pair was given."""
    if not de or not vers:
        return None
    if get_station(de) is None or get_station(vers) is None:
        return TrainsView(notice="Gare inconnue.")
    if de == vers:
        return TrainsView(notice="Les gares de départ et d'arrivée doivent être différentes.")
    state = request.app.state
    fetched: Fetched[list[TrainInfo]] = await state.source.journeys(de, vers, when, 8)
    # A stale answer may hold trains of another time or date: keep those leaving from `when` on, that day.
    trains = [
        t for t in fetched.items if t.departure >= when and t.base_departure.astimezone(PARIS).date() == when.date()
    ]
    rows = [
        train_row(
            t,
            title=f"Arrivée {hhmm(t.arrival)}" if t.arrival else "",
            meta=f"Train {t.train_number} · direction {t.direction}",
            suivre=with_query("/suivre", de=de, vers=vers, heure=hhmm(t.base_departure)),
        )
        for t in trains
    ]
    stale = age_minutes(state.clock(), fetched.fetched_at) if fetched.stale else None
    return TrainsView(rows, stale)


@router.get("/trajet", response_class=HTMLResponse, dependencies=[Depends(rate_limit)])
async def trajet(request: Request, de: str = "", vers: str = "", heure: str = ""):
    when = parse_when(heure, request.app.state.clock().astimezone(PARIS))
    view = await build_trajet(request, de, vers, when)
    heure_param = heure if _HHMM.fullmatch(heure) else ""  # blank: the refreshed fragment follows the clock
    return render(
        request, "trajet.html", "trajet", de=de, vers=vers, heure=hhmm(when), heure_param=heure_param, view=view
    )


@router.get("/fragments/trajet", response_class=HTMLResponse, dependencies=[Depends(refresh_rate_limit)])
async def fragment_trajet(request: Request, de: str = "", vers: str = "", heure: str = ""):
    when = parse_when(heure, request.app.state.clock().astimezone(PARIS))
    return fragment(request, await build_trajet(request, de, vers, when))


# --- gare ----------------------------------------------------------------------------------------


async def build_gare(request: Request, stop_id: str, vers: str) -> tuple[TrainsView | None, list[str]]:
    """The departure board of a station, and the termini found in the answer (for the filter links)."""
    if not stop_id:
        return None, []
    if get_station(stop_id) is None:
        return TrainsView(notice="Gare inconnue."), []
    state = request.app.state
    fetched: Fetched[list[TrainInfo]] = await state.source.departures(stop_id, GARE_DEPARTURES)
    directions = list(dict.fromkeys(t.direction for t in fetched.items if t.direction))
    # A stale board may list trains that already left: keep those still due (expected departure from now on).
    now = state.clock()
    rows = [
        train_row(t, title=f"Direction {t.direction}", meta=f"Train {t.train_number}")
        for t in fetched.items
        if t.departure >= now and (not vers or t.direction == vers)
    ]
    stale = age_minutes(state.clock(), fetched.fetched_at) if fetched.stale else None
    return TrainsView(rows, stale, empty="Aucun départ trouvé."), directions


@router.get("/gare", response_class=HTMLResponse, dependencies=[Depends(rate_limit)])
async def gare(request: Request, id: str = "", vers: str = ""):
    view, directions = await build_gare(request, id, vers)
    return render(request, "gare.html", "gare", stop_id=id, vers=vers, view=view, directions=directions)


@router.get("/fragments/gare", response_class=HTMLResponse, dependencies=[Depends(refresh_rate_limit)])
async def fragment_gare(request: Request, id: str = "", vers: str = ""):
    view, _ = await build_gare(request, id, vers)
    return fragment(request, view)


# --- home ----------------------------------------------------------------------------------------


class _StaleRecorder:
    """Wraps the source and remembers the oldest stale answer it served (the home page shows its age)."""

    def __init__(self, source) -> None:
        self._source = source
        self.since: datetime | None = None

    async def journeys(self, *args, **kwargs):
        fetched = await self._source.journeys(*args, **kwargs)
        if fetched.stale and (self.since is None or fetched.fetched_at < self.since):
            self.since = fetched.fetched_at
        return fetched


async def build_today(request: Request, user: dict) -> TrainsView:
    state = request.app.state
    recorder = _StaleRecorder(state.source)
    now = state.clock()
    items = await today_statuses(state.repo, recorder, user["discord_id"], now)
    stale = age_minutes(now, recorder.since) if recorder.since is not None else None
    return TrainsView([today_row(i) for i in items], stale, empty="Aucun train suivi aujourd'hui.")


def install_url(request: Request) -> str | None:
    client_id = request.app.state.settings.discord_client_id
    if not client_id:
        return None
    return with_query(
        "https://discord.com/oauth2/authorize", client_id=client_id, integration_type="1", scope="applications.commands"
    )


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(refresh_rate_limit)])
async def home(request: Request):
    user = current_user(request)
    if user is None:
        return redirect("/trajet")
    follows = request.app.state.repo.list_follows(user["discord_id"])
    first = follows[0] if follows else None
    return render(
        request,
        "accueil.html",
        "accueil",
        view=await build_today(request, user),
        follows=follows,
        days={f.id: format_days(f.days) for f in follows},
        flash=request.session.pop("flash", None),
        de=first.from_id if first else "",
        vers=first.to_id if first else "",
        heure=hhmm(request.app.state.clock()),
        install_url=install_url(request),
    )


@router.get("/fragments/mes-trains", response_class=HTMLResponse, dependencies=[Depends(refresh_rate_limit)])
async def fragment_mes_trains(request: Request):
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401)
    return fragment(request, await build_today(request, user))


# --- following trains ----------------------------------------------------------------------------


def parse_jours(jours: list[str]) -> int:
    """Day mask of the form's `jours` values: one preset (semaine, tous, week-end) or day names."""
    mask = 0
    for token in jours:
        try:
            mask |= parse_days(token)
        except ValueError:
            raise FollowError("Jours invalides.") from None
    if not mask:
        raise FollowError("Choisis au moins un jour.")
    return mask


def follow_form(request: Request, *, status: int = 200, error: str | None = None, **values) -> HTMLResponse:
    return render(request, "suivre.html", "accueil", status, error=error, presets=DAY_PRESETS, **values)


@router.get("/suivre", response_class=HTMLResponse, dependencies=[Depends(require_user)])
async def suivre(request: Request, de: str = "", vers: str = "", heure: str = ""):
    return follow_form(request, de=de, vers=vers, heure=heure, checked=list(DAY_NAMES[:5]))


@router.post("/suivis", response_class=HTMLResponse, dependencies=[Depends(rate_limit)])
async def create_follow(
    request: Request,
    de: str = Form(""),
    vers: str = Form(""),
    heure: str = Form(""),
    jours: list[str] = Form(default_factory=list),
):
    user = require_user(request)
    state = request.app.state
    days = 0
    try:
        days = parse_jours(jours)
        follow = await add_follow(state.repo, state.source, user["discord_id"], de, vers, heure, days, state.clock())
    except FollowError as exc:
        checked = [name for i, name in enumerate(DAY_NAMES) if days & (1 << i)] or jours
        return follow_form(request, status=400, error=str(exc), de=de, vers=vers, heure=heure, checked=checked)
    request.session["flash"] = (
        f"Enregistré : {follow.dep_time} (train {follow.train_number}), {format_days(follow.days)}"
    )
    return redirect("/")


@router.post("/suivis/{follow_id}/supprimer")
async def delete_follow(request: Request, follow_id: int):
    user = require_user(request)
    removed = request.app.state.repo.delete_follow(user["discord_id"], follow_id)
    request.session["flash"] = "Train retiré." if removed else "Train introuvable."
    return redirect("/")


@router.post("/suivis/{follow_id}/remplacer", dependencies=[Depends(rate_limit)])
async def replace_follow(request: Request, follow_id: int, heure: str = Form("")):
    user = require_user(request)
    state = request.app.state
    try:
        follow = await replace_follow_time(state.repo, state.source, user["discord_id"], follow_id, heure, state.clock())
    except FollowError as exc:
        request.session["flash"] = str(exc)
    else:
        request.session["flash"] = f"Remplacé : {follow.dep_time} (train {follow.train_number})"
    return redirect("/")


@router.get("/healthz")
async def healthz():
    return {"ok": True}
