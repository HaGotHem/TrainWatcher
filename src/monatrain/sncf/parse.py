"""Parse Navitia /journeys and /departures responses into TrainInfo."""

from datetime import datetime

from monatrain.models import Status, TrainInfo
from monatrain.stations import uic_code
from monatrain.timeutil import parse_navitia_dt

_NO_SERVICE = "NO_SERVICE"
_BUS_WORDS = ("autocar", "bus")


def parse_journeys(payload: dict, from_id: str, to_id: str) -> list[TrainInfo]:
    disruptions = _disruptions_by_id(payload)
    trains = []
    for journey in payload.get("journeys", []):
        section = next((s for s in journey.get("sections", []) if s.get("type") == "public_transport"), None)
        if section is None:
            continue
        trains.append(
            _build(
                section,
                section.get("display_informations", {}),
                disruptions,
                from_id=from_id,
                to_id=to_id,
                user_stops={uic_code(from_id), uic_code(to_id)},
                no_service=journey.get("status") == _NO_SERVICE,
                with_arrival=True,
            )
        )
    return trains


def parse_departures(payload: dict, stop_id: str) -> list[TrainInfo]:
    disruptions = _disruptions_by_id(payload)
    return [
        _build(
            departure.get("stop_date_time", {}),
            departure.get("display_informations", {}),
            disruptions,
            from_id=stop_id,
            to_id=None,
            user_stops={uic_code(stop_id)},
            no_service=False,
            with_arrival=False,
        )
        for departure in payload.get("departures", [])
    ]


def _build(
    times: dict,
    info: dict,
    disruptions: dict[str, dict],
    *,
    from_id: str,
    to_id: str | None,
    user_stops: set[str],
    no_service: bool,
    with_arrival: bool,
) -> TrainInfo:
    base_departure = _dt(times.get("base_departure_date_time") or times.get("departure_date_time"))
    departure = _dt(times.get("departure_date_time")) or base_departure
    if base_departure is None or departure is None:
        raise ValueError("Horaire de départ absent de la réponse SNCF")

    base_arrival = arrival = None
    if with_arrival:
        base_arrival = _dt(times.get("base_arrival_date_time") or times.get("arrival_date_time"))
        arrival = _dt(times.get("arrival_date_time")) or base_arrival

    linked = _linked_disruptions(info, disruptions)
    delay_min = int((departure - base_departure).total_seconds() // 60)
    realtime = times.get("data_freshness") != "base_schedule"
    physical_mode = info.get("physical_mode", "")

    if no_service or any(_cancels(d, user_stops) for d in linked):
        status = Status.CANCELLED
    elif any(word in physical_mode.lower() for word in _BUS_WORDS):
        status = Status.REPLACED_BY_BUS
    elif realtime and delay_min >= 1:
        status = Status.DELAYED
    else:
        status = Status.ON_TIME

    return TrainInfo(
        from_id=from_id,
        to_id=to_id,
        base_departure=base_departure,
        departure=departure,
        base_arrival=base_arrival,
        arrival=arrival,
        train_number=info.get("trip_short_name") or info.get("headsign", ""),
        line=info.get("code") or info.get("label", ""),
        direction=info.get("direction", ""),
        physical_mode=physical_mode,
        status=status,
        delay_min=delay_min,
        reason=_reason(linked),
        realtime=realtime,
    )


def _dt(value: str | None) -> datetime | None:
    return parse_navitia_dt(value) if value else None


def _disruptions_by_id(payload: dict) -> dict[str, dict]:
    return {d["id"]: d for d in payload.get("disruptions", []) if "id" in d}


def _linked_disruptions(info: dict, disruptions: dict[str, dict]) -> list[dict]:
    ids = [link.get("id") for link in info.get("links", []) if link.get("rel") == "disruptions"]
    return [disruptions[i] for i in ids if i in disruptions]


def _cancels(disruption: dict, user_stops: set[str]) -> bool:
    if disruption.get("severity", {}).get("effect") == _NO_SERVICE:
        return True
    for obj in disruption.get("impacted_objects", []):
        for stop in obj.get("impacted_stops", []):
            if stop.get("stop_time_effect") == "deleted" and _uic_or_none(stop.get("stop_point", {}).get("id", "")) in user_stops:
                return True
    return False


def _uic_or_none(stop_id: str) -> str | None:
    try:
        return uic_code(stop_id)
    except ValueError:
        return None


def _reason(linked: list[dict]) -> str | None:
    if not linked:
        return None
    messages = linked[0].get("messages") or []
    return messages[0].get("text") if messages else None
