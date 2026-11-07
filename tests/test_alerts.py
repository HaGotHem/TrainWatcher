"""Task 4: pure alert decision `decide(state, train)`.

Rules (brief + global constraints):
- CANCELLED, last notified status not "cancelled" -> CANCELLED;
- REPLACED_BY_BUS, last notified status neither "replaced_by_bus" nor "cancelled" -> REPLACED_BY_BUS;
- delay_min >= 5 and (nothing notified, or last DM was "delayed" with delay_min >= notified_delay + 5)
  -> DELAYED;
- otherwise None. After a "cancelled" or "replaced_by_bus" DM, a delay never triggers a DM.
"""

from datetime import datetime, timedelta

import pytest

from monatrain.alerts import AlertKind, decide
from monatrain.models import Status, TrainInfo
from monatrain.repo import DailyState
from monatrain.timeutil import PARIS

NICE = "stop_area:SNCF:87756056"
MONACO = "stop_area:SNCF:87756403"
BASE_DEP = datetime(2026, 11, 9, 7, 42, tzinfo=PARIS)

NOTHING = DailyState(found=True, last_delay=None, notified_status=None, notified_delay=None)


def notified(status: str, delay: int) -> DailyState:
    return DailyState(found=True, last_delay=delay, notified_status=status, notified_delay=delay)


def train(status: Status, delay_min: int = 0, reason: str | None = None) -> TrainInfo:
    dep = BASE_DEP + timedelta(minutes=delay_min)
    return TrainInfo(
        from_id=NICE,
        to_id=MONACO,
        base_departure=BASE_DEP,
        departure=dep,
        base_arrival=BASE_DEP + timedelta(minutes=20),
        arrival=dep + timedelta(minutes=20),
        train_number="86012",
        line="TER",
        direction="Vintimille",
        physical_mode="TER / Intercités",
        status=status,
        delay_min=delay_min,
        reason=reason,
        realtime=True,
    )


def test_alert_kind_values_match_notified_statuses():
    # The kind is what gets stored as notified_status, and decide() compares against those strings.
    assert AlertKind.DELAYED == "delayed"
    assert AlertKind.CANCELLED == "cancelled"
    assert AlertKind.REPLACED_BY_BUS == "replaced_by_bus"
    assert {k.name for k in AlertKind} == {"DELAYED", "CANCELLED", "REPLACED_BY_BUS"}


def test_first_delay_5_alerts():
    assert decide(NOTHING, train(Status.DELAYED, 5)) is AlertKind.DELAYED
    assert decide(NOTHING, train(Status.DELAYED, 25)) is AlertKind.DELAYED


def test_first_delay_with_no_daily_row_alerts():
    # A fresh DailyState (no row yet for that day) behaves like "nothing notified".
    empty = DailyState(found=None, last_delay=None, notified_status=None, notified_delay=None)
    assert decide(empty, train(Status.DELAYED, 5)) is AlertKind.DELAYED


def test_delay_4_no_alert():
    assert decide(NOTHING, train(Status.DELAYED, 4)) is None
    assert decide(NOTHING, train(Status.DELAYED, 1)) is None


def test_delay_grows_by_5_realerts():
    # last_delay (12) differs from notified_delay (8): the threshold is relative to the last DM.
    state = DailyState(found=True, last_delay=12, notified_status="delayed", notified_delay=8)
    assert decide(state, train(Status.DELAYED, 8)) is None
    assert decide(state, train(Status.DELAYED, 12)) is None
    assert decide(state, train(Status.DELAYED, 13)) is AlertKind.DELAYED
    assert decide(state, train(Status.DELAYED, 20)) is AlertKind.DELAYED


def test_delay_decreasing_after_dm_is_silent():
    state = notified("delayed", 13)
    assert decide(state, train(Status.DELAYED, 6)) is None
    assert decide(state, train(Status.DELAYED, 13)) is None


def test_cancel_after_delay_alerts():
    assert decide(notified("delayed", 8), train(Status.CANCELLED)) is AlertKind.CANCELLED
    assert decide(NOTHING, train(Status.CANCELLED)) is AlertKind.CANCELLED


def test_cancel_after_bus_alerts():
    assert decide(notified("replaced_by_bus", 0), train(Status.CANCELLED)) is AlertKind.CANCELLED


def test_cancel_once_only():
    assert decide(notified("cancelled", 0), train(Status.CANCELLED)) is None


@pytest.mark.parametrize("last", ["cancelled", "replaced_by_bus"])
def test_delay_after_cancel_silent(last):
    state = notified(last, 0)
    assert decide(state, train(Status.DELAYED, 5)) is None
    assert decide(state, train(Status.DELAYED, 30)) is None


def test_bus_once():
    assert decide(NOTHING, train(Status.REPLACED_BY_BUS)) is AlertKind.REPLACED_BY_BUS
    assert decide(notified("delayed", 8), train(Status.REPLACED_BY_BUS)) is AlertKind.REPLACED_BY_BUS
    assert decide(notified("replaced_by_bus", 0), train(Status.REPLACED_BY_BUS)) is None


def test_bus_after_cancel_silent():
    assert decide(notified("cancelled", 0), train(Status.REPLACED_BY_BUS)) is None


@pytest.mark.parametrize(
    "state",
    [
        NOTHING,
        DailyState(found=None, last_delay=None, notified_status=None, notified_delay=None),
        notified("delayed", 8),
        notified("cancelled", 0),
        notified("replaced_by_bus", 0),
    ],
)
def test_on_time_never_alerts(state):
    assert decide(state, train(Status.ON_TIME, 0)) is None
