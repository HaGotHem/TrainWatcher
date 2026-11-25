"""Pure handlers of the Discord commands (no discord.py): each returns the French reply text."""

import logging
import re
from datetime import datetime, time, timedelta
from typing import NamedTuple

from monatrain.display import hhmm as _hhmm
from monatrain.display import station_name, status_label, today_label
from monatrain.follow import FollowError, NoTrainFoundError, add_follow, replace_follow_time, today_statuses
from monatrain.models import TrainInfo
from monatrain.ratelimit import SlidingWindowLimiter
from monatrain.repo import Follow, Repo
from monatrain.sncf.client import SncfError
from monatrain.stations import get_station, search_stations
from monatrain.timeutil import PARIS, format_days, parse_days

log = logging.getLogger(__name__)

MSG_RATE = "Doucement 🙂 réessaie dans quelques minutes."
MSG_SNCF_DOWN = "Horaires SNCF momentanément indisponibles."
MSG_NO_TRAIN = "Tu ne suis aucun train. Utilise /suivre."
MSG_NOT_YOURS = "Train introuvable dans ta liste."
MSG_UNKNOWN_STATION = "Gare inconnue."

MAX_CHOICES = 25
DEPARTS_SHOWN = 8
_BOARD_COUNT = 20  # same query as the web departure board, so that both share the cache
_TYPED_HOUR = re.compile(r"(\d{1,2})(?::\d{0,2})?")

type Choices = list[tuple[str, str]]


def dm_warning(install_url: str) -> str:
    return f"⚠️ Je n'arrive pas à t'envoyer de DM : installe l'app sur ton compte : {install_url}"


# --- /suivre -------------------------------------------------------------------------------------


async def handle_suivre(
    repo: Repo,
    source,
    limiter: SlidingWindowLimiter,
    user_id: str,
    username: str,
    depart_id: str,
    arrivee_id: str,
    heure: str,
    jours: str,
    jours_perso: str | None,
    now: datetime,
    *,
    install_url: str,
) -> str:
    if not limiter.hit(user_id):
        return MSG_RATE
    try:
        days = parse_days((jours_perso or "") if jours == "perso" else jours)
    except ValueError as exc:
        return str(exc)

    existing = repo.get_user(user_id)
    repo.upsert_user(user_id, username, existing["avatar"] if existing else None)
    try:
        follow = await add_follow(repo, source, user_id, depart_id, arrivee_id, heure, days, now)
    except FollowError as exc:
        return str(exc)
    except SncfError as exc:
        log.warning("Suivi impossible, API SNCF indisponible : %s", exc)
        return MSG_SNCF_DOWN

    reply = (
        f"✅ Je surveille le {follow.dep_time} {station_name(follow.from_id)} → {station_name(follow.to_id)} "
        f"(train {follow.train_number}), {format_days(follow.days)}."
    )
    user = repo.get_user(user_id)
    if user is not None and not user["dm_ok"]:
        reply += "\n" + dm_warning(install_url)
    return reply


class ReplaceResult(NamedTuple):
    final: bool  # True: definitive answer (the buttons go away); False: the user may click again later
    text: str


async def handle_remplacer(
    repo: Repo, source, limiter: SlidingWindowLimiter, user_id: str, follow_id: int, hhmm: str, now: datetime
) -> ReplaceResult:
    """The "Suivre HH:MM" button of a suggestion: follow that time instead of the missing train."""
    if not limiter.hit(user_id):
        return ReplaceResult(False, MSG_RATE)
    try:
        follow = await replace_follow_time(repo, source, user_id, follow_id, hhmm, now)
    except NoTrainFoundError as exc:  # maybe later: keep the buttons
        return ReplaceResult(False, str(exc))
    except FollowError as exc:  # not theirs, duplicate, ...: nothing more to try
        return ReplaceResult(True, str(exc))
    except SncfError as exc:
        log.warning("Remplacement impossible, API SNCF indisponible : %s", exc)
        return ReplaceResult(False, MSG_SNCF_DOWN)
    return ReplaceResult(True, f"✅ C'est noté, je surveille maintenant le {follow.dep_time}.")


# --- /mes-trains ---------------------------------------------------------------------------------


async def handle_mes_trains(
    repo: Repo, source, limiter: SlidingWindowLimiter, user_id: str, now: datetime
) -> str:
    if not limiter.hit(user_id):
        return MSG_RATE
    follows = repo.list_follows(user_id)
    if not follows:
        return MSG_NO_TRAIN

    badges = {item.follow.id: today_label(item) for item in await today_statuses(repo, source, user_id, now)}
    lines = [
        f"{f.dep_time} {station_name(f.from_id)} → {station_name(f.to_id)} · {format_days(f.days)} · "
        f"{badges.get(f.id, "Pas aujourd'hui")}"
        for f in sorted(follows, key=lambda f: (f.dep_time, f.id))
    ]
    return "\n".join(lines)


# --- /retirer ------------------------------------------------------------------------------------


def handle_retirer(repo: Repo, user_id: str, follow_id: int) -> str:
    follow = next((f for f in repo.list_follows(user_id) if f.id == follow_id), None)
    if follow is None or not repo.delete_follow(user_id, follow_id):
        return MSG_NOT_YOURS
    return f"✅ C'est retiré : {follow.dep_time} {station_name(follow.from_id)} → {station_name(follow.to_id)}."


# --- /departs ------------------------------------------------------------------------------------


async def handle_departs(
    repo: Repo, source, limiter: SlidingWindowLimiter, gare_id: str, vers_id: str | None, now: datetime
) -> str:
    # `repo` and `limiter` are part of the handler signature, but this handler has no user: the client
    # applies the per-user limit before calling it.
    if get_station(gare_id) is None or (vers_id is not None and get_station(vers_id) is None):
        return MSG_UNKNOWN_STATION
    if vers_id == gare_id:
        return "Les gares de départ et d'arrivée doivent être différentes."

    try:
        if vers_id is None:
            fetched = await source.departures(gare_id, _BOARD_COUNT)
        else:
            fetched = await source.journeys(gare_id, vers_id, now, DEPARTS_SHOWN)
    except SncfError as exc:
        log.warning("Départs indisponibles pour %s : %s", gare_id, exc)
        return MSG_SNCF_DOWN

    # A stale answer may hold trains that already left.
    cutoff = now.replace(second=0, microsecond=0)
    trains = [t for t in fetched.items if t.departure >= cutoff][:DEPARTS_SHOWN]
    title = f"Prochains départs de {station_name(gare_id)}" + (f" vers {station_name(vers_id)}" if vers_id else "")
    if not trains:
        return f"{title} : aucun train trouvé."
    return "\n".join([title, *(_depart_line(t, vers_id is not None) for t in trains)])


def _depart_line(train: TrainInfo, with_arrival: bool) -> str:
    if with_arrival and train.arrival:
        where = f"arr. {_hhmm(train.arrival)}"
    else:
        where = f"direction {train.direction}"
    return f"{_hhmm(train.base_departure)} · {where} · train {train.train_number} · {status_label(train)}"


# --- autocompletes -------------------------------------------------------------------------------


def autocomplete_gare(current: str) -> Choices:
    return [(station.name, station.id) for station in search_stations(current, MAX_CHOICES)]


def autocomplete_mes_trains(repo: Repo, user_id: str) -> Choices:
    return [_follow_choice(f) for f in repo.list_follows(user_id)][:MAX_CHOICES]


def _follow_choice(follow: Follow) -> tuple[str, str]:
    label = f"{follow.dep_time} {station_name(follow.from_id)} → {station_name(follow.to_id)}"
    return label[:100], str(follow.id)


def autocomplete_needs_api(depart_id: str | None, arrivee_id: str | None, current: str) -> bool:
    """True when `autocomplete_heure` would query the SNCF API: two known, different stations and a usable time."""
    if not depart_id or not arrivee_id or depart_id == arrivee_id:
        return False
    if get_station(depart_id) is None or get_station(arrivee_id) is None:
        return False
    return _typed_hour(current)[0]


def _typed_hour(current: str) -> tuple[bool, int | None]:
    """(usable, hour) for the time typed so far (H, HH, HH:, HH:M, HH:MM); hour is None when nothing is typed."""
    text = current.strip()
    if not text:
        return True, None
    typed = _TYPED_HOUR.fullmatch(text)
    if typed is None or int(typed.group(1)) > 23:
        return False, None
    return True, int(typed.group(1))


async def autocomplete_heure(
    source, depart_id: str | None, arrivee_id: str | None, current: str, now: datetime
) -> Choices:
    if not autocomplete_needs_api(depart_id, arrivee_id, current):
        return []

    now = now.astimezone(PARIS)
    _, hour = _typed_hour(current)
    start = now if hour is None else _next_occurrence(hour, now)

    try:
        fetched = await source.journeys(depart_id, arrivee_id, start)
    except SncfError as exc:
        log.warning("Autocomplétion des heures impossible : %s", exc)
        return []

    # A stale answer may hold trains of another day: keep those of `start`'s day, from `start` on.
    trains = [
        t
        for t in fetched.items
        if t.base_departure >= start and t.base_departure.astimezone(PARIS).date() == start.date()
    ]
    return [(_heure_label(t), _hhmm(t.base_departure)) for t in trains][:MAX_CHOICES]


def _next_occurrence(hour: int, now: datetime) -> datetime:
    """HH:00 today, or tomorrow when that moment is already over."""
    start = datetime.combine(now.date(), time(hour), tzinfo=PARIS)
    if start < now:
        start = datetime.combine(now.date() + timedelta(days=1), time(hour), tzinfo=PARIS)
    return start


def _heure_label(train: TrainInfo) -> str:
    label = f"{_hhmm(train.base_departure)} · train {train.train_number}"
    return f"{label} · arr. {_hhmm(train.arrival)}" if train.arrival else label
