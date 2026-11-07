"""Task 4: French DM texts `format_alert(kind, train)` and `format_missing(follow, suggestion)`.

- {Départ}/{Arrivée}: station names from get_station(train.from_id / train.to_id)
  (format_missing: follow.from_id / follow.to_id).
- {n}: train_number; times: HH:MM in Paris time; {d}: delay_min.
- "Retard": "prévu à" = base_departure, "départ estimé" = departure.
- The "Motif : ..." line is present only when reason is not None.
- format_missing: the first {HH:MM} is follow.dep_time; the suggestion's time and number come
  from the suggested TrainInfo.
"""

from datetime import UTC, datetime, timedelta

from monatrain.alerts import AlertKind
from monatrain.messages import format_alert, format_missing
from monatrain.models import Status, TrainInfo
from monatrain.repo import Follow
from monatrain.timeutil import PARIS

NICE = "stop_area:SNCF:87756056"  # Nice-Ville
MONACO = "stop_area:SNCF:87756403"  # Monaco Monte-Carlo
CANNES = "stop_area:SNCF:87757625"  # Cannes
ANTIBES = "stop_area:SNCF:87757674"  # Antibes
BASE_DEP = datetime(2026, 11, 9, 7, 42, tzinfo=PARIS)


def train(
    status: Status,
    delay_min: int = 0,
    reason: str | None = None,
    *,
    from_id: str = NICE,
    to_id: str = MONACO,
    base_departure: datetime = BASE_DEP,
    train_number: str = "86012",
) -> TrainInfo:
    dep = base_departure + timedelta(minutes=delay_min)
    return TrainInfo(
        from_id=from_id,
        to_id=to_id,
        base_departure=base_departure,
        departure=dep,
        base_arrival=base_departure + timedelta(minutes=20),
        arrival=dep + timedelta(minutes=20),
        train_number=train_number,
        line="TER",
        direction="Vintimille",
        physical_mode="TER / Intercités",
        status=status,
        delay_min=delay_min,
        reason=reason,
        realtime=True,
    )


def follow(dep_time: str = "07:42") -> Follow:
    return Follow(
        id=1,
        user_id="123456789012345678",
        from_id=CANNES,
        to_id=ANTIBES,
        dep_time=dep_time,
        days=31,
        train_number="86101",
        suggestion_sent=False,
    )


# --- format_alert: delay -------------------------------------------------------------------------


def test_format_delay_exact_text():
    text = format_alert(AlertKind.DELAYED, train(Status.DELAYED, 7, "Conditions météorologiques"))
    assert text == (
        "⚠️ Retard · Nice-Ville → Monaco Monte-Carlo\n"
        "Train 86012 prévu à 07:42, départ estimé 07:49 (+7 min).\n"
        "Motif : Conditions météorologiques"
    )


def test_format_without_reason_has_no_motif_line():
    text = format_alert(AlertKind.DELAYED, train(Status.DELAYED, 12))
    assert text == (
        "⚠️ Retard · Nice-Ville → Monaco Monte-Carlo\n"
        "Train 86012 prévu à 07:42, départ estimé 07:54 (+12 min)."
    )
    assert "Motif" not in text


def test_format_delay_uses_paris_time():
    # 06:42 UTC on 2026-11-09 is 07:42 in Paris (CET, UTC+1); estimated 06:51 UTC -> 07:51.
    base_utc = datetime(2026, 11, 9, 6, 42, tzinfo=UTC)
    text = format_alert(AlertKind.DELAYED, train(Status.DELAYED, 9, base_departure=base_utc))
    assert text == (
        "⚠️ Retard · Nice-Ville → Monaco Monte-Carlo\n"
        "Train 86012 prévu à 07:42, départ estimé 07:51 (+9 min)."
    )


def test_format_delay_station_names_follow_train_direction():
    text = format_alert(
        AlertKind.DELAYED, train(Status.DELAYED, 5, from_id=MONACO, to_id=NICE, train_number="86031")
    )
    assert text == (
        "⚠️ Retard · Monaco Monte-Carlo → Nice-Ville\n"
        "Train 86031 prévu à 07:42, départ estimé 07:47 (+5 min)."
    )


# --- format_alert: cancelled ---------------------------------------------------------------------


def test_format_cancelled_exact_text():
    text = format_alert(AlertKind.CANCELLED, train(Status.CANCELLED, reason="Mouvement social"))
    assert text == (
        "❌ Supprimé · Nice-Ville → Monaco Monte-Carlo\n"
        "Le train 86012 de 07:42 est supprimé.\n"
        "Motif : Mouvement social"
    )


def test_format_cancelled_without_reason_has_no_motif_line():
    text = format_alert(AlertKind.CANCELLED, train(Status.CANCELLED))
    assert text == "❌ Supprimé · Nice-Ville → Monaco Monte-Carlo\nLe train 86012 de 07:42 est supprimé."


# --- format_alert: replaced by bus ---------------------------------------------------------------


def test_format_bus_exact_text():
    text = format_alert(AlertKind.REPLACED_BY_BUS, train(Status.REPLACED_BY_BUS, reason="Travaux"))
    assert text == (
        "🚌 Remplacé par car · Nice-Ville → Monaco Monte-Carlo\n"
        "Le train 86012 de 07:42 est remplacé par un car.\n"
        "Motif : Travaux"
    )


def test_format_bus_without_reason_has_no_motif_line():
    text = format_alert(AlertKind.REPLACED_BY_BUS, train(Status.REPLACED_BY_BUS))
    assert text == (
        "🚌 Remplacé par car · Nice-Ville → Monaco Monte-Carlo\n"
        "Le train 86012 de 07:42 est remplacé par un car."
    )


# --- format_missing ------------------------------------------------------------------------------


def test_format_missing_with_and_without_suggestion():
    suggestion = train(
        Status.ON_TIME,
        from_id=CANNES,
        to_id=ANTIBES,
        base_departure=datetime(2026, 11, 9, 7, 52, tzinfo=PARIS),
        train_number="86103",
    )
    assert format_missing(follow("07:42"), suggestion) == (
        "❓ Ton train Cannes → Antibes de 07:42 est introuvable depuis 2 jours (horaires modifiés ?).\n"
        "Le plus proche : 07:52 (train 86103). Tu veux le suivre à la place ?"
    )
    assert format_missing(follow("07:42"), None) == (
        "❓ Ton train Cannes → Antibes de 07:42 est introuvable depuis 2 jours, "
        "et aucun train direct n'existe à ± 15 min. Tu peux le retirer avec /retirer."
    )


def test_format_missing_suggestion_time_in_paris():
    # 06:30 UTC on 2026-11-09 is 07:30 in Paris.
    suggestion = train(
        Status.ON_TIME,
        from_id=CANNES,
        to_id=ANTIBES,
        base_departure=datetime(2026, 11, 9, 6, 30, tzinfo=UTC),
        train_number="86099",
    )
    assert format_missing(follow("07:42"), suggestion) == (
        "❓ Ton train Cannes → Antibes de 07:42 est introuvable depuis 2 jours (horaires modifiés ?).\n"
        "Le plus proche : 07:30 (train 86099). Tu veux le suivre à la place ?"
    )
