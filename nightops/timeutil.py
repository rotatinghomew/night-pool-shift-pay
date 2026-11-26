"""时间工具：带偏移的 ISO 8601、营业日、双周工资期。

所有交换时刻都带偏移，统一折到 UTC 比较；营业日按站点所在时区
（样本均为 +08:00）的日历日归属，跨午夜的班归开始营业日。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

DEFAULT_TZ = timezone(timedelta(hours=8))
# 2026-01-05 是周一；每两个自然周为一期，期号从该周一起算。
PERIOD_ANCHOR = date(2026, 1, 5)
PERIOD_DAYS = 14


class TimeError(ValueError):
    """时刻或区间不符合约定。"""


def parse_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise TimeError(f"时刻必须是字符串: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TimeError(f"时刻不是 ISO 8601: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TimeError(f"时刻必须带偏移: {value!r}")
    return parsed


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TimeError(f"时刻必须带偏移: {value!r}")
    return value.astimezone(timezone.utc)


def format_time(value: datetime, tz: timezone = DEFAULT_TZ) -> str:
    return to_utc(value).astimezone(tz).isoformat()


def business_day(value: datetime, tz: timezone = DEFAULT_TZ) -> date:
    """按营业日时区归属日历日。"""

    return to_utc(value).astimezone(tz).date()


def round_cents(value: Decimal | int | float) -> int:
    """按分取整，四舍五入。"""

    return int(Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True, order=True)
class Interval:
    """半开区间 [start, end)，内部按 UTC 比较。"""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.start.utcoffset() is None:
            raise TimeError("区间起点必须带偏移")
        if self.end.tzinfo is None or self.end.utcoffset() is None:
            raise TimeError("区间终点必须带偏移")
        object.__setattr__(self, "start", to_utc(self.start))
        object.__setattr__(self, "end", to_utc(self.end))
        if self.start >= self.end:
            raise TimeError("区间必须为正长度")

    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: "Interval") -> "Interval | None":
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        if start < end:
            return Interval(start, end)
        return None

    def contains(self, moment: datetime) -> bool:
        return self.start <= to_utc(moment) < self.end

    def subtract(self, other: "Interval") -> list["Interval"]:
        if not self.overlaps(other):
            return [self]
        pieces: list[Interval] = []
        if self.start < other.start:
            pieces.append(Interval(self.start, min(other.start, self.end)))
        if other.end < self.end:
            pieces.append(Interval(max(other.end, self.start), self.end))
        return pieces

    def describe(self) -> dict:
        return {"from": format_time(self.start), "until": format_time(self.end)}

    def minutes_in(self, other: "Interval") -> int:
        overlap = self.intersection(other)
        return overlap.minutes() if overlap else 0


def subtract_intervals(base: Interval, cuts: list[Interval]) -> list[Interval]:
    pieces = [base]
    for cut in cuts:
        next_pieces: list[Interval] = []
        for piece in pieces:
            next_pieces.extend(piece.subtract(cut))
        pieces = next_pieces
        if not pieces:
            break
    return sorted(pieces)


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    ordered = sorted(intervals)
    merged: list[Interval] = []
    for item in ordered:
        if merged and item.start <= merged[-1].end:
            last = merged[-1]
            merged[-1] = Interval(last.start, max(last.end, item.end))
        else:
            merged.append(item)
    return merged


def total_minutes(intervals: Iterable[Interval]) -> int:
    return sum(item.minutes() for item in merge_intervals(intervals))


def clip_intervals(
    intervals: Iterable[Interval], bounds: Iterable[Interval]
) -> list[Interval]:
    result: list[Interval] = []
    for item in intervals:
        for bound in bounds:
            overlap = item.intersection(bound)
            if overlap is not None:
                result.append(overlap)
    return merge_intervals(result)


def split_by_cuts(
    base: Iterable[Interval], cuts: Iterable[Interval]
) -> tuple[list[Interval], list[Interval]]:
    """把 base 分成 (未被 cuts 覆盖, 被 cuts 覆盖) 两部分。"""

    base_list = merge_intervals(base)
    cut_list = merge_intervals(cuts)
    kept: list[Interval] = []
    for item in base_list:
        kept.extend(subtract_intervals(item, cut_list))
    kept = merge_intervals(kept)
    covered: list[Interval] = []
    for item in base_list:
        for cut in cut_list:
            overlap = item.intersection(cut)
            if overlap is not None:
                covered.append(overlap)
    return kept, merge_intervals(covered)


def period_of(day: date) -> tuple[date, date]:
    """返回 (期初营业日, 期末营业日)；期末含当日。"""

    offset = (day - PERIOD_ANCHOR).days
    index = offset // PERIOD_DAYS
    start = PERIOD_ANCHOR + timedelta(days=index * PERIOD_DAYS)
    end = start + timedelta(days=PERIOD_DAYS - 1)
    return start, end


def period_label(day: date) -> str:
    start, end = period_of(day)
    return f"{start.isoformat()}~{end.isoformat()}"


def start_of_day(day: date, tz: timezone = DEFAULT_TZ) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=tz).astimezone(timezone.utc)


def next_period(period_start: date) -> tuple[date, date]:
    start = period_start + timedelta(days=PERIOD_DAYS)
    return start, start + timedelta(days=PERIOD_DAYS - 1)
