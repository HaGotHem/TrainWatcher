"""Pure alert decision: which DM, if any, to send for a followed train."""

from enum import StrEnum

from monatrain.models import Status, TrainInfo
from monatrain.repo import DailyState

DELAY_THRESHOLD_MIN = 5


class AlertKind(StrEnum):
    DELAYED = "delayed"
    CANCELLED = "cancelled"
    REPLACED_BY_BUS = "replaced_by_bus"


def decide(state: DailyState, train: TrainInfo) -> AlertKind | None:
    last = state.notified_status

    if train.status is Status.CANCELLED:
        return None if last == AlertKind.CANCELLED else AlertKind.CANCELLED

    if train.status is Status.REPLACED_BY_BUS:
        if last not in (AlertKind.REPLACED_BY_BUS, AlertKind.CANCELLED):
            return AlertKind.REPLACED_BY_BUS

    if train.status is Status.DELAYED and train.delay_min >= DELAY_THRESHOLD_MIN:
        if last is None:
            return AlertKind.DELAYED
        if (
            last == AlertKind.DELAYED
            and state.notified_delay is not None
            and train.delay_min >= state.notified_delay + DELAY_THRESHOLD_MIN
        ):
            return AlertKind.DELAYED

    return None
