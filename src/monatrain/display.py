"""Display helpers shared by the web site and the Discord bot."""

from datetime import datetime

from monatrain.follow import TodayItem
from monatrain.models import Status, TrainInfo
from monatrain.stations import get_station
from monatrain.timeutil import PARIS


def station_name(stop_id: str | None) -> str:
    station = get_station(stop_id) if stop_id else None
    return station.name if station else (stop_id or "?")


def hhmm(moment: datetime) -> str:
    return moment.astimezone(PARIS).strftime("%H:%M")


def status_label(train: TrainInfo) -> str:
    if train.status is Status.CANCELLED:
        return "Supprimé"
    if train.status is Status.REPLACED_BY_BUS:
        return "Remplacé par car"
    if train.status is Status.DELAYED:
        return f"+{train.delay_min} min"
    return "À l'heure"


def today_label(item: TodayItem) -> str:
    """The badge of a followed train today: its status when known, else where it stands."""
    if item.train is not None:
        return status_label(item.train)
    if item.state == "past":
        return "Parti"
    if item.state == "missing":
        return "Introuvable"
    if item.state == "live":
        return "Statut inconnu"
    return "Prévu"
