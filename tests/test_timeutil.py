from datetime import date, datetime, timedelta, timezone

import pytest

from monatrain.timeutil import (
    ALL_DAYS,
    PARIS,
    WEEKDAYS,
    WEEKEND,
    day_bit,
    format_days,
    format_navitia_dt,
    next_service_date,
    now_paris,
    parse_days,
    parse_navitia_dt,
    runs_on,
)

# Calendar anchor: 2026-11-06 is a Friday.
MON = date(2026, 11, 9)
TUE = date(2026, 11, 10)
WED = date(2026, 11, 11)
THU = date(2026, 11, 12)
FRI = date(2026, 11, 6)
SAT = date(2026, 11, 7)
SUN = date(2026, 11, 8)


# --- Brief tests -----------------------------------------------------------

def test_navitia_dt_roundtrip_is_paris_aware():
    dt = parse_navitia_dt("20261106T164600"); assert dt.tzinfo is not None and dt.utcoffset() == timedelta(hours=1)
    assert format_navitia_dt(dt) == "20261106T164600"


def test_days_parse_and_format():
    assert parse_days("semaine") == 31 and parse_days("lun,mer,ven") == 1|4|16
    assert format_days(31) == "lun-ven" and format_days(127) == "tous les jours" and format_days(96) == "week-end"
    assert format_days(1|4|16) == "lun, mer, ven"
    with pytest.raises(ValueError): parse_days("lundi-bof")


def test_next_service_date():  # vendredi 2026-11-06 à 09:00, train 08:04 en semaine → lundi 09
    now = datetime(2026, 11, 6, 9, 0, tzinfo=PARIS)
    assert next_service_date(31, "08:04", now) == date(2026, 11, 9)
    assert next_service_date(31, "18:12", now) == date(2026, 11, 6)


# --- Additional pinning tests ----------------------------------------------

def test_paris_zone():
    assert PARIS.key == "Europe/Paris"


def test_now_paris_is_aware_paris_and_current():
    now = now_paris()
    assert now.tzinfo is not None
    assert getattr(now.tzinfo, "key", None) == "Europe/Paris"
    assert abs(now - datetime.now(timezone.utc)) < timedelta(minutes=1)


def test_parse_navitia_dt_fields_and_zone():
    dt = parse_navitia_dt("20261106T164600")
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second) == (2026, 11, 6, 16, 46, 0)
    assert getattr(dt.tzinfo, "key", None) == "Europe/Paris"
    assert dt == datetime(2026, 11, 6, 16, 46, tzinfo=PARIS)


def test_parse_navitia_dt_summer_time_offset():
    dt = parse_navitia_dt("20260715T080430")
    assert dt.utcoffset() == timedelta(hours=2)
    assert (dt.hour, dt.minute, dt.second) == (8, 4, 30)
    assert format_navitia_dt(dt) == "20260715T080430"


def test_format_navitia_dt_converts_other_zones_to_paris():
    utc_dt = datetime(2026, 11, 6, 15, 46, tzinfo=timezone.utc)
    assert format_navitia_dt(utc_dt) == "20261106T164600"
    summer_utc = datetime(2026, 7, 15, 6, 4, tzinfo=timezone.utc)
    assert format_navitia_dt(summer_utc) == "20260715T080400"


def test_day_mask_constants():
    assert WEEKDAYS == 31
    assert ALL_DAYS == 127
    assert WEEKEND == 96


def test_day_bit_each_weekday():
    assert day_bit(MON) == 1
    assert day_bit(TUE) == 2
    assert day_bit(WED) == 4
    assert day_bit(THU) == 8
    assert day_bit(FRI) == 16
    assert day_bit(SAT) == 32
    assert day_bit(SUN) == 64


def test_runs_on():
    assert runs_on(WEEKDAYS, FRI) is True
    assert runs_on(WEEKDAYS, SAT) is False
    assert runs_on(WEEKDAYS, SUN) is False
    assert runs_on(WEEKEND, SUN) is True
    assert runs_on(WEEKEND, MON) is False
    assert runs_on(ALL_DAYS, WED) is True
    assert runs_on(1 | 4 | 16, WED) is True
    assert runs_on(1 | 4 | 16, TUE) is False


def test_parse_days_presets():
    assert parse_days("semaine") == WEEKDAYS
    assert parse_days("tous") == ALL_DAYS
    assert parse_days("week-end") == WEEKEND
    assert parse_days("weekend") == WEEKEND


def test_parse_days_each_abbreviation():
    assert parse_days("lun") == 1
    assert parse_days("mar") == 2
    assert parse_days("mer") == 4
    assert parse_days("jeu") == 8
    assert parse_days("ven") == 16
    assert parse_days("sam") == 32
    assert parse_days("dim") == 64
    assert parse_days("lun,mar,mer,jeu,ven,sam,dim") == 127
    assert parse_days("sam,dim") == 96


def test_parse_days_accepts_formatted_list_with_spaces():
    # format_days output for a custom list must parse back to the same mask
    assert parse_days("lun, mer, ven") == 1 | 4 | 16
    assert parse_days(format_days(2 | 8)) == 2 | 8


def test_parse_days_rejects_invalid():
    with pytest.raises(ValueError):
        parse_days("")
    with pytest.raises(ValueError):
        parse_days("lun,xyz")
    with pytest.raises(ValueError):
        parse_days("monday")


def test_format_days_custom_lists_in_week_order():
    assert format_days(1) == "lun"
    assert format_days(64) == "dim"
    assert format_days(2 | 8) == "mar, jeu"
    assert format_days(16 | 32 | 64) == "ven, sam, dim"
    assert format_days(63) == "lun, mar, mer, jeu, ven, sam"


def test_next_service_date_weekend_mask_from_friday():
    now = datetime(2026, 11, 6, 9, 0, tzinfo=PARIS)
    assert next_service_date(WEEKEND, "10:00", now) == SAT
    assert next_service_date(WEEKEND, "08:00", now) == SAT


def test_next_service_date_all_days_time_passed_is_tomorrow():
    now = datetime(2026, 11, 6, 9, 0, tzinfo=PARIS)
    assert next_service_date(ALL_DAYS, "08:59", now) == SAT
    assert next_service_date(ALL_DAYS, "09:01", now) == FRI


def test_next_service_date_single_day_wraps_to_next_week():
    now = datetime(2026, 11, 6, 9, 0, tzinfo=PARIS)
    assert next_service_date(16, "08:00", now) == date(2026, 11, 13)  # next Friday
    assert next_service_date(16, "20:00", now) == FRI


def test_next_service_date_from_saturday_with_weekdays():
    now = datetime(2026, 11, 7, 7, 0, tzinfo=PARIS)
    assert next_service_date(WEEKDAYS, "08:04", now) == MON


def test_next_service_date_crosses_year_boundary():
    now = datetime(2026, 12, 31, 23, 0, tzinfo=PARIS)  # Thursday
    assert next_service_date(ALL_DAYS, "08:00", now) == date(2027, 1, 1)
