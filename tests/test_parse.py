"""Task 2: models and parsing of SNCF (Navitia) /journeys and /departures responses.

Fixtures:
- journeys_monaco_nice.json, departures_monaco.json, departures_nice.json: real responses captured
  by scripts/capture_fixtures.py on 2026-11-07 (night, so all base_schedule, no disruption).
- journey_*.json, departure_base_schedule.json: minimal variants hand-derived from the real files.
"""

import copy
import dataclasses
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from monatrain.models import Fetched, Status, TrainInfo
from monatrain.sncf.parse import parse_departures, parse_journeys
from monatrain.timeutil import PARIS

FIXTURES = Path(__file__).parent / "fixtures"
MONACO = "stop_area:SNCF:87756403"
NICE = "stop_area:SNCF:87756056"
MONACO_SP = "stop_point:SNCF:87756403:Train"
VILLEFRANCHE_SP = "stop_point:SNCF:87756361:Train"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _pt_section(payload: dict) -> dict:
    return next(s for s in payload["journeys"][0]["sections"] if s["type"] == "public_transport")


def _plus(navitia_dt: str, minutes: int) -> str:
    dt = datetime.strptime(navitia_dt, "%Y%m%dT%H%M%S") + timedelta(minutes=minutes)
    return dt.strftime("%Y%m%dT%H%M%S")


# --- models ------------------------------------------------------------------------------------


def test_status_values():
    assert Status.ON_TIME == "on_time"
    assert Status.DELAYED == "delayed"
    assert Status.CANCELLED == "cancelled"
    assert Status.REPLACED_BY_BUS == "replaced_by_bus"
    assert isinstance(Status.DELAYED, str)


def test_train_info_is_frozen():
    t = parse_journeys(load("journey_delayed.json"), MONACO, NICE)[0]
    assert isinstance(t, TrainInfo)
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.delay_min = 0


def test_fetched_holds_items_and_is_frozen():
    at = datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)
    f = Fetched[list[TrainInfo]](items=[], fetched_at=at, stale=True)
    assert f.items == [] and f.fetched_at == at and f.stale is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.stale = False


# --- brief tests -------------------------------------------------------------------------------


def test_delayed_journey():
    t = parse_journeys(load("journey_delayed.json"), MONACO, NICE)[0]
    assert t.status is Status.DELAYED and t.delay_min == 8 and t.reason == "Réutilisation d'un train"
    assert t.base_departure.strftime("%H:%M") == "08:04" and t.train_number == "86053"


def test_delayed_journey_all_fields():
    t = parse_journeys(load("journey_delayed.json"), MONACO, NICE)[0]
    assert t.from_id == MONACO and t.to_id == NICE
    assert t.base_departure == datetime(2026, 11, 6, 8, 4, tzinfo=PARIS)
    assert t.departure == datetime(2026, 11, 6, 8, 12, tzinfo=PARIS)
    assert t.base_arrival == datetime(2026, 11, 6, 8, 28, tzinfo=PARIS)
    assert t.arrival == datetime(2026, 11, 6, 8, 36, tzinfo=PARIS)
    assert t.base_departure.utcoffset() == timedelta(hours=1)  # aware, Europe/Paris (CET)
    assert t.line == "C3"
    assert t.direction == "Grasse (Grasse)"
    assert t.physical_mode == "TER / Intercités"
    assert t.realtime is True


def test_no_service_without_realtime_times():
    t = parse_journeys(load("journey_no_service.json"), MONACO, NICE)[0]
    assert t.status is Status.CANCELLED and t.departure == t.base_departure and t.delay_min == 0


def test_stop_deleted_at_users_arrival_is_cancelled():
    assert parse_journeys(load("journey_stop_deleted_at_arrival.json"), MONACO, NICE)[0].status is Status.CANCELLED


def test_stop_deleted_elsewhere_is_not_cancelled():
    t = parse_journeys(load("journey_stop_deleted_elsewhere.json"), MONACO, NICE)[0]
    assert t.status is not Status.CANCELLED
    assert t.status is Status.ON_TIME and t.delay_min == 0
    assert t.reason == "Incident d'exploitation"


def test_bus_replacement():
    assert parse_journeys(load("journey_bus.json"), MONACO, NICE)[0].status is Status.REPLACED_BY_BUS


def test_base_schedule_is_on_time():
    t = parse_departures(load("departure_base_schedule.json"), MONACO)[0]
    assert t.status is Status.ON_TIME and t.realtime is False and t.to_id is None and t.direction


def test_real_fixtures_parse_without_error():
    assert parse_journeys(load("journeys_monaco_nice.json"), MONACO, NICE)
    assert parse_departures(load("departures_nice.json"), NICE)


# --- classification rules not covered by a dedicated fixture ----------------------------------


def test_linked_disruption_with_no_service_effect_is_cancelled():
    payload = load("journey_delayed.json")
    payload["disruptions"][0]["severity"]["effect"] = "NO_SERVICE"
    t = parse_journeys(payload, MONACO, NICE)[0]
    assert t.status is Status.CANCELLED
    assert t.reason == "Réutilisation d'un train"


def test_stop_deleted_at_users_departure_is_cancelled():
    payload = load("journey_stop_deleted_elsewhere.json")
    for stop in payload["disruptions"][0]["impacted_objects"][0]["impacted_stops"]:
        if stop["stop_point"]["id"] == VILLEFRANCHE_SP:
            stop["stop_time_effect"] = "unchanged"
        elif stop["stop_point"]["id"] == MONACO_SP:
            stop["stop_time_effect"] = "deleted"
    assert parse_journeys(payload, MONACO, NICE)[0].status is Status.CANCELLED


def test_unlinked_disruption_is_ignored():
    payload = load("journey_stop_deleted_at_arrival.json")
    info = _pt_section(payload)["display_informations"]
    info["links"] = [link for link in info["links"] if link.get("rel") != "disruptions"]
    t = parse_journeys(payload, MONACO, NICE)[0]
    assert t.status is Status.ON_TIME
    assert t.reason is None


def test_cancelled_takes_priority_over_bus():
    payload = load("journey_no_service.json")
    _pt_section(payload)["display_informations"]["physical_mode"] = "Autocar"
    assert parse_journeys(payload, MONACO, NICE)[0].status is Status.CANCELLED


def test_bus_takes_priority_over_delay():
    payload = load("journey_bus.json")
    section = _pt_section(payload)
    section["departure_date_time"] = _plus(section["base_departure_date_time"], 10)
    t = parse_journeys(payload, MONACO, NICE)[0]
    assert t.status is Status.REPLACED_BY_BUS
    assert t.delay_min == 10


@pytest.mark.parametrize("mode", ["Bus", "bus de substitution", "AUTOCAR"])
def test_bus_detection_is_case_insensitive(mode):
    payload = load("journey_bus.json")
    _pt_section(payload)["display_informations"]["physical_mode"] = mode
    assert parse_journeys(payload, MONACO, NICE)[0].status is Status.REPLACED_BY_BUS


def test_delayed_departure():
    payload = load("departure_base_schedule.json")
    sdt = payload["departures"][0]["stop_date_time"]
    sdt["data_freshness"] = "realtime"
    sdt["departure_date_time"] = _plus(sdt["base_departure_date_time"], 5)
    t = parse_departures(payload, MONACO)[0]
    assert t.status is Status.DELAYED and t.delay_min == 5 and t.realtime is True
    assert t.departure - t.base_departure == timedelta(minutes=5)


# --- real fixtures, field by field -------------------------------------------------------------


def test_real_journeys_fields():
    payload = load("journeys_monaco_nice.json")
    trains = parse_journeys(payload, MONACO, NICE)
    assert len(trains) == len(payload["journeys"]) == 4
    t = trains[0]  # crow_fly sections around the public_transport one must be ignored
    assert t.train_number == "881202"
    assert t.line == "C13"
    assert t.direction == "Les Arcs - Draguignan (Les Arcs-sur-Argens)"
    assert t.physical_mode == "TER / Intercités"
    assert t.from_id == MONACO and t.to_id == NICE
    assert t.base_departure == t.departure == datetime(2026, 11, 7, 6, 4, tzinfo=PARIS)
    assert t.base_arrival == t.arrival == datetime(2026, 11, 7, 6, 27, tzinfo=PARIS)
    assert t.status is Status.ON_TIME and t.delay_min == 0
    assert t.realtime is False and t.reason is None
    assert [x.train_number for x in trains] == ["881202", "86004", "881106", "86006"]


def test_real_departures_fields():
    payload = load("departures_nice.json")
    trains = parse_departures(payload, NICE)
    assert len(trains) == len(payload["departures"]) == 4
    t = trains[0]
    assert t.from_id == NICE and t.to_id is None
    assert t.train_number == "881303"
    assert t.direction == "Breil-sur-Roya (Breil-sur-Roya)"
    assert t.base_departure == t.departure == datetime(2026, 11, 7, 5, 32, tzinfo=PARIS)
    assert t.status is Status.ON_TIME and t.realtime is False and t.reason is None
    assert trains[2].physical_mode == "Train grande vitesse"


# --- ruling R9: realtime = data_freshness != "base_schedule" -------------------------------------


def test_adapted_schedule_journey_is_realtime_delayed():
    # journey_adapted_schedule.json: journey_delayed.json with data_freshness "adapted_schedule"
    # and every realtime time moved to base + 6 min (instead of + 8).
    t = parse_journeys(load("journey_adapted_schedule.json"), MONACO, NICE)[0]
    assert t.realtime is True
    assert t.status is Status.DELAYED and t.delay_min == 6
    assert t.base_departure == datetime(2026, 11, 6, 8, 4, tzinfo=PARIS)
    assert t.departure == datetime(2026, 11, 6, 8, 10, tzinfo=PARIS)
    assert t.arrival == datetime(2026, 11, 6, 8, 34, tzinfo=PARIS)


def test_adapted_schedule_departure_is_realtime_delayed():
    payload = load("departure_base_schedule.json")
    sdt = payload["departures"][0]["stop_date_time"]
    sdt["data_freshness"] = "adapted_schedule"
    sdt["departure_date_time"] = _plus(sdt["base_departure_date_time"], 6)
    t = parse_departures(payload, MONACO)[0]
    assert t.realtime is True and t.status is Status.DELAYED and t.delay_min == 6


def test_base_schedule_journey_with_shifted_departure_is_on_time():
    payload = load("journey_delayed.json")
    section = _pt_section(payload)
    section["data_freshness"] = "base_schedule"
    section["departure_date_time"] = _plus(section["base_departure_date_time"], 6)
    t = parse_journeys(payload, MONACO, NICE)[0]
    assert t.realtime is False
    assert t.status is Status.ON_TIME
    assert t.delay_min == 6
