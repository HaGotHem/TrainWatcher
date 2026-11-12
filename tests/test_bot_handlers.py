"""Task 9: Discord app — pure command handlers (monatrain.bot.handlers), no discord.py involved.

Pinned behaviour (brief + dispatch decisions):
- Every handler returns the French reply text (str); autocompletes return list[(label, value)],
  at most 25 items. Errors are RETURNED as text, never raised:
  * `FollowError` messages as-is ("Gare inconnue.", "Tu suis déjà 10 trains.", ...);
  * `SncfError` -> "Horaires SNCF momentanément indisponibles." (same text as the web 503 page);
  * a `parse_days` error (jours="perso") -> its French message.
- `handle_suivre(repo, source, limiter, user_id, username, depart_id, arrivee_id, heure, jours,
  jours_perso, now, *, install_url)`: `jours` is one of "semaine", "tous", "week-end", "perso";
  with "perso", `jours_perso` ("lun,mer,ven") is parsed by `parse_days`. It records the user (it
  gets the username) and the follow, and answers
  "✅ Je surveille le 08:04 Monaco Monte-Carlo → Nice-Ville (train 86053), lun-ven."
  If the user's dm_ok is 0, it adds
  "⚠️ Je n'arrive pas à t'envoyer de DM : installe l'app sur ton compte : {install_url}".
- Per-user limit: 20 calls / 600 s (SlidingWindowLimiter passed in) on the handlers that call the
  API (suivre, mes_trains); past it: "Doucement 🙂 réessaie dans quelques minutes." and no API call.
  Every call counts, whatever its outcome.
- `handle_retirer` on a follow that is not the user's -> "Train introuvable dans ta liste.", and the
  follow is untouched.
- `autocomplete_heure(source, depart_id, arrivee_id, current, now)`: "8" / "08" -> one journeys
  query from 08:00 on the next date where 08:00 is still ahead; labels
  "08:04 · train 86053 · arr. 08:24", value "08:04"; trains of another date are dropped; empty
  list (and no call) if a station is missing.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pytest

from monatrain.bot.handlers import (
    autocomplete_gare,
    autocomplete_heure,
    autocomplete_mes_trains,
    handle_departs,
    handle_mes_trains,
    handle_retirer,
    handle_suivre,
)
from monatrain.models import Fetched, Status, TrainInfo
from monatrain.ratelimit import SlidingWindowLimiter
from monatrain.sncf.client import SncfUnavailable
from monatrain.timeutil import PARIS, WEEKDAYS, WEEKEND, parse_days

MONACO = "stop_area:SNCF:87756403"
NICE = "stop_area:SNCF:87756056"
EZE = "stop_area:SNCF:87756387"
THEOULE = "stop_area:SNCF:87757583"
MENTON = "stop_area:SNCF:87756486"
UNKNOWN = "stop_area:SNCF:87000000"  # well-formed, but not one of the 29 stations

FRI = date(2026, 11, 6)  # fixed_now's day (conftest: Friday 2026-11-06 08:00 Paris)
THU = date(2026, 11, 5)
SAT = date(2026, 11, 7)

USER = "111"
OTHER_USER = "222"
INSTALL_URL = "https://discord.com/oauth2/authorize?client_id=123456789&integration_type=1&scope=applications.commands"

MSG_RATE = "Doucement 🙂 réessaie dans quelques minutes."
MSG_SNCF_DOWN = "Horaires SNCF momentanément indisponibles."
MSG_NO_TRAIN = "Tu ne suis aucun train. Utilise /suivre."
MSG_NOT_YOURS = "Train introuvable dans ta liste."
DM_WARNING = f"⚠️ Je n'arrive pas à t'envoyer de DM : installe l'app sur ton compte : {INSTALL_URL}"
SUCCESS_0804 = "✅ Je surveille le 08:04 Monaco Monte-Carlo → Nice-Ville (train 86053), lun-ven."


def at(d: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=PARIS)


def train(
    base: datetime,
    number: str = "86053",
    *,
    delay: int = 0,
    status: Status = Status.ON_TIME,
    from_id: str = MONACO,
    to_id: str = NICE,
    direction: str = "Nice-Ville",
) -> TrainInfo:
    dep = base + timedelta(minutes=delay)
    return TrainInfo(
        from_id=from_id,
        to_id=to_id,
        base_departure=base,
        departure=dep,
        base_arrival=base + timedelta(minutes=20),
        arrival=dep + timedelta(minutes=20),
        train_number=number,
        line="TER",
        direction=direction,
        physical_mode="TER / Intercités",
        status=status,
        delay_min=delay,
        reason=None,
        realtime=True,
    )


@dataclass(frozen=True)
class Call:
    kind: str
    args: tuple
    when: datetime | None
    count: int


class FakeSource:
    """Stands in for SncfClient: records calls, returns the same canned trains whatever is asked."""

    def __init__(self, items: list[TrainInfo] | None = None, *, error: Exception | None = None) -> None:
        self.items = list(items or [])
        self.error = error
        self.calls: list[Call] = []

    async def journeys(self, from_id: str, to_id: str, when: datetime, count: int = 8) -> Fetched[list[TrainInfo]]:
        self.calls.append(Call("journeys", (from_id, to_id), when, count))
        if self.error is not None:
            raise self.error
        return Fetched(list(self.items), datetime(2026, 11, 6, 7, 0, tzinfo=PARIS), False)

    async def departures(self, stop_id: str, count: int = 10) -> Fetched[list[TrainInfo]]:
        self.calls.append(Call("departures", (stop_id,), None, count))
        if self.error is not None:
            raise self.error
        return Fetched(list(self.items), datetime(2026, 11, 6, 7, 0, tzinfo=PARIS), False)


class FrozenClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def limiter_clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def limiter(limiter_clock) -> SlidingWindowLimiter:
    return SlidingWindowLimiter(20, 600, clock=limiter_clock)


def _user(repo, discord_id: str = USER) -> str:
    repo.upsert_user(discord_id, f"user{discord_id}", None)
    return discord_id


async def _suivre(repo, source, limiter, now, *, user_id=USER, depart=MONACO, arrivee=NICE,
                  heure="08:04", jours="semaine", jours_perso=None) -> str:
    return await handle_suivre(
        repo, source, limiter, user_id, f"user{user_id}", depart, arrivee, heure, jours, jours_perso, now,
        install_url=INSTALL_URL,
    )


# --- /suivre -----------------------------------------------------------------------------------


async def test_suivre_success_text(repo, limiter, fixed_now):
    source = FakeSource([train(at(FRI, "08:04"), "86053")])

    reply = await _suivre(repo, source, limiter, fixed_now)

    assert reply == SUCCESS_0804
    assert repo.get_user(USER) is not None  # the user is recorded (follows belong to a user)
    follows = repo.list_follows(USER)
    assert len(follows) == 1
    f = follows[0]
    assert (f.from_id, f.to_id, f.dep_time, f.days, f.train_number) == (MONACO, NICE, "08:04", WEEKDAYS, "86053")


async def test_suivre_saves_the_exact_time_of_the_train_found(repo, limiter, fixed_now):
    # Asked 08:00, the closest train leaves at 08:04: the reply and the follow use 08:04.
    source = FakeSource([train(at(FRI, "08:04"), "86053")])

    reply = await _suivre(repo, source, limiter, fixed_now, heure="08:00")

    assert reply == SUCCESS_0804
    assert repo.list_follows(USER)[0].dep_time == "08:04"


@pytest.mark.parametrize(
    ("jours", "label", "mask"),
    [("tous", "tous les jours", 127), ("week-end", "week-end", WEEKEND)],
)
async def test_suivre_presets(repo, limiter, fixed_now, jours, label, mask):
    # Friday 08:00: "tous" runs today (Fri 06), "week-end" first runs on Saturday 07.
    source = FakeSource([train(at(FRI, "08:04"), "86053"), train(at(SAT, "08:04"), "86053")])

    reply = await _suivre(repo, source, limiter, fixed_now, jours=jours)

    assert reply == f"✅ Je surveille le 08:04 Monaco Monte-Carlo → Nice-Ville (train 86053), {label}."
    assert repo.list_follows(USER)[0].days == mask


async def test_suivre_custom_days(repo, limiter, fixed_now):
    source = FakeSource([train(at(FRI, "08:04"), "86053")])

    reply = await _suivre(repo, source, limiter, fixed_now, jours="perso", jours_perso="lun,mer,ven")

    assert reply == "✅ Je surveille le 08:04 Monaco Monte-Carlo → Nice-Ville (train 86053), lun, mer, ven."
    assert repo.list_follows(USER)[0].days == 1 | 4 | 16


async def test_suivre_custom_days_parse_error_is_returned(repo, limiter, fixed_now):
    source = FakeSource([train(at(FRI, "08:04"), "86053")])
    with pytest.raises(ValueError) as expected:
        parse_days("lun,xyz")

    reply = await _suivre(repo, source, limiter, fixed_now, jours="perso", jours_perso="lun,xyz")

    assert reply == str(expected.value)
    assert repo.list_follows(USER) == []
    assert source.calls == []


@pytest.mark.parametrize(
    ("depart", "arrivee", "heure", "expected"),
    [
        (UNKNOWN, NICE, "08:04", "Gare inconnue."),
        (MONACO, MONACO, "08:04", "Les gares de départ et d'arrivée doivent être différentes."),
        (MONACO, NICE, "8h04", "Heure invalide, utilise le format HH:MM."),
        (MONACO, NICE, "11:00", "Aucun train direct trouvé autour de 11:00."),
    ],
)
async def test_suivre_error_passthrough(repo, limiter, fixed_now, depart, arrivee, heure, expected):
    source = FakeSource([train(at(FRI, "08:04"), "86053")])

    reply = await _suivre(repo, source, limiter, fixed_now, depart=depart, arrivee=arrivee, heure=heure)

    assert reply == expected
    assert repo.list_follows(USER) == []


async def test_suivre_error_passthrough_limit_and_duplicate(repo, limiter, fixed_now):
    _user(repo)
    source = FakeSource([train(at(FRI, "08:04"), "86053")])
    assert await _suivre(repo, source, limiter, fixed_now) == SUCCESS_0804

    assert await _suivre(repo, source, limiter, fixed_now) == "Tu suis déjà ce train."

    for i in range(9):  # 10 follows in all
        repo.add_follow(USER, NICE, MONACO, f"1{i}:00", WEEKDAYS, f"9000{i}")
    assert await _suivre(repo, source, limiter, fixed_now, depart=MENTON) == "Tu suis déjà 10 trains."
    assert len(repo.list_follows(USER)) == 10


async def test_suivre_sncf_error_is_returned(repo, limiter, fixed_now):
    source = FakeSource(error=SncfUnavailable("boom"))

    reply = await _suivre(repo, source, limiter, fixed_now)

    assert reply == MSG_SNCF_DOWN
    assert repo.list_follows(USER) == []


async def test_suivre_dm_warning_when_dm_blocked(repo, limiter, fixed_now):
    _user(repo)
    repo.set_dm_ok(USER, False)
    source = FakeSource([train(at(FRI, "08:04"), "86053")])

    reply = await _suivre(repo, source, limiter, fixed_now)

    assert reply.startswith(SUCCESS_0804)
    assert DM_WARNING in reply
    assert len(repo.list_follows(USER)) == 1


async def test_suivre_no_dm_warning_when_dm_ok(repo, limiter, fixed_now):
    _user(repo)
    repo.set_dm_ok(USER, True)
    source = FakeSource([train(at(FRI, "08:04"), "86053")])

    reply = await _suivre(repo, source, limiter, fixed_now)

    assert reply == SUCCESS_0804
    assert "⚠️" not in reply


# --- /mes-trains -------------------------------------------------------------------------------


async def test_mes_trains_empty_and_listed(repo, limiter, fixed_now):
    source = FakeSource([train(at(FRI, "08:04"), "86053", delay=7, status=Status.DELAYED)])
    _user(repo)
    _user(repo, OTHER_USER)

    assert await handle_mes_trains(repo, source, limiter, USER, fixed_now) == MSG_NO_TRAIN

    repo.add_follow(USER, MONACO, NICE, "08:04", WEEKDAYS, "86053")  # live today, delayed
    repo.add_follow(USER, NICE, MONACO, "18:30", WEEKDAYS, "86100")  # today, later: upcoming
    repo.add_follow(USER, NICE, EZE, "10:00", WEEKEND, "86200")  # does not run today
    repo.add_follow(OTHER_USER, MENTON, NICE, "07:15", WEEKDAYS, "86300")  # not this user's

    reply = await handle_mes_trains(repo, source, limiter, USER, fixed_now)
    lines = [line for line in reply.splitlines() if line.strip()]

    def line_with(hhmm: str) -> str:
        found = [line for line in lines if hhmm in line]
        assert len(found) == 1, f"{hhmm} should appear on exactly one line of: {reply!r}"
        return found[0]

    first = line_with("08:04")
    assert "Monaco Monte-Carlo" in first and "Nice-Ville" in first
    assert "lun-ven" in first
    assert "+7 min" in first

    evening = line_with("18:30")
    assert "Nice-Ville" in evening and "Monaco Monte-Carlo" in evening
    assert "lun-ven" in evening
    assert "Prévu" in evening

    weekend = line_with("10:00")
    assert "Èze" in weekend and "week-end" in weekend

    assert "07:15" not in reply and "Menton" not in reply


async def test_mes_trains_sncf_down_still_lists(repo, limiter, fixed_now):
    _user(repo)
    repo.add_follow(USER, MONACO, NICE, "08:04", WEEKDAYS, "86053")
    source = FakeSource(error=SncfUnavailable("boom"))

    reply = await handle_mes_trains(repo, source, limiter, USER, fixed_now)

    assert "08:04" in reply and "Monaco Monte-Carlo" in reply


# --- /retirer ----------------------------------------------------------------------------------


def test_retirer_other_users_train_refused(repo):
    _user(repo)
    _user(repo, OTHER_USER)
    theirs = repo.add_follow(OTHER_USER, MONACO, NICE, "08:04", WEEKDAYS, "86053")

    assert handle_retirer(repo, USER, theirs.id) == MSG_NOT_YOURS
    assert [f.id for f in repo.list_follows(OTHER_USER)] == [theirs.id]

    assert handle_retirer(repo, USER, 999_999) == MSG_NOT_YOURS


def test_retirer_own_train(repo):
    _user(repo)
    mine = repo.add_follow(USER, MONACO, NICE, "08:04", WEEKDAYS, "86053")

    reply = handle_retirer(repo, USER, mine.id)

    assert isinstance(reply, str) and reply
    assert reply != MSG_NOT_YOURS
    assert repo.list_follows(USER) == []


# --- autocompletes -----------------------------------------------------------------------------


def test_autocomplete_gare_accents():
    assert ("Èze", EZE) in autocomplete_gare("eze")
    assert ("Théoule-sur-Mer", THEOULE) in autocomplete_gare("theoule")
    assert autocomplete_gare("monaco") == [("Monaco Monte-Carlo", MONACO)]
    nice = autocomplete_gare("nice")
    assert ("Nice-Ville", NICE) in nice
    assert all("Nice" in label for label, _ in nice)


def test_autocomplete_gare_blank_and_cap():
    assert autocomplete_gare("") == []
    every = autocomplete_gare("e")  # matches most of the 29 stations
    assert 0 < len(every) <= 25


def test_autocomplete_mes_trains(repo):
    _user(repo)
    _user(repo, OTHER_USER)
    a = repo.add_follow(USER, MONACO, NICE, "08:04", WEEKDAYS, "86053")
    b = repo.add_follow(USER, NICE, MONACO, "18:30", WEEKDAYS, "86100")
    repo.add_follow(OTHER_USER, MENTON, NICE, "07:15", WEEKDAYS, "86300")

    choices = autocomplete_mes_trains(repo, USER)

    assert sorted(value for _, value in choices) == sorted([str(a.id), str(b.id)])
    labels = dict((value, label) for label, value in choices)
    assert "08:04" in labels[str(a.id)] and "Monaco Monte-Carlo" in labels[str(a.id)]
    assert "18:30" in labels[str(b.id)] and "Nice-Ville" in labels[str(b.id)]
    assert autocomplete_mes_trains(repo, "nobody") == []


@pytest.mark.parametrize("current", ["8", "08"])
async def test_autocomplete_heure_from_partial_hour(current):
    now = at(FRI, "07:30")
    source = FakeSource(
        [
            train(at(THU, "08:10"), "86099"),  # another date (stale answer): dropped
            train(at(FRI, "08:04"), "86053"),
            train(at(FRI, "08:34"), "86055"),
        ]
    )

    choices = await autocomplete_heure(source, MONACO, NICE, current, now)

    assert choices == [
        ("08:04 · train 86053 · arr. 08:24", "08:04"),
        ("08:34 · train 86055 · arr. 08:54", "08:34"),
    ]
    assert len(source.calls) == 1
    call = source.calls[0]
    assert call.kind == "journeys" and call.args == (MONACO, NICE)
    assert call.when == at(FRI, "08:00")


async def test_autocomplete_heure_past_hour_looks_at_next_day():
    now = at(FRI, "09:00")  # 08:00 is over today: tomorrow's trains
    source = FakeSource([train(at(SAT, "08:04"), "86053")])

    choices = await autocomplete_heure(source, MONACO, NICE, "8", now)

    assert choices == [("08:04 · train 86053 · arr. 08:24", "08:04")]
    assert source.calls[0].when == at(SAT, "08:00")


@pytest.mark.parametrize(("depart", "arrivee"), [(None, NICE), (MONACO, None), ("", NICE), (MONACO, "")])
async def test_autocomplete_heure_missing_station(depart, arrivee, fixed_now):
    source = FakeSource([train(at(FRI, "08:04"), "86053")])

    assert await autocomplete_heure(source, depart, arrivee, "8", fixed_now) == []
    assert source.calls == []


async def test_autocomplete_heure_sncf_error_gives_empty_list(fixed_now):
    source = FakeSource(error=SncfUnavailable("boom"))

    assert await autocomplete_heure(source, MONACO, NICE, "8", fixed_now) == []


async def test_autocomplete_heure_cap_25(fixed_now):
    many = [train(at(FRI, "09:00") + timedelta(minutes=2 * i), str(87000 + i)) for i in range(40)]
    source = FakeSource(many)

    choices = await autocomplete_heure(source, MONACO, NICE, "9", fixed_now)

    assert 0 < len(choices) <= 25


# --- /departs ----------------------------------------------------------------------------------


async def test_departs_with_destination_lists_next_trains(repo, limiter, fixed_now):
    trains = [train(at(FRI, "08:04") + timedelta(minutes=10 * i), str(86100 + i)) for i in range(10)]
    source = FakeSource(trains)

    reply = await handle_departs(repo, source, limiter, MONACO, NICE, fixed_now)

    assert "08:04" in reply and "86100" in reply
    shown = [t.train_number for t in trains if t.train_number in reply]
    assert 1 <= len(shown) <= 8
    assert all(c.kind == "journeys" and c.args == (MONACO, NICE) for c in source.calls)


async def test_departs_without_destination_uses_the_board(repo, limiter, fixed_now):
    source = FakeSource([train(at(FRI, "08:04"), "86053", direction="Vintimille")])

    reply = await handle_departs(repo, source, limiter, MONACO, None, fixed_now)

    assert "08:04" in reply and "86053" in reply
    assert source.calls and source.calls[0].kind == "departures" and source.calls[0].args == (MONACO,)


async def test_departs_errors_are_returned(repo, limiter, fixed_now):
    assert await handle_departs(repo, FakeSource(), limiter, UNKNOWN, None, fixed_now) == "Gare inconnue."
    down = FakeSource(error=SncfUnavailable("boom"))
    assert await handle_departs(repo, down, limiter, MONACO, NICE, fixed_now) == MSG_SNCF_DOWN


# --- per-user rate limit -----------------------------------------------------------------------


async def test_user_rate_limit_message(repo, limiter, limiter_clock, fixed_now):
    _user(repo)
    _user(repo, OTHER_USER)
    repo.add_follow(USER, MONACO, NICE, "08:04", WEEKDAYS, "86053")  # live: each call queries the API
    source = FakeSource([train(at(FRI, "08:04"), "86053")])

    for _ in range(20):
        assert await handle_mes_trains(repo, source, limiter, USER, fixed_now) != MSG_RATE
    calls_before = len(source.calls)

    assert await handle_mes_trains(repo, source, limiter, USER, fixed_now) == MSG_RATE
    assert await _suivre(repo, source, limiter, fixed_now, depart=NICE, arrivee=MONACO) == MSG_RATE
    assert len(source.calls) == calls_before  # refused before any API call
    assert len(repo.list_follows(USER)) == 1

    # Another user has their own budget.
    assert await _suivre(repo, source, limiter, fixed_now, user_id=OTHER_USER) == SUCCESS_0804

    # The window slides: after 600 s the user may call again.
    limiter_clock.t = 600.0
    assert await handle_mes_trains(repo, source, limiter, USER, fixed_now) != MSG_RATE


async def test_retirer_and_local_autocompletes_are_not_rate_limited(repo, limiter, fixed_now):
    _user(repo)
    source = FakeSource([train(at(FRI, "08:04"), "86053")])
    repo.add_follow(USER, MONACO, NICE, "08:04", WEEKDAYS, "86053")
    for _ in range(20):
        await handle_mes_trains(repo, source, limiter, USER, fixed_now)
    assert await handle_mes_trains(repo, source, limiter, USER, fixed_now) == MSG_RATE

    assert len(autocomplete_mes_trains(repo, USER)) == 1
    assert autocomplete_gare("nice")
    follow_id = repo.list_follows(USER)[0].id
    assert handle_retirer(repo, USER, follow_id) != MSG_RATE
    assert repo.list_follows(USER) == []


# --- bot client module -------------------------------------------------------------------------


def test_bot_client_module_imports():
    import importlib

    module = importlib.import_module("monatrain.bot.client")
    assert hasattr(module, "MonatrainBot")


# --- "Suivre HH:MM" button: handle_remplacer (Task 9 review, ruling R20) -------------------------
# `async handle_remplacer(repo, source, limiter, user_id, follow_id, hhmm, now) -> ReplaceResult`,
# where `ReplaceResult` is a NamedTuple (final: bool, text: str) exported by monatrain.bot.handlers.
# final=True: the answer is definitive, the client removes the buttons; final=False: the user may
# click again later (rate limit, SNCF down, no train found right now).
# Imported inside the tests so that the rest of this file keeps passing meanwhile.

MSG_NO_TRAIN_0800 = "Aucun train direct trouvé autour de 08:00."


async def _remplacer(repo, source, limiter, follow_id, hhmm="08:00", *, user_id=USER, now=None):
    from monatrain.bot.handlers import handle_remplacer

    return await handle_remplacer(
        repo, source, limiter, user_id, follow_id, hhmm, now or datetime(2026, 11, 6, 8, 0, tzinfo=PARIS)
    )


def _replace_result(final: bool, text: str):
    from monatrain.bot.handlers import ReplaceResult

    assert ReplaceResult._fields == ("final", "text")
    return ReplaceResult(final, text)


async def test_remplacer_success(repo, limiter):
    _user(repo)
    follow = repo.add_follow(USER, MONACO, NICE, "08:30", WEEKDAYS, "86999")
    repo.mark_suggestion_sent(follow.id)
    source = FakeSource([train(at(FRI, "08:04"), "86053")])

    result = await _remplacer(repo, source, limiter, follow.id)

    assert result == _replace_result(True, "✅ C'est noté, je surveille maintenant le 08:04.")
    assert (result.final, result.text) == (True, "✅ C'est noté, je surveille maintenant le 08:04.")
    [stored] = repo.list_follows(USER)
    assert (stored.dep_time, stored.train_number, stored.suggestion_sent) == ("08:04", "86053", False)


async def test_remplacer_not_owned_is_final(repo, limiter):
    _user(repo)
    _user(repo, OTHER_USER)
    theirs = repo.add_follow(OTHER_USER, MONACO, NICE, "08:30", WEEKDAYS, "86999")
    source = FakeSource([train(at(FRI, "08:04"), "86053")])

    assert await _remplacer(repo, source, limiter, theirs.id) == _replace_result(True, MSG_NOT_YOURS)
    assert repo.list_follows(OTHER_USER) == [theirs]
    assert await _remplacer(repo, source, limiter, 999_999) == _replace_result(True, MSG_NOT_YOURS)


async def test_remplacer_duplicate_is_final(repo, limiter):
    _user(repo)
    repo.add_follow(USER, MONACO, NICE, "08:04", WEEKDAYS, "86053")
    later = repo.add_follow(USER, MONACO, NICE, "08:30", WEEKDAYS, "86999")
    source = FakeSource([train(at(FRI, "08:04"), "86053")])

    assert await _remplacer(repo, source, limiter, later.id) == _replace_result(True, "Tu suis déjà ce train.")
    assert later in repo.list_follows(USER)


async def test_remplacer_sncf_down_is_not_final(repo, limiter):
    _user(repo)
    follow = repo.add_follow(USER, MONACO, NICE, "08:30", WEEKDAYS, "86999")
    source = FakeSource(error=SncfUnavailable("boom"))

    assert await _remplacer(repo, source, limiter, follow.id) == _replace_result(False, MSG_SNCF_DOWN)
    assert repo.list_follows(USER) == [follow]


async def test_remplacer_rate_limited_is_not_final(repo):
    _user(repo)
    follow = repo.add_follow(USER, MONACO, NICE, "08:30", WEEKDAYS, "86999")
    source = FakeSource([train(at(FRI, "08:04"), "86053")])
    exhausted = SlidingWindowLimiter(0, 600, clock=lambda: 0.0)

    assert await _remplacer(repo, source, exhausted, follow.id) == _replace_result(False, MSG_RATE)
    assert source.calls == []
    assert repo.list_follows(USER) == [follow]


async def test_remplacer_no_train_found_is_not_final(repo, limiter):
    _user(repo)
    follow = repo.add_follow(USER, MONACO, NICE, "08:30", WEEKDAYS, "86999")
    source = FakeSource([])

    assert await _remplacer(repo, source, limiter, follow.id) == _replace_result(False, MSG_NO_TRAIN_0800)
    assert repo.list_follows(USER) == [follow]


# --- autocomplete_needs_api (Task 9 review, ruling R20) -----------------------------------------
# `autocomplete_needs_api(depart_id, arrivee_id, current) -> bool`: True only when both stations are
# known and different, and `current` (stripped) is empty or an hour 0-23 / a prefix of HH:MM. The
# client hits its separate autocomplete bucket only when this is True.


def _needs_api(depart_id, arrivee_id, current):
    from monatrain.bot.handlers import autocomplete_needs_api

    return autocomplete_needs_api(depart_id, arrivee_id, current)


@pytest.mark.parametrize("current", ["", "  ", "8", "08", "0", "23", "08:", "08:0", "08:04", "8:04"])
def test_autocomplete_needs_api_true(current):
    assert _needs_api(MONACO, NICE, current) is True


@pytest.mark.parametrize("current", ["24", "99", "abc", "8h", "123", "08:045", "-1"])
def test_autocomplete_needs_api_bad_text(current):
    assert _needs_api(MONACO, NICE, current) is False


@pytest.mark.parametrize(
    ("depart", "arrivee"),
    [(None, NICE), (MONACO, None), ("", NICE), (MONACO, ""), (UNKNOWN, NICE), (MONACO, UNKNOWN), (MONACO, MONACO)],
)
def test_autocomplete_needs_api_stations(depart, arrivee):
    assert _needs_api(depart, arrivee, "8") is False
