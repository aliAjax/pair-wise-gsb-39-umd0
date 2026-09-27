"""Transfer guarantee computation layer (换乘保障计算).

Pure standard-library functions with no database access. The data layer
(``app.Database``) loads line/stop/trip rows and hands plain dictionaries to
this module; the page layer only renders the returned judgments. Keeping the
math here means the same rules drive draft checks, route queries and publish
snapshots.
"""
from __future__ import annotations

from typing import Any, Iterable

OK = "ok"
PENDING_ADJUSTMENT = "pending_adjustment"
NO_SERVICE = "no_service"
NO_ARRIVAL = "no_arrival"
UNREACHABLE = "unreachable"

STATUS_LABELS = {
    OK: "正常",
    PENDING_ADJUSTMENT: "待调整",
    NO_SERVICE: "无接驳班次",
    NO_ARRIVAL: "无到达班次",
    UNREACHABLE: "换乘站不可达",
}


def trip_stop_times(rows: list[dict[str, Any]], departure_minute: int, direction: int = 0) -> dict[int, int]:
    """Minute of the service day at which one trip serves each stop.

    ``rows`` are the line's line_stops ordered by sequence. Direction 0 starts
    at the first stop; direction 1 starts at the last stop and reuses the
    stored forward segment minutes in reverse.
    """
    times: dict[int, int] = {}
    if int(direction) == 0:
        current = departure_minute
        for index, row in enumerate(rows):
            if index:
                current += int(row["travel_minutes_from_previous"])
            times[int(row["stop_id"])] = current
        return times
    remaining = sum(int(row["travel_minutes_from_previous"]) for row in rows)
    for row in rows:
        remaining -= int(row["travel_minutes_from_previous"])
        times[int(row["stop_id"])] = departure_minute + remaining
    return times


def departures_at_stop(rows: list[dict[str, Any]], trips: list[dict[str, Any]], stop_id: int) -> list[int]:
    """Sorted service-day minutes at which the line's trips serve ``stop_id``."""
    minutes: list[int] = []
    for trip in trips:
        times = trip_stop_times(rows, int(trip["departure_minute"]), int(trip.get("direction", 0)))
        if int(stop_id) in times:
            minutes.append(times[int(stop_id)])
    return sorted(minutes)


def last_arrival_at_stop(line_specs: Iterable[dict[str, Any]], stop_id: int) -> int | None:
    """Latest minute any of the given lines serves ``stop_id`` (末班到达)."""
    best: int | None = None
    for spec in line_specs:
        for minute in departures_at_stop(spec["rows"], spec["trips"], stop_id):
            if best is None or minute > best:
                best = minute
    return best


def empty_judgment(status: str) -> dict[str, Any]:
    if status not in STATUS_LABELS:
        raise ValueError(f"unknown judgment status: {status}")
    return {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "arrival_minute": None,
        "ready_minute": None,
        "walk_minutes": None,
        "min_buffer_minutes": None,
        "latest_catchable_departure": None,
        "last_departure": None,
        "slack_minutes": None,
        "shortfall_minutes": None,
    }


def judge_guarantee(*, arrival_minute: int | None, walk_minutes: int, min_buffer_minutes: int,
                    departures: list[int]) -> dict[str, Any]:
    """Judge one transfer guarantee against an estimated arrival minute.

    The passenger is ready to board at ``arrival + walk``. The latest feeder
    departure they can still catch must leave at least ``min_buffer`` minutes
    of slack, otherwise the guarantee is marked 待调整 (pending_adjustment)
    with the exact shortfall in minutes.
    """
    ordered = sorted(int(minute) for minute in departures)
    last = ordered[-1] if ordered else None
    if arrival_minute is None:
        judgment = empty_judgment(NO_ARRIVAL)
        judgment.update({"walk_minutes": walk_minutes, "min_buffer_minutes": min_buffer_minutes,
                         "last_departure": last})
        return judgment
    ready = int(arrival_minute) + int(walk_minutes)
    if last is None:
        judgment = empty_judgment(NO_SERVICE)
        judgment.update({"arrival_minute": arrival_minute, "ready_minute": ready,
                         "walk_minutes": walk_minutes, "min_buffer_minutes": min_buffer_minutes})
        return judgment
    catchable = [minute for minute in ordered if minute >= ready]
    latest = catchable[-1] if catchable else None
    reference = latest if latest is not None else last
    slack = reference - ready
    if latest is not None and slack >= min_buffer_minutes:
        status, shortfall = OK, 0
    else:
        status, shortfall = PENDING_ADJUSTMENT, min_buffer_minutes - slack
    return {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "arrival_minute": arrival_minute,
        "ready_minute": ready,
        "walk_minutes": walk_minutes,
        "min_buffer_minutes": min_buffer_minutes,
        "latest_catchable_departure": latest,
        "last_departure": last,
        "slack_minutes": slack,
        "shortfall_minutes": shortfall,
    }
