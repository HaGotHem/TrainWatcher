"""French DM texts for alerts and for followed trains that have disappeared."""

from datetime import datetime

from monatrain.alerts import AlertKind
from monatrain.models import TrainInfo
from monatrain.repo import Follow
from monatrain.stations import get_station
from monatrain.timeutil import PARIS


def format_alert(kind: AlertKind, train: TrainInfo) -> str:
    header = f"{_title(kind)} · {_station_name(train.from_id)} → {_station_name(train.to_id)}"
    if kind is AlertKind.DELAYED:
        body = (
            f"Train {train.train_number} prévu à {_hhmm(train.base_departure)}, "
            f"départ estimé {_hhmm(train.departure)} (+{train.delay_min} min)."
        )
    elif kind is AlertKind.CANCELLED:
        body = f"Le train {train.train_number} de {_hhmm(train.base_departure)} est supprimé."
    else:
        body = f"Le train {train.train_number} de {_hhmm(train.base_departure)} est remplacé par un car."

    lines = [header, body]
    if train.reason:
        lines.append(f"Motif : {train.reason}")
    return "\n".join(lines)


def format_missing(follow: Follow, suggestion: TrainInfo | None) -> str:
    route = f"{_station_name(follow.from_id)} → {_station_name(follow.to_id)}"
    first = f"❓ Ton train {route} de {follow.dep_time} est introuvable depuis 2 jours"
    if suggestion is None:
        return f"{first}, et aucun train direct n'existe à ± 15 min. Tu peux le retirer avec /retirer."
    return (
        f"{first} (horaires modifiés ?).\n"
        f"Le plus proche : {_hhmm(suggestion.base_departure)} (train {suggestion.train_number}). "
        "Tu veux le suivre à la place ?"
    )


def _title(kind: AlertKind) -> str:
    if kind is AlertKind.DELAYED:
        return "⚠️ Retard"
    if kind is AlertKind.CANCELLED:
        return "❌ Supprimé"
    return "🚌 Remplacé par car"


def _station_name(stop_id: str | None) -> str:
    if stop_id is None:
        return "?"
    station = get_station(stop_id)
    return station.name if station is not None else stop_id


def _hhmm(moment: datetime) -> str:
    return moment.astimezone(PARIS).strftime("%H:%M")
