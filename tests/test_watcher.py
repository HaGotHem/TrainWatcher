"""Task 6: the Watcher (monatrain.watcher) polls followed trains and sends DM alerts.

Pinned behaviour (brief + global constraints + review focus 1, 3, 4):
- A follow is watched on service day d (today AND yesterday, for trains around midnight) when
  base = d + dep_time (aware Europe/Paris) satisfies base - 60 min <= now <= base + max(last_delay, 0)
  (last_delay = daily_state.last_delay of d, None counting as 0).
- Follows are grouped by (from_id, to_id, d, dep_time): ONE source.journeys(from_id, to_id,
  base - 1 min, count=4) per group, at most once every 120 s (process memory).
- The answer may be empty or stale (trains of another time/date): only the train whose
  base_departure == base exactly counts.
- Train found: set_found(True) + set_last_delay(train.delay_min) for every subscriber, then decide().
  On an alert: notifier.send(user, format_alert(kind, train)); set_notified(kind.value, delay_min)
  only if send returned True, else set_dm_ok(False). One refused DM does not stop the others.
- SncfError: logged, the group is skipped, tick() does not raise.
- Window over (now > base) and the train never seen that day: set_found(False). When
  missing_streak >= 2 and no suggestion was sent yet: nearest_alternative, then
  send(format_missing(follow, alt), suggestion=(follow.id, alt)) and mark_suggestion_sent.
- An absent train never produces a "cancelled" DM; days outside the mask are ignored.
"""

import contextlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest

from monatrain.alerts import AlertKind
from monatrain.models import Fetched, Status, TrainInfo
from monatrain.sncf.client import SncfError, SncfUnavailable
from monatrain.messages import format_alert, format_missing
from monatrain.timeutil import ALL_DAYS, PARIS
from monatrain.watcher import Notifier, Watcher  # noqa: F401  (Notifier is part of the interface)

MONACO = "stop_area:SNCF:87756403"
NICE = "stop_area:SNCF:87756056"
CANNES = "stop_area:SNCF:87757625"
ANTIBES = "stop_area:SNCF:87757674"

MONDAY = 1
THURSDAY = 8
FRIDAY = 16
SATURDAY = 32

THU = date(2026, 11, 5)
FRI = date(2026, 11, 6)  # fixed_now's day
SAT = date(2026, 11, 7)

U1, U2, U3 = "111", "222", "333"


def at(d: date, hhmm: str, second: int = 0) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, second, tzinfo=PARIS)


def train(
    base: datetime,
    number: str = "86053",
    *,
    delay: int = 0,
    status: Status | None = None,
    from_id: str = MONACO,
    to_id: str = NICE,
) -> TrainInfo:
    if status is None:
        status = Status.DELAYED if delay > 0 else Status.ON_TIME
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
    """Stands in for SncfClient("watcher"): counts journeys() calls and returns canned trains.

    `items` is returned whatever the route/time asked (like a stale cache entry would), unless a
    per-route list is given in `routes`. `error` / `route_errors` make the call raise instead.
    """

    def __init__(
        self,
        items: list[TrainInfo] | None = None,
        *,
        routes: dict[tuple[str, str], list[TrainInfo]] | None = None,
        error: Exception | None = None,
        route_errors: dict[tuple[str, str], Exception] | None = None,
        when_errors: dict[datetime, Exception] | None = None,
        stale: bool = False,
    ) -> None:
        self.items = list(items or [])
        self.routes = dict(routes or {})
        self.error = error
        self.route_errors = dict(route_errors or {})
        self.when_errors = dict(when_errors or {})  # raise only for queries starting at that time
        self.stale = stale
        self.calls: list[Call] = []

    async def journeys(self, from_id: str, to_id: str, when: datetime, count: int = 8) -> Fetched[list[TrainInfo]]:
        self.calls.append(Call(from_id, to_id, when, count))
        if self.error is not None:
            raise self.error
        if (from_id, to_id) in self.route_errors:
            raise self.route_errors[(from_id, to_id)]
        if when in self.when_errors:
            raise self.when_errors[when]
        items = self.routes.get((from_id, to_id), self.items)
        return Fetched(list(items), datetime(2026, 11, 6, 7, 0, tzinfo=PARIS), self.stale)


@dataclass(frozen=True)
class Sent:
    user_id: str
    text: str
    suggestion: tuple[int, TrainInfo] | None


class FakeNotifier:
    """Records every send() attempt; returns False (DM refused) for the user ids in `refuse`."""

    def __init__(self, refuse: set[str] | None = None, *, crash: set[str] | None = None) -> None:
        self.refuse = set(refuse or ())
        self.crash = set(crash or ())  # send() raises for these user ids
        self.sent: list[Sent] = []

    async def send(self, user_id: str, text: str, *, suggestion: tuple[int, TrainInfo] | None = None) -> bool:
        self.sent.append(Sent(user_id, text, suggestion))
        if user_id in self.crash:
            raise RuntimeError("discord exploded")
        return user_id not in self.refuse


def follow_of(repo, user_id: str = U1, *, from_id=MONACO, to_id=NICE, dep="08:00", days=ALL_DAYS, number="86053"):
    if repo.get_user(user_id) is None:
        repo.upsert_user(user_id, f"user{user_id}", None)
    return repo.add_follow(user_id, from_id, to_id, dep, days, number)


# --- window --------------------------------------------------------------------------------------


async def test_not_watched_before_60_min(repo):
    f = follow_of(repo)
    source = FakeSource([train(at(FRI, "08:00"), delay=10)])
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    await w.tick(at(FRI, "06:59", 59))
    assert source.calls == []
    assert notifier.sent == []
    assert repo.get_daily(f.id, FRI).found is None

    await w.tick(at(FRI, "07:00"))  # base - 60 min: inclusive
    assert source.calls == [Call(MONACO, NICE, at(FRI, "07:59"), 4)]
    assert source.calls[0].when.utcoffset() == at(FRI, "07:59").utcoffset()
    assert len(notifier.sent) == 1


async def test_not_watched_after_window_without_delay(repo):
    follow_of(repo, days=FRIDAY)
    source = FakeSource([train(at(FRI, "08:00"))])
    w = Watcher(repo, source, FakeNotifier())

    await w.tick(at(FRI, "08:00"))  # base + max(None -> 0): inclusive
    assert len(source.calls) == 1
    await w.tick(at(FRI, "08:05"))  # on time, last_delay 0: window over
    assert len(source.calls) == 1


# --- alerts --------------------------------------------------------------------------------------


async def test_delay_alert_sent_once_then_on_growth(repo):
    f = follow_of(repo)
    source = FakeSource()
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)
    base = at(FRI, "08:00")

    # +3 min: below the threshold, no DM, but the state is tracked.
    source.items = [train(base, delay=3)]
    await w.tick(at(FRI, "07:20"))
    assert notifier.sent == []
    s = repo.get_daily(f.id, FRI)
    assert (s.found, s.last_delay, s.notified_status, s.notified_delay) == (True, 3, None, None)

    # +6 min: first DM.
    t6 = train(base, delay=6)
    source.items = [t6]
    await w.tick(at(FRI, "07:30"))
    assert notifier.sent == [Sent(U1, format_alert(AlertKind.DELAYED, t6), None)]
    s = repo.get_daily(f.id, FRI)
    assert (s.found, s.last_delay, s.notified_status, s.notified_delay) == (True, 6, "delayed", 6)

    # +8 min: grows by less than 5 min, no new DM; last_delay follows the train.
    source.items = [train(base, delay=8)]
    await w.tick(at(FRI, "07:33"))
    assert len(notifier.sent) == 1
    s = repo.get_daily(f.id, FRI)
    assert (s.last_delay, s.notified_status, s.notified_delay) == (8, "delayed", 6)

    # Same +8 again: still nothing.
    await w.tick(at(FRI, "07:36"))
    assert len(notifier.sent) == 1

    # +11 min: grew by >= 5 min since the last DM (6): new DM.
    t11 = train(base, delay=11)
    source.items = [t11]
    await w.tick(at(FRI, "07:39"))
    assert notifier.sent[1:] == [Sent(U1, format_alert(AlertKind.DELAYED, t11), None)]
    s = repo.get_daily(f.id, FRI)
    assert (s.last_delay, s.notified_status, s.notified_delay) == (11, "delayed", 11)

    # Cancelled: new DM of another kind.
    tc = train(base, status=Status.CANCELLED)
    source.items = [tc]
    await w.tick(at(FRI, "07:42"))
    assert notifier.sent[2:] == [Sent(U1, format_alert(AlertKind.CANCELLED, tc), None)]
    assert repo.get_daily(f.id, FRI).notified_status == "cancelled"

    # Still cancelled: no duplicate.
    await w.tick(at(FRI, "07:45"))
    assert len(notifier.sent) == 3
    assert len(source.calls) == 7


async def test_replaced_by_bus_alert(repo):
    f = follow_of(repo)
    tb = train(at(FRI, "08:00"), status=Status.REPLACED_BY_BUS)
    source = FakeSource([tb])
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    await w.tick(at(FRI, "07:30"))
    assert notifier.sent == [Sent(U1, format_alert(AlertKind.REPLACED_BY_BUS, tb), None)]
    assert repo.get_daily(f.id, FRI).notified_status == "replaced_by_bus"


async def test_on_time_train_no_dm(repo):
    f = follow_of(repo)
    source = FakeSource([train(at(FRI, "08:00"))])
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    await w.tick(at(FRI, "07:30"))
    assert notifier.sent == []
    s = repo.get_daily(f.id, FRI)
    assert (s.found, s.last_delay, s.notified_status) == (True, 0, None)


async def test_matches_exact_scheduled_departure_only(repo):
    """A stale/other answer: same time on another date, and another time today. Neither is ours."""
    f = follow_of(repo, days=FRIDAY)
    source = FakeSource(
        [
            train(at(THU, "08:00"), "86001", delay=20),
            train(at(FRI, "08:01"), "86002", delay=20),
            train(at(FRI, "07:59"), "86003", status=Status.CANCELLED),
        ]
    )
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    await w.tick(at(FRI, "07:30"))
    assert len(source.calls) == 1
    assert notifier.sent == []
    s = repo.get_daily(f.id, FRI)
    assert s.found is not True
    assert s.last_delay is None
    assert s.notified_status is None


# --- grouping / polling --------------------------------------------------------------------------


async def test_group_one_request_many_dms(repo):
    f1 = follow_of(repo, U1)
    f2 = follow_of(repo, U2)
    f3 = follow_of(repo, U3)
    t10 = train(at(FRI, "08:00"), delay=10)
    source = FakeSource([t10])
    notifier = FakeNotifier(refuse={U1})  # the first subscriber refuses: the others still get theirs
    w = Watcher(repo, source, notifier)

    await w.tick(at(FRI, "07:30"))

    assert source.calls == [Call(MONACO, NICE, at(FRI, "07:59"), 4)]
    text = format_alert(AlertKind.DELAYED, t10)
    assert sorted(notifier.sent, key=lambda s: s.user_id) == [Sent(U1, text, None), Sent(U2, text, None), Sent(U3, text, None)]

    for f in (f1, f2, f3):
        s = repo.get_daily(f.id, FRI)
        assert (s.found, s.last_delay) == (True, 10)
    assert repo.get_daily(f1.id, FRI).notified_status is None
    assert repo.get_daily(f1.id, FRI).notified_delay is None
    for f in (f2, f3):
        s = repo.get_daily(f.id, FRI)
        assert (s.notified_status, s.notified_delay) == ("delayed", 10)

    assert repo.get_user(U1)["dm_ok"] == 0
    assert repo.get_user(U2)["dm_ok"] == 1
    assert repo.get_user(U3)["dm_ok"] == 1


async def test_poll_interval_120s(repo):
    follow_of(repo)
    source = FakeSource([train(at(FRI, "08:00"), delay=10)])
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    await w.tick(at(FRI, "07:30"))
    await w.tick(at(FRI, "07:31"))  # t + 60 s: same group, too soon
    assert len(source.calls) == 1

    await w.tick(at(FRI, "07:33"))  # t + 180 s: polled again
    assert len(source.calls) == 2
    assert len(notifier.sent) == 1  # no duplicate DM


async def test_poll_interval_is_per_group(repo):
    follow_of(repo, U1)
    follow_of(repo, U2, from_id=CANNES, to_id=ANTIBES)
    follow_of(repo, U3, dep="08:10")
    source = FakeSource()
    w = Watcher(repo, source, FakeNotifier())

    await w.tick(at(FRI, "07:30"))
    assert sorted((c.from_id, c.to_id, c.when) for c in source.calls) == sorted(
        [
            (MONACO, NICE, at(FRI, "07:59")),
            (CANNES, ANTIBES, at(FRI, "07:59")),
            (MONACO, NICE, at(FRI, "08:09")),
        ]
    )
    assert all(c.count == 4 for c in source.calls)


async def test_window_across_midnight(repo):
    f = follow_of(repo, dep="23:50", days=ALL_DAYS)
    base = at(FRI, "23:50")
    t15 = train(base, delay=15)  # leaves at 00:05 on Saturday
    source = FakeSource([t15])
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    await w.tick(at(FRI, "23:45"))  # in the window before base: train seen at +15 min
    assert notifier.sent == [Sent(U1, format_alert(AlertKind.DELAYED, t15), None)]
    assert repo.get_daily(f.id, FRI).last_delay == 15
    assert source.calls == [Call(MONACO, NICE, at(FRI, "23:49"), 4)]

    await w.tick(at(FRI, "23:55"))  # after base, still watched thanks to last_delay 15
    assert source.calls[1:] == [Call(MONACO, NICE, at(FRI, "23:49"), 4)]
    assert len(notifier.sent) == 1  # no duplicate DM

    await w.tick(at(SAT, "00:03"))  # yesterday's train, still within base + 15 min
    assert source.calls[2:] == [Call(MONACO, NICE, at(FRI, "23:49"), 4)]
    assert len(notifier.sent) == 1  # no duplicate DM
    s = repo.get_daily(f.id, FRI)
    assert (s.found, s.last_delay, s.notified_status, s.notified_delay) == (True, 15, "delayed", 15)
    assert repo.get_daily(f.id, SAT) == repo.get_daily(-1, SAT)  # Saturday's own train untouched

    await w.tick(at(SAT, "00:06"))  # past base + 15 min: no longer watched
    assert len(source.calls) == 3
    assert len(notifier.sent) == 1
    assert repo.get_daily(f.id, FRI).found is True


# --- resilience ----------------------------------------------------------------------------------


async def test_tick_survives_source_error(repo):
    f = follow_of(repo)
    t10 = train(at(FRI, "08:00"), delay=10)
    source = FakeSource([t10], error=SncfUnavailable("API SNCF indisponible (HTTP 503)"))
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    await w.tick(at(FRI, "07:30"))  # must not raise
    assert len(source.calls) == 1
    assert notifier.sent == []
    s = repo.get_daily(f.id, FRI)
    assert (s.found, s.last_delay, s.notified_status) == (None, None, None)

    source.error = None  # API back: retried on a later tick
    await w.tick(at(FRI, "07:33"))
    assert len(source.calls) == 2
    assert notifier.sent == [Sent(U1, format_alert(AlertKind.DELAYED, t10), None)]


async def test_source_error_on_one_group_does_not_stop_others(repo):
    f_bad = follow_of(repo, U1)
    follow_of(repo, U2, from_id=CANNES, to_id=ANTIBES)
    t10 = train(at(FRI, "08:00"), delay=10, from_id=CANNES, to_id=ANTIBES)
    source = FakeSource(
        routes={(CANNES, ANTIBES): [t10]},
        route_errors={(MONACO, NICE): SncfError("Requête SNCF refusée (HTTP 400, bad_request)")},
    )
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    await w.tick(at(FRI, "07:30"))
    assert len(source.calls) == 2
    assert notifier.sent == [Sent(U2, format_alert(AlertKind.DELAYED, t10), None)]
    assert repo.get_daily(f_bad.id, FRI).found is None


# --- missing trains ------------------------------------------------------------------------------


async def test_absent_train_never_sends_cancel(repo):
    f = follow_of(repo, days=FRIDAY)
    # Our 08:00 is absent; the answer holds another, cancelled train.
    source = FakeSource([train(at(FRI, "08:10"), "86999", status=Status.CANCELLED)])
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    await w.tick(at(FRI, "07:30"))
    await w.tick(at(FRI, "07:45"))
    assert notifier.sent == []
    assert repo.get_daily(f.id, FRI).found is None  # not yet over

    await w.tick(at(FRI, "08:01"))  # window over, never seen
    assert notifier.sent == []
    s = repo.get_daily(f.id, FRI)
    assert (s.found, s.notified_status, s.notified_delay) == (False, None, None)
    assert repo.missing_streak(f.id) == 1


async def test_missing_two_days_sends_one_suggestion(repo):
    f = follow_of(repo, days=THURSDAY | FRIDAY | SATURDAY)
    alt = train(at(FRI, "08:06"), "86999")
    source = FakeSource([])  # Navitia "no_solution"
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    # Thursday: absent all along.
    await w.tick(at(THU, "07:30"))
    await w.tick(at(THU, "08:01"))
    assert repo.get_daily(f.id, THU).found is False
    assert repo.missing_streak(f.id) == 1
    assert notifier.sent == []

    # Friday: still absent (only an 08:06 train exists now).
    source.items = [alt]
    await w.tick(at(FRI, "07:30"))
    assert notifier.sent == []
    await w.tick(at(FRI, "08:01"))
    assert repo.get_daily(f.id, FRI).found is False
    assert repo.missing_streak(f.id) == 2
    assert notifier.sent == [Sent(U1, format_missing(f, alt), (f.id, alt))]
    assert repo.all_follows()[0].suggestion_sent is True

    # Saturday: absent again, but the single suggestion was already sent.
    source.items = []
    await w.tick(at(SAT, "07:30"))
    await w.tick(at(SAT, "08:01"))
    assert repo.missing_streak(f.id) == 3
    assert len(notifier.sent) == 1


async def test_missing_suggestion_without_alternative(repo):
    f = follow_of(repo, days=THURSDAY | FRIDAY)
    source = FakeSource([])
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    for d in (THU, FRI):
        await w.tick(at(d, "07:30"))
        await w.tick(at(d, "08:01"))

    assert notifier.sent == [Sent(U1, format_missing(f, None), None)]
    assert repo.all_follows()[0].suggestion_sent is True


async def test_found_train_resets_missing_streak_no_suggestion(repo):
    f = follow_of(repo, days=THURSDAY | FRIDAY | SATURDAY)
    source = FakeSource([])
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    await w.tick(at(THU, "07:30"))
    await w.tick(at(THU, "08:01"))  # missing on Thursday
    source.items = [train(at(FRI, "08:00"))]
    await w.tick(at(FRI, "07:30"))  # seen on Friday
    await w.tick(at(FRI, "08:01"))
    assert repo.get_daily(f.id, FRI).found is True
    source.items = []
    await w.tick(at(SAT, "07:30"))
    await w.tick(at(SAT, "08:01"))  # missing on Saturday: streak 1 only
    assert repo.missing_streak(f.id) == 1
    assert notifier.sent == []


# Ruling R12 (a): "missing" (found=False) only after at least one SUCCESSFUL journeys call that day.


async def test_missing_not_marked_when_first_tick_after_base(repo):
    """Restart at base + 10 min: the train was never polled in its window, so nothing is known."""
    f = follow_of(repo, days=FRIDAY)
    source = FakeSource([])
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    await w.tick(at(FRI, "08:10"))
    await w.tick(at(FRI, "08:20"))
    assert source.calls == []
    assert notifier.sent == []
    assert repo.get_daily(f.id, FRI).found is None
    assert repo.missing_streak(f.id) == 0


async def test_missing_not_marked_when_every_call_failed(repo):
    f = follow_of(repo, days=FRIDAY)
    source = FakeSource([], error=SncfUnavailable("API SNCF indisponible (HTTP 503)"))
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    await w.tick(at(FRI, "07:30"))
    await w.tick(at(FRI, "07:45"))
    assert len(source.calls) == 2
    await w.tick(at(FRI, "08:01"))  # window over, but every in-window call raised
    assert notifier.sent == []
    assert repo.get_daily(f.id, FRI).found is None
    assert repo.missing_streak(f.id) == 0


async def test_missing_marked_after_one_successful_call_without_train(repo):
    f = follow_of(repo, days=FRIDAY)
    source = FakeSource([], error=SncfUnavailable("API SNCF indisponible (HTTP 503)"))
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    await w.tick(at(FRI, "07:30"))  # fails
    source.error = None
    source.items = [train(at(FRI, "08:10"), "86999")]  # succeeds, but not our train
    await w.tick(at(FRI, "07:45"))
    assert len(source.calls) == 2
    assert repo.get_daily(f.id, FRI).found is None  # not over yet

    await w.tick(at(FRI, "08:01"))
    assert notifier.sent == []
    assert repo.get_daily(f.id, FRI).found is False
    assert repo.missing_streak(f.id) == 1


# Ruling R12 (b): a refused missing-train DM still counts as the one suggestion.


async def test_missing_suggestion_refused_dm_still_marked_sent(repo):
    f = follow_of(repo, days=THURSDAY | FRIDAY | SATURDAY)
    source = FakeSource([])
    notifier = FakeNotifier(refuse={U1})
    w = Watcher(repo, source, notifier)

    for d in (THU, FRI):
        await w.tick(at(d, "07:30"))
        await w.tick(at(d, "08:01"))

    assert notifier.sent == [Sent(U1, format_missing(f, None), None)]
    assert repo.all_follows()[0].suggestion_sent is True
    assert repo.get_user(U1)["dm_ok"] == 0

    await w.tick(at(FRI, "08:30"))
    await w.tick(at(SAT, "07:30"))
    await w.tick(at(SAT, "08:01"))  # missing a third day: still no new attempt
    assert repo.missing_streak(f.id) == 3
    assert len(notifier.sent) == 1


# --- day mask ------------------------------------------------------------------------------------


async def test_day_not_in_mask_ignored(repo):
    f = follow_of(repo, days=MONDAY)  # fixed day: Friday
    source = FakeSource([train(at(FRI, "08:00"), delay=20)])
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    await w.tick(at(FRI, "07:30"))
    await w.tick(at(FRI, "08:01"))
    assert source.calls == []
    assert notifier.sent == []
    assert repo.get_daily(f.id, FRI).found is None
    assert repo.missing_streak(f.id) == 0


# --- R14: a stale answer counts as a failed call ------------------------------------------------


async def test_stale_answer_without_train_never_marks_missing(repo):
    f = follow_of(repo, days=THURSDAY | FRIDAY)
    source = FakeSource([], stale=True)
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    for d in (THU, FRI):
        await w.tick(at(d, "07:30"))
        await w.tick(at(d, "07:45"))
        await w.tick(at(d, "08:01"))
        assert repo.get_daily(f.id, d).found is None
    assert repo.missing_streak(f.id) == 0
    assert notifier.sent == []
    assert repo.all_follows()[0].suggestion_sent is False


async def test_stale_answer_with_delayed_train_ignored(repo):
    f = follow_of(repo, days=FRIDAY)
    source = FakeSource([train(at(FRI, "08:00"), delay=10)], stale=True)
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    await w.tick(at(FRI, "07:30"))
    assert len(source.calls) == 1
    assert notifier.sent == []
    assert repo.get_daily(f.id, FRI) == repo.get_daily(-1, FRI)  # no found / last_delay / notified


# --- R15: tomorrow is a candidate service date --------------------------------------------------


async def test_train_after_midnight_polled_the_evening_before(repo):
    follow_of(repo, dep="00:20", days=ALL_DAYS)
    source = FakeSource([])
    w = Watcher(repo, source, FakeNotifier())

    await w.tick(at(FRI, "23:20"))  # Saturday 00:20 - 60 min
    assert source.calls == [Call(MONACO, NICE, at(SAT, "00:19"), 4)]


async def test_midnight_train_delay_alert_from_previous_evening(repo):
    f = follow_of(repo, dep="00:00", days=ALL_DAYS)
    t6 = train(at(SAT, "00:00"), delay=6)
    source = FakeSource([t6])
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    await w.tick(at(FRI, "23:00"))
    assert source.calls == [Call(MONACO, NICE, at(FRI, "23:59"), 4)]
    assert notifier.sent == [Sent(U1, format_alert(AlertKind.DELAYED, t6), None)]
    s = repo.get_daily(f.id, SAT)
    assert (s.found, s.last_delay, s.notified_status, s.notified_delay) == (True, 6, "delayed", 6)


# --- R16: suggestion retries ---------------------------------------------------------------------


async def test_suggestion_retry_throttled_after_source_error(repo):
    f = follow_of(repo, days=THURSDAY | FRIDAY)
    suggestion_query = at(FRI, "07:45")  # nearest_alternative asks from dep_time - 15 min
    source = FakeSource([], when_errors={suggestion_query: SncfUnavailable("API SNCF indisponible (HTTP 503)")})
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    def suggestion_calls() -> int:
        return sum(1 for c in source.calls if c.when == suggestion_query)

    await w.tick(at(THU, "07:30"))
    await w.tick(at(THU, "08:01"))  # missing day 1
    await w.tick(at(FRI, "07:30"))
    await w.tick(at(FRI, "08:01"))  # missing day 2: suggestion attempted, source fails
    assert repo.get_daily(f.id, FRI).found is False
    assert suggestion_calls() == 1
    assert notifier.sent == []
    assert repo.all_follows()[0].suggestion_sent is False

    for hhmm in ("08:02", "08:10", "08:20", "08:30"):  # less than 30 min later: no new attempt
        await w.tick(at(FRI, hhmm))
    assert suggestion_calls() == 1

    source.when_errors.clear()
    await w.tick(at(FRI, "08:32"))  # 31 min after the failure: retried, succeeds
    assert suggestion_calls() == 2
    assert notifier.sent == [Sent(U1, format_missing(f, None), None)]
    assert repo.all_follows()[0].suggestion_sent is True

    await w.tick(at(FRI, "09:10"))
    assert suggestion_calls() == 2
    assert len(notifier.sent) == 1


async def test_suggestion_send_crash_still_marked_sent(repo):
    f = follow_of(repo, days=THURSDAY | FRIDAY | SATURDAY)
    source = FakeSource([])
    notifier = FakeNotifier(crash={U1})
    w = Watcher(repo, source, notifier)

    await w.tick(at(THU, "07:30"))
    await w.tick(at(THU, "08:01"))
    await w.tick(at(FRI, "07:30"))
    with contextlib.suppress(Exception):  # whether tick() swallows it is not pinned here
        await w.tick(at(FRI, "08:01"))
    assert notifier.sent == [Sent(U1, format_missing(f, None), None)]
    assert repo.all_follows()[0].suggestion_sent is True

    for when in (at(FRI, "08:40"), at(FRI, "09:30"), at(SAT, "07:30"), at(SAT, "08:01"), at(SAT, "09:00")):
        await w.tick(when)
    assert len(notifier.sent) == 1


# --- R17 -----------------------------------------------------------------------------------------


async def test_successful_send_restores_dm_ok(repo):
    follow_of(repo)
    repo.set_dm_ok(U1, False)
    t10 = train(at(FRI, "08:00"), delay=10)
    notifier = FakeNotifier()
    w = Watcher(repo, FakeSource([t10]), notifier)

    await w.tick(at(FRI, "07:30"))
    assert notifier.sent == [Sent(U1, format_alert(AlertKind.DELAYED, t10), None)]
    assert repo.get_user(U1)["dm_ok"] == 1


async def test_malformed_dep_time_follow_skipped(repo, db):
    follow_of(repo, U1)  # creates user U1
    repo.upsert_user(U2, "user222", None)
    db.execute(
        "INSERT INTO followed (user_id, from_id, to_id, dep_time, days, train_number) VALUES (?, ?, ?, ?, ?, ?)",
        (U2, CANNES, ANTIBES, "8h", ALL_DAYS, "86077"),
    )
    t10 = train(at(FRI, "08:00"), delay=10)
    source = FakeSource(routes={(MONACO, NICE): [t10]})
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    await w.tick(at(FRI, "07:30"))  # must not raise
    assert source.calls == [Call(MONACO, NICE, at(FRI, "07:59"), 4)]
    assert notifier.sent == [Sent(U1, format_alert(AlertKind.DELAYED, t10), None)]


async def test_poll_interval_real_elapsed_time_across_dst_fall_back(repo):
    """2026-10-25: 03:00 CEST -> 02:00 CET. 02:59 CEST and 02:02 CET (fold=1) are 3 real minutes apart."""
    sun = date(2026, 10, 25)
    base = datetime(2026, 10, 25, 1, 30, tzinfo=PARIS)  # unambiguous, CEST
    late = train(base, delay=120)  # keeps the window open until 01:30 UTC
    follow_of(repo, dep="01:30", days=ALL_DAYS)
    source = FakeSource([late])
    notifier = FakeNotifier()
    w = Watcher(repo, source, notifier)

    before = datetime(2026, 10, 25, 2, 59, tzinfo=PARIS)  # fold=0, CEST
    after = datetime(2026, 10, 25, 2, 2, tzinfo=PARIS, fold=1)  # CET
    assert before.utcoffset() == timedelta(hours=2)
    assert after.utcoffset() == timedelta(hours=1)
    assert after.astimezone(UTC) - before.astimezone(UTC) == timedelta(minutes=3)

    await w.tick(at(sun, "01:00"))  # +120 seen: DM, last_delay 120
    assert len(notifier.sent) == 1
    await w.tick(before)
    assert len(source.calls) == 2
    await w.tick(after)  # 180 s of real time later: polled again
    assert len(source.calls) == 3
    assert len(notifier.sent) == 1


async def test_constructor_defaults(repo):
    w = Watcher(repo, FakeSource(), FakeNotifier(), poll_interval_s=120, window_before_min=60)
    await w.tick(at(FRI, "07:30"))  # no follows: nothing to do, no error
