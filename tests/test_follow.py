"""Task 5: shared "follow a train" services (monatrain.follow), used by the web site and the bot.

Pinned behaviour (brief + global constraints):
- `list_trains_around(source, from_id, to_id, hhmm, on, count=8)` asks `source.journeys` from
  `on` + `hhmm` - 15 min (aware Europe/Paris) and returns its items.
- `resolve_train(source, from_id, to_id, hhmm, days, now)` looks, on `next_service_date(days, hhmm,
  now)`, for the train whose SCHEDULED departure (base_departure) is closest to `hhmm`, within
  +/- 15 min (inclusive). Only trains of that service date count: a stale answer may carry trains
  of an earlier query. Same station twice, or a station outside the list, is refused before any
  API call. `SncfError` from the source is not turned into a `FollowError`.
- `add_follow(...)` saves the EXACT time and the number of the train found, and turns
  `FollowLimitError` / `DuplicateFollowError` into French `FollowError`s.
- `today_statuses(repo, source, user_id, now)` lists the user's follows that run today, sorted by
  time; calls the API only for those scheduled within [now - 30 min, now + 3 h] ("live"); others are
  "past" / "upcoming" without a call. The train is matched strictly on its scheduled departure of
  today. A live train absent from the answer is "missing" if today's daily_state.found is False,
  else "live" with train=None. `stale` comes from the Fetched answer.
- `nearest_alternative(source, follow, on)`: the train closest to the follow's time on `on`, within
  +/- 15 min, excluding the follow's current time; None if there is none.
"""

import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pytest

from monatrain.follow import (
    FollowError,
    TodayItem,
    add_follow,
    list_trains_around,
    nearest_alternative,
    resolve_train,
    today_statuses,
)
from monatrain.models import Fetched, Status, TrainInfo
from monatrain.repo import Follow
from monatrain.sncf.client import SncfError, SncfUnavailable
from monatrain.timeutil import ALL_DAYS, PARIS, WEEKDAYS, WEEKEND

MONACO = "stop_area:SNCF:87756403"
NICE = "stop_area:SNCF:87756056"
CANNES = "stop_area:SNCF:87757625"
ANTIBES = "stop_area:SNCF:87757674"
MENTON = "stop_area:SNCF:87756486"
UNKNOWN = "stop_area:SNCF:87000000"  # well-formed, but not one of the 29 stations ("hors liste")

MONDAY = 1
FRIDAY = 16

FRI = date(2026, 11, 6)  # fixed_now's day
THU = date(2026, 11, 5)
MON = date(2026, 11, 9)

USER = "111"
OTHER_USER = "222"

MSG_SAME_STATION = "Les gares de départ et d'arrivée doivent être différentes."
MSG_UNKNOWN_STATION = "Gare inconnue."
MSG_BAD_TIME = "Heure invalide, utilise le format HH:MM."
MSG_LIMIT = "Tu suis déjà 10 trains."
MSG_DUPLICATE = "Tu suis déjà ce train."


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
        direction="Nice-Ville",
        physical_mode="TER / Intercités",
        status=status,
        delay_min=delay,
        reason=None,
        realtime=True,
    )


@dataclass(frozen=True)
class Call:
    from_id: str
    to_id: str
    when: datetime
    count: int


class FakeSource:
    """Stands in for SncfClient: records every journeys() call and returns canned trains.

    It returns the same canned list whatever the route and time asked, like a stale cache entry
    of an earlier query would: the code under test must pick the relevant trains itself.
    """

    def __init__(
        self,
        items: list[TrainInfo] | None = None,
        *,
        stale: bool = False,
        error: Exception | None = None,
        route_errors: dict[tuple[str, str], Exception] | None = None,
    ) -> None:
        self.items = list(items or [])
        self.stale = stale
        self.error = error
        self.route_errors = dict(route_errors or {})
        self.calls: list[Call] = []

    async def journeys(self, from_id: str, to_id: str, when: datetime, count: int = 8) -> Fetched[list[TrainInfo]]:
        self.calls.append(Call(from_id, to_id, when, count))
        if self.error is not None:
            raise self.error
        if (from_id, to_id) in self.route_errors:
            raise self.route_errors[(from_id, to_id)]
        return Fetched(list(self.items), datetime(2026, 11, 6, 7, 0, tzinfo=PARIS), self.stale)


def _user(repo, discord_id: str = USER) -> str:
    repo.upsert_user(discord_id, f"user{discord_id}", None)
    return discord_id


def _follow(
    repo,
    dep_time: str,
    *,
    user_id: str = USER,
    from_id: str = MONACO,
    to_id: str = NICE,
    days: int = WEEKDAYS,
    number: str = "86053",
) -> Follow:
    return repo.add_follow(user_id, from_id, to_id, dep_time, days, number)


# --- FollowError / TodayItem -------------------------------------------------------------------


def test_follow_error_is_a_value_error():
    assert issubclass(FollowError, ValueError)
    assert str(FollowError(MSG_LIMIT)) == MSG_LIMIT


def test_today_item_is_a_dataclass_with_the_brief_fields():
    assert dataclasses.is_dataclass(TodayItem)
    assert [f.name for f in dataclasses.fields(TodayItem)] == ["follow", "state", "train", "stale"]


# --- list_trains_around ------------------------------------------------------------------------


async def test_list_trains_around_queries_15_min_before_and_returns_trains():
    trains = [train(at(MON, "07:52"), "86051"), train(at(MON, "08:04"), "86053")]
    source = FakeSource(trains)

    result = await list_trains_around(source, MONACO, NICE, "08:00", MON)

    assert result == trains
    assert len(source.calls) == 1
    call = source.calls[0]
    assert (call.from_id, call.to_id) == (MONACO, NICE)
    assert call.when == at(MON, "07:45")
    assert call.when.utcoffset() == timedelta(hours=1)  # aware, Paris winter time
    assert call.count == 8


async def test_list_trains_around_passes_count():
    source = FakeSource([])

    assert await list_trains_around(source, MONACO, NICE, "08:00", MON, count=3) == []
    assert source.calls[0].count == 3


async def test_list_trains_around_window_start_crosses_midnight():
    source = FakeSource([])

    await list_trains_around(source, MONACO, NICE, "00:05", MON)

    assert source.calls[0].when == at(date(2026, 11, 8), "23:50")


# --- resolve_train -----------------------------------------------------------------------------


async def test_resolve_exact_time(fixed_now):
    # Friday 08:00: today's 07:42 has left, Sat/Sun are not in the mask -> service date is Monday.
    wanted = train(at(MON, "07:42"), "86053")
    source = FakeSource([train(at(MON, "07:30"), "86051"), wanted, train(at(MON, "07:55"), "86055")])

    result = await resolve_train(source, MONACO, NICE, "07:42", WEEKDAYS, fixed_now)

    assert result == wanted
    assert len(source.calls) == 1
    call = source.calls[0]
    assert (call.from_id, call.to_id) == (MONACO, NICE)
    assert call.when == at(MON, "07:27")


async def test_resolve_nearest_within_15_min_saves_exact_time(repo):
    now = datetime(2026, 11, 6, 7, 0, tzinfo=PARIS)  # Friday 07:00 -> service date is today
    _user(repo)
    source = FakeSource(
        [
            train(at(FRI, "07:40"), "86049"),
            train(at(FRI, "08:04"), "86053"),
            train(at(FRI, "08:20"), "86057"),
        ]
    )

    resolved = await resolve_train(source, MONACO, NICE, "08:00", WEEKDAYS, now)
    assert resolved.train_number == "86053"
    assert resolved.base_departure == at(FRI, "08:04")

    follow = await add_follow(repo, source, USER, MONACO, NICE, "08:00", WEEKDAYS, now)

    assert isinstance(follow, Follow)
    assert follow.user_id == USER
    assert (follow.from_id, follow.to_id) == (MONACO, NICE)
    assert follow.dep_time == "08:04"
    assert follow.train_number == "86053"
    assert follow.days == WEEKDAYS
    assert repo.list_follows(USER) == [follow]


async def test_resolve_prefers_closest_scheduled_time_over_realtime(fixed_now):
    # Service date: Monday (Friday 08:00, hhmm 08:00 with a Monday-only mask).
    # 07:47 delayed by 13 min leaves at 08:00 but is scheduled 13 min away; 08:09 is 9 min away.
    delayed = train(at(MON, "07:47"), "86051", delay=13, status=Status.DELAYED)  # leaves 08:00
    scheduled_closer = train(at(MON, "08:09"), "86055")
    source = FakeSource([delayed, scheduled_closer])

    result = await resolve_train(source, MONACO, NICE, "08:00", MONDAY, fixed_now)

    assert result == scheduled_closer


@pytest.mark.parametrize("hhmm", ["07:45", "08:15"])
async def test_resolve_15_min_is_inclusive(fixed_now, hhmm):
    source = FakeSource([train(at(MON, "08:00"), "86053")])

    result = await resolve_train(source, MONACO, NICE, hhmm, MONDAY, fixed_now)

    assert result.base_departure == at(MON, "08:00")


@pytest.mark.parametrize("hhmm", ["07:44", "08:16"])
async def test_resolve_16_min_away_is_not_found(fixed_now, hhmm):
    source = FakeSource([train(at(MON, "08:00"), "86053")])

    with pytest.raises(FollowError) as exc_info:
        await resolve_train(source, MONACO, NICE, hhmm, MONDAY, fixed_now)

    assert str(exc_info.value) == f"Aucun train direct trouvé autour de {hhmm}."


@pytest.mark.parametrize(
    "items",
    [
        pytest.param([], id="empty-answer"),
        pytest.param(
            [train(at(MON, "07:30"), "86049"), train(at(MON, "08:30"), "86061")], id="all-out-of-window"
        ),
    ],
)
async def test_resolve_none_raises_french_message(fixed_now, items):
    source = FakeSource(items)

    with pytest.raises(FollowError) as exc_info:
        await resolve_train(source, MONACO, NICE, "08:00", MONDAY, fixed_now)

    assert str(exc_info.value) == "Aucun train direct trouvé autour de 08:00."
    assert isinstance(exc_info.value, ValueError)


async def test_resolve_ignores_trains_of_another_date_from_stale_answer(fixed_now):
    # Service date is Monday; the stale answer comes from an earlier query and holds Thursday's train.
    source = FakeSource([train(at(THU, "08:04"), "86053")], stale=True)

    with pytest.raises(FollowError) as exc_info:
        await resolve_train(source, MONACO, NICE, "08:00", MONDAY, fixed_now)

    assert str(exc_info.value) == "Aucun train direct trouvé autour de 08:00."


async def test_follow_same_station_rejected(repo, fixed_now):
    _user(repo)
    source = FakeSource([train(at(MON, "08:04"), "86053", from_id=NICE, to_id=NICE)])

    with pytest.raises(FollowError) as exc_info:
        await resolve_train(source, NICE, NICE, "08:00", MONDAY, fixed_now)
    assert str(exc_info.value) == MSG_SAME_STATION

    with pytest.raises(FollowError) as exc_info:
        await add_follow(repo, source, USER, NICE, NICE, "08:00", MONDAY, fixed_now)
    assert str(exc_info.value) == MSG_SAME_STATION

    assert source.calls == []
    assert repo.list_follows(USER) == []


@pytest.mark.parametrize(
    ("from_id", "to_id"),
    [pytest.param(UNKNOWN, NICE, id="unknown-departure"), pytest.param(MONACO, UNKNOWN, id="unknown-arrival")],
)
async def test_follow_unknown_station_rejected(repo, fixed_now, from_id, to_id):
    _user(repo)
    source = FakeSource([train(at(MON, "08:04"), "86053", from_id=from_id, to_id=to_id)])

    with pytest.raises(FollowError) as exc_info:
        await resolve_train(source, from_id, to_id, "08:00", MONDAY, fixed_now)
    assert str(exc_info.value) == MSG_UNKNOWN_STATION

    with pytest.raises(FollowError) as exc_info:
        await add_follow(repo, source, USER, from_id, to_id, "08:00", MONDAY, fixed_now)
    assert str(exc_info.value) == MSG_UNKNOWN_STATION

    assert source.calls == []
    assert repo.list_follows(USER) == []


async def test_resolve_lets_sncf_error_propagate(fixed_now):
    source = FakeSource(error=SncfUnavailable("API SNCF injoignable (ConnectError)"))

    with pytest.raises(SncfError) as exc_info:
        await resolve_train(source, MONACO, NICE, "08:00", MONDAY, fixed_now)

    assert not isinstance(exc_info.value, FollowError)


# --- add_follow --------------------------------------------------------------------------------


async def test_add_follow_limit_message(repo, fixed_now):
    _user(repo)
    for i in range(10):
        _follow(repo, f"06:{i:02d}", number=f"860{i:02d}")
    source = FakeSource([train(at(MON, "08:04"), "86053")])

    with pytest.raises(FollowError) as exc_info:
        await add_follow(repo, source, USER, MONACO, NICE, "08:00", MONDAY, fixed_now)

    assert str(exc_info.value) == MSG_LIMIT
    assert len(repo.list_follows(USER)) == 10


async def test_add_follow_duplicate_message(repo, fixed_now):
    _user(repo)
    existing = _follow(repo, "08:04", number="86053")
    source = FakeSource([train(at(MON, "08:04"), "86053")])

    # 08:00 resolves to the 08:04 train, which is already followed on this route.
    with pytest.raises(FollowError) as exc_info:
        await add_follow(repo, source, USER, MONACO, NICE, "08:00", MONDAY, fixed_now)

    assert str(exc_info.value) == MSG_DUPLICATE
    assert repo.list_follows(USER) == [existing]


async def test_add_follow_not_found_saves_nothing(repo, fixed_now):
    _user(repo)
    source = FakeSource([])

    with pytest.raises(FollowError) as exc_info:
        await add_follow(repo, source, USER, MONACO, NICE, "08:00", MONDAY, fixed_now)

    assert str(exc_info.value) == "Aucun train direct trouvé autour de 08:00."
    assert repo.list_follows(USER) == []


async def test_add_follow_lets_sncf_error_propagate(repo, fixed_now):
    _user(repo)
    source = FakeSource(error=SncfError("Requête SNCF refusée (HTTP 400, bad_request)"))

    with pytest.raises(SncfError) as exc_info:
        await add_follow(repo, source, USER, MONACO, NICE, "08:00", MONDAY, fixed_now)

    assert not isinstance(exc_info.value, FollowError)
    assert repo.list_follows(USER) == []


# --- today_statuses ----------------------------------------------------------------------------

NOW_0830 = datetime(2026, 11, 6, 8, 30, tzinfo=PARIS)  # Friday


async def test_today_statuses_only_calls_api_for_live_window(repo):
    _user(repo)
    # Inserted out of order: the result must be sorted by time.
    evening = _follow(repo, "20:00", from_id=NICE, to_id=MENTON, number="86099")
    early = _follow(repo, "06:00", from_id=CANNES, to_id=ANTIBES, number="86001")
    live = _follow(repo, "09:00", from_id=MONACO, to_id=NICE, number="86061")
    live_train = train(at(FRI, "09:00"), "86061", delay=6, status=Status.DELAYED)
    source = FakeSource([live_train])

    items = await today_statuses(repo, source, USER, NOW_0830)

    assert len(source.calls) == 1
    call = source.calls[0]
    assert (call.from_id, call.to_id) == (MONACO, NICE)
    assert call.when.date() == FRI

    assert [item.follow for item in items] == [early, live, evening]
    assert [item.state for item in items] == ["past", "live", "upcoming"]
    assert [item.train for item in items] == [None, live_train, None]
    assert [item.stale for item in items] == [False, False, False]
    assert all(isinstance(item, TodayItem) for item in items)


async def test_today_statuses_skips_days_not_running(repo):
    _user(repo)
    _follow(repo, "09:00", days=WEEKEND, number="86061")  # in the live window, but not on Fridays
    _follow(repo, "10:00", days=MONDAY, number="86071")  # in the live window, but Mondays only
    friday_only = _follow(repo, "12:00", days=FRIDAY, number="86081")
    weekdays = _follow(repo, "21:00", days=WEEKDAYS, number="86099")
    every_day = _follow(repo, "06:00", days=ALL_DAYS, number="86001")
    source = FakeSource([train(at(FRI, "09:00"), "86061"), train(at(FRI, "10:00"), "86071")])

    items = await today_statuses(repo, source, USER, NOW_0830)

    assert [item.follow for item in items] == [every_day, friday_only, weekdays]
    assert [item.state for item in items] == ["past", "upcoming", "upcoming"]
    assert source.calls == []


async def test_today_statuses_lists_only_the_users_follows(repo):
    _user(repo)
    _user(repo, OTHER_USER)
    mine = _follow(repo, "21:00", number="86099")
    _follow(repo, "20:00", user_id=OTHER_USER, number="86097")
    _follow(repo, "09:00", user_id=OTHER_USER, number="86061")  # live, but not this user's
    source = FakeSource([train(at(FRI, "09:00"), "86061")])

    items = await today_statuses(repo, source, USER, NOW_0830)

    assert [item.follow for item in items] == [mine]
    assert source.calls == []


async def test_today_statuses_without_follows_is_empty(repo):
    _user(repo)
    source = FakeSource([])

    assert await today_statuses(repo, source, USER, NOW_0830) == []
    assert source.calls == []


async def test_today_statuses_window_bounds_are_inclusive(repo):
    _user(repo)
    _follow(repo, "07:59", from_id=CANNES, to_id=ANTIBES, number="86011")  # now - 31 min
    _follow(repo, "08:00", from_id=MONACO, to_id=NICE, number="86021")  # now - 30 min
    _follow(repo, "11:30", from_id=NICE, to_id=MONACO, number="86031")  # now + 3 h
    _follow(repo, "11:31", from_id=ANTIBES, to_id=CANNES, number="86041")  # now + 3 h 1 min
    source = FakeSource([])

    items = await today_statuses(repo, source, USER, NOW_0830)

    assert [item.follow.dep_time for item in items] == ["07:59", "08:00", "11:30", "11:31"]
    assert [item.state for item in items] == ["past", "live", "live", "upcoming"]
    assert {(c.from_id, c.to_id) for c in source.calls} == {(MONACO, NICE), (NICE, MONACO)}


async def test_today_statuses_matches_scheduled_time_strictly(repo):
    _user(repo)
    _follow(repo, "09:00", number="86061")
    other_time = train(at(FRI, "09:05"), "86063")
    followed = train(at(FRI, "09:00"), "86061", delay=7, status=Status.DELAYED)  # leaves 09:07
    source = FakeSource([other_time, followed])

    [item] = await today_statuses(repo, source, USER, NOW_0830)

    assert item.state == "live"
    assert item.train == followed


async def test_today_statuses_live_absent_and_not_found_is_missing(repo):
    _user(repo)
    follow = _follow(repo, "09:00", number="86061")
    repo.set_found(follow.id, FRI, False)
    # Only a train at another scheduled time: strict matching -> the followed one is absent.
    source = FakeSource([train(at(FRI, "09:05"), "86063")])

    [item] = await today_statuses(repo, source, USER, NOW_0830)

    assert item.follow == follow
    assert item.state == "missing"
    assert item.train is None
    assert len(source.calls) == 1


@pytest.mark.parametrize("found", [None, True], ids=["found-unknown", "found-true"])
async def test_today_statuses_live_absent_otherwise_stays_live(repo, found):
    _user(repo)
    follow = _follow(repo, "09:00", number="86061")
    if found is not None:
        repo.set_found(follow.id, FRI, found)
    source = FakeSource([])

    [item] = await today_statuses(repo, source, USER, NOW_0830)

    assert item.state == "live"
    assert item.train is None
    assert len(source.calls) == 1


async def test_today_statuses_missing_uses_todays_daily_state(repo):
    _user(repo)
    follow = _follow(repo, "09:00", number="86061")
    repo.set_found(follow.id, THU, False)  # yesterday only; nothing known for today
    source = FakeSource([])

    [item] = await today_statuses(repo, source, USER, NOW_0830)

    assert item.state == "live"
    assert item.train is None


async def test_today_statuses_propagates_stale(repo):
    _user(repo)
    _follow(repo, "09:00", number="86061")
    cached = train(at(FRI, "09:00"), "86061")
    source = FakeSource([cached], stale=True)

    [item] = await today_statuses(repo, source, USER, NOW_0830)

    assert item.state == "live"
    assert item.train == cached
    assert item.stale is True


async def test_today_statuses_stale_train_of_another_day_does_not_match(repo):
    _user(repo)
    _follow(repo, "09:00", number="86061")
    # A stale answer from an earlier query: yesterday's 09:00, same number. Not today's train.
    source = FakeSource([train(at(THU, "09:00"), "86061", delay=20, status=Status.DELAYED)], stale=True)

    [item] = await today_statuses(repo, source, USER, NOW_0830)

    assert item.state == "live"
    assert item.train is None


# --- nearest_alternative -----------------------------------------------------------------------


def _follow_obj(dep_time: str = "07:42") -> Follow:
    return Follow(
        id=1,
        user_id=USER,
        from_id=MONACO,
        to_id=NICE,
        dep_time=dep_time,
        days=WEEKDAYS,
        train_number="86053",
        suggestion_sent=False,
    )


async def test_nearest_alternative_picks_closest_other_time():
    best = train(at(MON, "07:50"), "86055")  # 8 min after
    source = FakeSource(
        [
            train(at(MON, "07:42"), "86053"),  # the current time: excluded
            best,
            train(at(MON, "07:30"), "86051"),  # 12 min before
            train(at(MON, "08:05"), "86059"),  # 23 min: out of window
        ]
    )

    result = await nearest_alternative(source, _follow_obj("07:42"), MON)

    assert result == best
    assert len(source.calls) == 1
    call = source.calls[0]
    assert (call.from_id, call.to_id) == (MONACO, NICE)
    assert at(MON, "06:42") <= call.when <= at(MON, "07:27")  # the query covers 07:27 onwards


async def test_nearest_alternative_uses_scheduled_time():
    # 07:30 delayed by 11 leaves at 07:41 but is scheduled 12 min away; 07:52 is 10 min away.
    delayed = train(at(MON, "07:30"), "86051", delay=11, status=Status.DELAYED)
    closer = train(at(MON, "07:52"), "86055")
    source = FakeSource([delayed, closer])

    assert await nearest_alternative(source, _follow_obj("07:42"), MON) == closer


async def test_nearest_alternative_15_min_is_inclusive():
    edge = train(at(MON, "07:57"), "86057")
    source = FakeSource([train(at(MON, "07:42"), "86053"), edge])

    assert await nearest_alternative(source, _follow_obj("07:42"), MON) == edge


async def test_nearest_alternative_none_when_only_current_or_far():
    source = FakeSource(
        [
            train(at(MON, "07:42"), "86053"),
            train(at(MON, "07:26"), "86049"),  # 16 min before
            train(at(MON, "07:58"), "86059"),  # 16 min after
        ]
    )

    assert await nearest_alternative(source, _follow_obj("07:42"), MON) is None


async def test_nearest_alternative_none_on_empty_answer():
    assert await nearest_alternative(FakeSource([]), _follow_obj("07:42"), MON) is None


async def test_nearest_alternative_ignores_trains_of_another_date():
    # Stale answer from an earlier query: Friday's 07:50, while the alternative is wanted for Monday.
    source = FakeSource([train(at(FRI, "07:50"), "86055")], stale=True)

    assert await nearest_alternative(source, _follow_obj("07:42"), MON) is None


# --- Controller ruling R10 ---------------------------------------------------------------------


async def test_today_statuses_one_call_per_route(repo):
    _user(repo)
    first = _follow(repo, "09:00", number="86061")
    second = _follow(repo, "09:30", number="86065")
    t_first = train(at(FRI, "09:00"), "86061")
    t_second = train(at(FRI, "09:30"), "86065", delay=5, status=Status.DELAYED)
    source = FakeSource([t_first, t_second])

    items = await today_statuses(repo, source, USER, NOW_0830)

    assert len(source.calls) == 1
    assert (source.calls[0].from_id, source.calls[0].to_id) == (MONACO, NICE)
    assert [item.follow for item in items] == [first, second]
    assert [item.state for item in items] == ["live", "live"]
    assert [item.train for item in items] == [t_first, t_second]


async def test_today_statuses_is_read_only(repo):
    _user(repo)
    past = _follow(repo, "06:00", from_id=CANNES, to_id=ANTIBES, number="86001")
    seen = _follow(repo, "09:00", number="86061")  # live, in the answer
    absent = _follow(repo, "09:30", number="86065")  # live, not in the answer
    unknown = _follow(repo, "10:00", from_id=NICE, to_id=MONACO, number="86071")  # live, no row
    upcoming = _follow(repo, "20:00", from_id=NICE, to_id=MENTON, number="86099")
    repo.set_found(past.id, FRI, True)
    repo.set_last_delay(seen.id, FRI, 2)
    repo.set_found(absent.id, FRI, True)
    source = FakeSource([train(at(FRI, "09:00"), "86061", delay=9, status=Status.DELAYED)])
    follows = [past, seen, absent, unknown, upcoming]
    before = {f.id: (repo.get_daily(f.id, FRI), repo.get_daily(f.id, THU)) for f in follows}

    await today_statuses(repo, source, USER, NOW_0830)

    after = {f.id: (repo.get_daily(f.id, FRI), repo.get_daily(f.id, THU)) for f in follows}
    assert after == before


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(SncfError("Requête SNCF refusée (HTTP 400, bad_request)"), id="SncfError"),
        pytest.param(SncfUnavailable("API SNCF injoignable (ConnectError)"), id="SncfUnavailable"),
    ],
)
async def test_today_statuses_survives_source_error_on_one_route(repo, error):
    _user(repo)
    early = _follow(repo, "06:00", from_id=MONACO, to_id=NICE, number="86001")  # past, same route
    broken_a = _follow(repo, "09:00", from_id=MONACO, to_id=NICE, number="86061")
    broken_b = _follow(repo, "09:30", from_id=MONACO, to_id=NICE, number="86065")
    repo.set_found(broken_b.id, FRI, False)  # would be "missing" with an answer; without one: live
    ok = _follow(repo, "10:00", from_id=NICE, to_id=MONACO, number="86071")
    later = _follow(repo, "20:00", from_id=MONACO, to_id=NICE, number="86099")  # upcoming, same route
    ok_train = train(at(FRI, "10:00"), "86071", from_id=NICE, to_id=MONACO)
    source = FakeSource([ok_train], route_errors={(MONACO, NICE): error})

    items = await today_statuses(repo, source, USER, NOW_0830)

    assert [item.follow for item in items] == [early, broken_a, broken_b, ok, later]
    assert [item.state for item in items] == ["past", "live", "live", "live", "upcoming"]
    assert [item.train for item in items] == [None, None, None, ok_train, None]
    assert [item.stale for item in items] == [False, True, True, False, False]
    assert {(c.from_id, c.to_id) for c in source.calls} == {(MONACO, NICE), (NICE, MONACO)}


async def test_resolve_tie_picks_earliest_scheduled(fixed_now):
    later = train(at(MON, "08:05"), "86055")
    earlier = train(at(MON, "07:55"), "86053")
    source = FakeSource([later, earlier])  # the later one comes first in the answer

    assert await resolve_train(source, MONACO, NICE, "08:00", MONDAY, fixed_now) == earlier


async def test_nearest_alternative_tie_picks_earliest_scheduled():
    later = train(at(MON, "07:50"), "86055")  # 8 min after 07:42
    earlier = train(at(MON, "07:34"), "86051")  # 8 min before 07:42
    source = FakeSource([later, earlier])

    assert await nearest_alternative(source, _follow_obj("07:42"), MON) == earlier


# --- Controller ruling R11 ---------------------------------------------------------------------

SUN = date(2026, 11, 8)


async def test_today_statuses_one_call_covers_wide_route_span(repo, fixed_now):
    # now = Fri 08:00, live window [07:30, 11:00]: 07:50 and 10:50 are both live, 180 min apart.
    _user(repo)
    first = _follow(repo, "07:50", number="86049")
    last = _follow(repo, "10:50", number="86079")
    t_first = train(at(FRI, "07:50"), "86049")
    t_last = train(at(FRI, "10:50"), "86079")
    source = FakeSource([t_first, t_last])

    items = await today_statuses(repo, source, USER, fixed_now)

    assert len(source.calls) == 1
    call = source.calls[0]
    assert (call.from_id, call.to_id) == (MONACO, NICE)
    assert call.when.date() == FRI
    assert call.when <= at(FRI, "07:50")
    assert call.count >= max(8, 180 // 10 + 2)  # 20
    assert [item.follow for item in items] == [first, last]
    assert [item.train for item in items] == [t_first, t_last]


async def test_resolve_only_considers_the_service_date(fixed_now):
    # Monday-only mask -> service date Mon 9 Nov. Sunday 23:59 is 6 min from 00:05 but on another date.
    sunday = train(at(SUN, "23:59"), "86001")
    monday = train(at(MON, "00:12"), "86003")
    source = FakeSource([sunday, train(at(SUN, "23:55"), "86000"), monday])

    assert await resolve_train(source, MONACO, NICE, "00:05", MONDAY, fixed_now) == monday


async def test_nearest_alternative_only_considers_the_given_date():
    sunday = train(at(SUN, "23:59"), "86001")
    monday = train(at(MON, "00:12"), "86003")
    source = FakeSource([sunday, monday])

    assert await nearest_alternative(source, _follow_obj("00:05"), MON) == monday


async def test_list_trains_around_drops_other_dates():
    monday = train(at(MON, "00:12"), "86003")
    source = FakeSource([train(at(SUN, "23:55"), "86000"), monday])

    assert await list_trains_around(source, MONACO, NICE, "00:05", MON) == [monday]


@pytest.mark.parametrize("hhmm", ["8h00", "25:00", ""])
async def test_malformed_time_rejected(repo, fixed_now, hhmm):
    _user(repo)
    source = FakeSource([train(at(MON, "08:00"), "86053")])

    with pytest.raises(FollowError) as exc_info:
        await resolve_train(source, MONACO, NICE, hhmm, MONDAY, fixed_now)
    assert str(exc_info.value) == MSG_BAD_TIME

    with pytest.raises(FollowError) as exc_info:
        await add_follow(repo, source, USER, MONACO, NICE, hhmm, MONDAY, fixed_now)
    assert str(exc_info.value) == MSG_BAD_TIME

    assert source.calls == []
    assert repo.list_follows(USER) == []


# --- replace_follow_time (Task 9 review, ruling R20) ---------------------------------------------
# `replace_follow_time(repo, source, user_id, follow_id, hhmm, now) -> Follow`: re-resolves the
# follow's route at `hhmm` on next_service_date(follow.days, hhmm, now) and stores the EXACT time and
# number of the train found (suggestion_sent reset). Imported inside the tests so that the rest of
# this file keeps passing while the function does not exist yet.

MSG_NOT_YOURS = "Train introuvable dans ta liste."


async def _replace(repo, source, user_id, follow_id, hhmm, now):
    from monatrain.follow import replace_follow_time

    return await replace_follow_time(repo, source, user_id, follow_id, hhmm, now)


async def test_replace_follow_time_success(repo, fixed_now):
    _user(repo)
    follow = _follow(repo, "08:30", number="86999")
    repo.mark_suggestion_sent(follow.id)
    source = FakeSource([train(at(FRI, "08:04"), "86053")])

    updated = await _replace(repo, source, USER, follow.id, "08:00", fixed_now)

    assert isinstance(updated, Follow)
    assert (updated.id, updated.dep_time, updated.train_number, updated.suggestion_sent) == (
        follow.id, "08:04", "86053", False,
    )
    [stored] = repo.list_follows(USER)
    assert (stored.id, stored.dep_time, stored.train_number, stored.suggestion_sent) == (
        follow.id, "08:04", "86053", False,
    )
    assert (stored.from_id, stored.to_id, stored.days) == (MONACO, NICE, WEEKDAYS)


async def test_replace_follow_time_other_users_follow_refused(repo, fixed_now):
    _user(repo)
    _user(repo, OTHER_USER)
    theirs = _follow(repo, "08:30", user_id=OTHER_USER, number="86999")
    source = FakeSource([train(at(FRI, "08:04"), "86053")])

    with pytest.raises(FollowError) as exc_info:
        await _replace(repo, source, USER, theirs.id, "08:00", fixed_now)
    assert str(exc_info.value) == MSG_NOT_YOURS
    assert repo.list_follows(OTHER_USER) == [theirs]

    with pytest.raises(FollowError) as exc_info:
        await _replace(repo, source, USER, 999_999, "08:00", fixed_now)
    assert str(exc_info.value) == MSG_NOT_YOURS


async def test_replace_follow_time_duplicate(repo, fixed_now):
    _user(repo)
    _follow(repo, "08:04", number="86053")
    later = _follow(repo, "08:30", number="86999")
    source = FakeSource([train(at(FRI, "08:04"), "86053")])

    with pytest.raises(FollowError) as exc_info:
        await _replace(repo, source, USER, later.id, "08:00", fixed_now)

    assert str(exc_info.value) == MSG_DUPLICATE
    assert later in repo.list_follows(USER)


async def test_replace_follow_time_sncf_error_propagates(repo, fixed_now):
    _user(repo)
    follow = _follow(repo, "08:30", number="86999")
    source = FakeSource(error=SncfUnavailable("boom"))

    with pytest.raises(SncfError):
        await _replace(repo, source, USER, follow.id, "08:00", fixed_now)
    assert repo.list_follows(USER) == [follow]


@pytest.mark.parametrize("hhmm", ["8h", "8:00", "24:00"])
async def test_replace_follow_time_malformed_time(repo, fixed_now, hhmm):
    _user(repo)
    follow = _follow(repo, "08:30", number="86999")
    source = FakeSource([train(at(FRI, "08:04"), "86053")])

    with pytest.raises(FollowError) as exc_info:
        await _replace(repo, source, USER, follow.id, hhmm, fixed_now)

    assert str(exc_info.value) == MSG_BAD_TIME
    assert source.calls == []
    assert repo.list_follows(USER) == [follow]
