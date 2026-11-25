"""Task 9 review (ruling R20): shared display helpers (monatrain.display), used by the web and the bot.

- `station_name(stop_id) -> str`: the station's name, or the raw id when it is not one of the 29.
- `hhmm(dt) -> str`: "HH:MM" in Paris time, whatever the datetime's own zone.
- `status_label(train) -> str`: "À l'heure", "+N min", "Supprimé", "Remplacé par car" (web wording).
- `today_label(item: TodayItem) -> str`: status_label(train) when the train is known; otherwise
  "Parti" (past), "Introuvable" (missing), "Statut inconnu" (live, train None), "Prévu" (upcoming).
"""

from datetime import UTC, datetime, timedelta

import pytest

from monatrain.display import hhmm, station_name, status_label, today_label
from monatrain.follow import TodayItem
from monatrain.models import Status, TrainInfo
from monatrain.repo import Follow
from monatrain.timeutil import PARIS, WEEKDAYS

MONACO = "stop_area:SNCF:87756403"
NICE = "stop_area:SNCF:87756056"
EZE = "stop_area:SNCF:87756387"
UNKNOWN = "stop_area:SNCF:87000000"

BASE = datetime(2026, 11, 6, 8, 4, tzinfo=PARIS)


def train(*, delay: int = 0, status: Status = Status.ON_TIME) -> TrainInfo:
    dep = BASE + timedelta(minutes=delay)
    return TrainInfo(
        from_id=MONACO,
        to_id=NICE,
        base_departure=BASE,
        departure=dep,
        base_arrival=BASE + timedelta(minutes=20),
        arrival=dep + timedelta(minutes=20),
        train_number="86053",
        line="TER",
        direction="Nice-Ville",
        physical_mode="TER / Intercités",
        status=status,
        delay_min=delay,
        reason=None,
        realtime=True,
    )


FOLLOW = Follow(
    id=1,
    user_id="111",
    from_id=MONACO,
    to_id=NICE,
    dep_time="08:04",
    days=WEEKDAYS,
    train_number="86053",
    suggestion_sent=False,
)


def test_station_name():
    assert station_name(MONACO) == "Monaco Monte-Carlo"
    assert station_name(EZE) == "Èze"
    assert station_name(UNKNOWN) == UNKNOWN


def test_hhmm_is_paris_time():
    assert hhmm(BASE) == "08:04"
    assert hhmm(datetime(2026, 11, 6, 7, 4, tzinfo=UTC)) == "08:04"  # UTC+1 in November
    assert hhmm(datetime(2026, 7, 6, 6, 4, tzinfo=UTC)) == "08:04"  # UTC+2 in summer


@pytest.mark.parametrize(
    ("t", "label"),
    [
        (train(), "À l'heure"),
        (train(delay=7, status=Status.DELAYED), "+7 min"),
        (train(status=Status.CANCELLED), "Supprimé"),
        (train(status=Status.REPLACED_BY_BUS), "Remplacé par car"),
    ],
)
def test_status_label(t, label):
    assert status_label(t) == label


@pytest.mark.parametrize(
    ("item", "label"),
    [
        (TodayItem(FOLLOW, "upcoming", None, False), "Prévu"),
        (TodayItem(FOLLOW, "past", None, False), "Parti"),
        (TodayItem(FOLLOW, "missing", None, False), "Introuvable"),
        (TodayItem(FOLLOW, "live", None, True), "Statut inconnu"),
        (TodayItem(FOLLOW, "live", None, False), "Statut inconnu"),
        (TodayItem(FOLLOW, "live", train(delay=7, status=Status.DELAYED), False), "+7 min"),
        (TodayItem(FOLLOW, "live", train(), True), "À l'heure"),
        (TodayItem(FOLLOW, "live", train(status=Status.CANCELLED), False), "Supprimé"),
    ],
)
def test_today_label(item, label):
    assert today_label(item) == label
