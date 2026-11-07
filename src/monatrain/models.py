from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Status(StrEnum):
    ON_TIME = "on_time"
    DELAYED = "delayed"
    CANCELLED = "cancelled"
    REPLACED_BY_BUS = "replaced_by_bus"


@dataclass(frozen=True)
class TrainInfo:
    from_id: str
    to_id: str | None
    base_departure: datetime
    departure: datetime
    base_arrival: datetime | None
    arrival: datetime | None
    train_number: str
    line: str
    direction: str
    physical_mode: str
    status: Status
    delay_min: int
    reason: str | None
    realtime: bool


@dataclass(frozen=True)
class Fetched[T]:
    items: T
    fetched_at: datetime
    stale: bool
