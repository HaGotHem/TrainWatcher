from datetime import date, datetime, time
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
NAVITIA_FORMAT = "%Y%m%dT%H%M%S"

# Day masks: bit 0 = lundi (date.weekday() == 0), bit 6 = dimanche.
DAY_NAMES = ("lun", "mar", "mer", "jeu", "ven", "sam", "dim")
WEEKDAYS = 31
ALL_DAYS = 127
WEEKEND = 96

_PRESETS = {
    "semaine": WEEKDAYS,
    "lun-ven": WEEKDAYS,
    "tous": ALL_DAYS,
    "tous les jours": ALL_DAYS,
    "week-end": WEEKEND,
    "weekend": WEEKEND,
}
_PRESET_LABELS = {WEEKDAYS: "lun-ven", ALL_DAYS: "tous les jours", WEEKEND: "week-end"}


def now_paris() -> datetime:
    return datetime.now(PARIS)


def parse_navitia_dt(s: str) -> datetime:
    """Parse a Navitia local timestamp (YYYYMMDDTHHMMSS, Paris time) into an aware datetime."""
    return datetime.strptime(s, NAVITIA_FORMAT).replace(tzinfo=PARIS)


def format_navitia_dt(dt: datetime) -> str:
    """Format an aware datetime as a Navitia timestamp in Paris local time."""
    return dt.astimezone(PARIS).strftime(NAVITIA_FORMAT)


def day_bit(d: date) -> int:
    return 1 << d.weekday()


def runs_on(mask: int, d: date) -> bool:
    return bool(mask & day_bit(d))


def parse_days(text: str) -> int:
    """Parse a preset (semaine, tous, week-end) or a comma-separated list (lun,mer,ven) into a mask."""
    key = " ".join(text.lower().split())
    if key in _PRESETS:
        return _PRESETS[key]

    mask = 0
    for token in text.split(","):
        name = token.strip().lower()
        if name not in DAY_NAMES:
            raise ValueError(
                f"Jour inconnu : {token.strip()!r}. Utilisez semaine, tous, week-end "
                "ou une liste de jours comme lun,mer,ven."
            )
        mask |= 1 << DAY_NAMES.index(name)
    return mask


def format_days(mask: int) -> str:
    if mask in _PRESET_LABELS:
        return _PRESET_LABELS[mask]
    return ", ".join(name for bit, name in enumerate(DAY_NAMES) if mask & (1 << bit))


def next_service_date(mask: int, hhmm: str, now: datetime) -> date:
    """Return the first date, from today, on which a train at hhmm (Paris time) runs and is still ahead of now."""
    local_now = now.astimezone(PARIS)
    departure = time.fromisoformat(hhmm)
    today = local_now.date()

    for offset in range(8):
        candidate = date.fromordinal(today.toordinal() + offset)
        if not runs_on(mask, candidate):
            continue
        if offset == 0 and datetime.combine(candidate, departure, tzinfo=PARIS) < local_now:
            continue
        return candidate

    raise ValueError("Aucun jour de circulation dans le masque")
