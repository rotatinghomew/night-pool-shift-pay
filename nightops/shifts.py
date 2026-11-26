"""班次、邀请、修订与骑手值守承诺。

规则摘要：

* 一个骑手在同一时段只能对一个站形成值守承诺；后到/后接受的重叠安排
  只取得尚未被承诺覆盖的部分，冲突本身留作可解释记录。
* 站长的修订只对尚未出现有效在岗凭证的安排生效；已签到/已签退的
  安排不能被单方改写，只能在核对程序中调整争议区间。
* 承诺区间是半开区间 [from, until)。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Iterable

from .attendance import AttendanceEvidence
from .exchange import Record
from .timeutil import (
    DEFAULT_TZ,
    Interval,
    TimeError,
    business_day,
    format_time,
    parse_time,
    subtract_intervals,
    to_utc,
)


class ShiftError(ValueError):
    """班次不符合约定。"""


@dataclass(frozen=True)
class Shift:
    shift: str
    station: str
    rider: str
    starts: datetime
    until: datetime
    source_index: int = -1
    revision: int = 0
    origin_shift: str | None = None
    cancelled: bool = False
    accepted: bool | None = None
    accepted_at: datetime | None = None
    rate: str | None = None

    @property
    def interval(self) -> Interval:
        return Interval(self.starts, self.until)

    @property
    def business_day(self):
        return business_day(self.starts, DEFAULT_TZ)

    @property
    def root(self) -> str:
        return self.origin_shift or self.shift

    def covers(self, moment: datetime) -> bool:
        return self.interval.contains(moment)

    def describe(self) -> dict:
        return {
            "shift": self.shift,
            "station": self.station,
            "rider": self.rider,
            "from": format_time(self.starts),
            "until": format_time(self.until),
            "revision": self.revision,
            "origin_shift": self.origin_shift,
            "cancelled": self.cancelled,
            "accepted": self.accepted,
            "accepted_at": format_time(self.accepted_at) if self.accepted_at else None,
            "rate": self.rate,
        }


def _read_shift_row(row: Record, index: int) -> Shift:
    for key in ("shift", "station", "rider", "from", "until"):
        if key not in row:
            raise ShiftError(f"shifts[{index}] 缺少 {key}")
    try:
        starts = to_utc(parse_time(row["from"]))
        until = to_utc(parse_time(row["until"]))
        accepted_at = (
            to_utc(parse_time(row["accepted_at"])) if row.get("accepted_at") else None
        )
    except TimeError as exc:
        raise ShiftError(str(exc)) from exc
    if starts >= until:
        raise ShiftError(f"shifts[{index}] from 必须早于 until")
    return Shift(
        shift=str(row["shift"]),
        station=str(row["station"]),
        rider=str(row["rider"]),
        starts=starts,
        until=until,
        source_index=index,
        revision=int(row.get("revision", 0)),
        origin_shift=str(row["origin_shift"]) if row.get("origin_shift") else None,
        cancelled=bool(row.get("cancelled", False)),
        accepted=row.get("accepted") if isinstance(row.get("accepted"), bool) else None,
        accepted_at=accepted_at,
        rate=str(row["rate"]) if row.get("rate") else None,
    )


def read_shifts(rows: Iterable[Record]) -> list[Shift]:
    shifts: list[Shift] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        shift = _read_shift_row(row, index)
        if shift.shift in seen:
            raise ShiftError(f"班次编号重复: {shift.shift}")
        seen.add(shift.shift)
        shifts.append(shift)
    return shifts


def evidence_for_shift(
    shift: Shift,
    evidence: Iterable[AttendanceEvidence],
) -> list[AttendanceEvidence]:
    """该班（含修订链）的全部在岗证据。"""

    root = shift.root
    return [item for item in evidence if item.shift == root or item.shift == shift.shift]


def evidence_confirms(
    shift: Shift,
    evidence: Iterable[AttendanceEvidence],
) -> bool:
    """是否已出现落在该班适用时段内的签到证据。"""

    for item in evidence_for_shift(shift, evidence):
        if item.kind != "in":
            continue
        # 证据窗口 [earliest, latest] 与半开班次 [starts, until) 相交；
        # 恰好在开始时刻的签到也算已到岗。
        if item.earliest < shift.until and item.latest >= shift.starts:
            return True
    return False


@dataclass
class RevisionResult:
    effective: list[Shift]
    rejected: list[dict] = field(default_factory=list)


def effective_shifts(
    shifts: Iterable[Shift],
    evidence: Iterable[AttendanceEvidence] = (),
) -> RevisionResult:
    """按修订链还原当前生效的班次。"""

    evidence_list = list(evidence)
    chains: dict[str, list[Shift]] = {}
    order: list[str] = []
    for shift in shifts:
        root = shift.root
        if root not in chains:
            chains[root] = []
            order.append(root)
        chains[root].append(shift)

    effective: list[Shift] = []
    rejected: list[dict] = []
    for root in order:
        chain = sorted(chains[root], key=lambda s: (s.revision, s.source_index))
        chosen = chain[0]
        for candidate in chain[1:]:
            if evidence_confirms(chosen, evidence_list):
                rejected.append(
                    {
                        "shift": candidate.shift,
                        "origin_shift": root,
                        "reason": "原安排已出现在岗凭证，改班/取消不生效",
                        "attempted": candidate.describe(),
                    }
                )
                continue
            chosen = candidate
        effective.append(chosen)
    return RevisionResult(effective=effective, rejected=rejected)


@dataclass(frozen=True)
class Commitment:
    """骑手对某一站、某个班、在特定区间内的唯一值守承诺。"""

    rider: str
    station: str
    shift: str
    interval: Interval
    accepted_at: datetime

    def describe(self) -> dict:
        return {
            "rider": self.rider,
            "station": self.station,
            "shift": self.shift,
            **self.interval.describe(),
            "accepted_at": format_time(self.accepted_at),
        }


@dataclass
class CommitmentBook:
    by_rider: dict[str, list[Commitment]] = field(default_factory=dict)
    by_shift: dict[str, list[Commitment]] = field(default_factory=dict)
    overlap_rejections: list[dict] = field(default_factory=list)
    declined: list[str] = field(default_factory=list)

    def add(self, commitment: Commitment) -> list[Commitment]:
        """加入承诺，返回实际取得的非重叠区间（可能为空）。"""

        existing = self.by_rider.setdefault(commitment.rider, [])
        overlapping = [c for c in existing if c.interval.overlaps(commitment.interval)]
        pieces = subtract_intervals(
            commitment.interval, [c.interval for c in overlapping]
        )
        if overlapping:
            self.overlap_rejections.append(
                {
                    "rider": commitment.rider,
                    "shift": commitment.shift,
                    "station": commitment.station,
                    "requested": {
                        "station": commitment.station,
                        "shift": commitment.shift,
                        **commitment.interval.describe(),
                    },
                    "conflicts": [c.describe() for c in overlapping],
                    "granted": [p.describe() for p in pieces],
                    "reason": "同一时段只能有一个站取得该骑手的值守承诺",
                }
            )
        granted: list[Commitment] = []
        for piece in pieces:
            item = Commitment(
                rider=commitment.rider,
                station=commitment.station,
                shift=commitment.shift,
                interval=piece,
                accepted_at=commitment.accepted_at,
            )
            existing.append(item)
            self.by_shift.setdefault(commitment.shift, []).append(item)
            granted.append(item)
        return granted

    def segments(self, shift: str) -> list[Interval]:
        return [c.interval for c in self.by_shift.get(shift, [])]

    def has(self, rider: str, station: str, moment: datetime) -> bool:
        for commitment in self.by_rider.get(rider, []):
            if commitment.station == station and commitment.interval.contains(moment):
                return True
        return False

    def commitment_at(self, rider: str, moment: datetime) -> Commitment | None:
        for commitment in self.by_rider.get(rider, []):
            if commitment.interval.contains(moment):
                return commitment
        return None

    def for_rider(self, rider: str) -> list[Commitment]:
        return list(self.by_rider.get(rider, []))

    def describe(self) -> dict:
        return {
            "commitments": [
                c.describe()
                for rider in sorted(self.by_rider)
                for c in sorted(self.by_rider[rider], key=lambda x: x.interval.start)
            ],
            "overlap_rejections": list(self.overlap_rejections),
            "declined": list(self.declined),
        }


def acceptance_order(shift: Shift) -> tuple:
    return (
        shift.accepted_at or shift.starts,
        shift.starts,
        shift.source_index,
    )


def build_commitments(shifts: Iterable[Shift]) -> CommitmentBook:
    """为已接受的班建立唯一承诺。

    `accepted is False` 的邀请不建立承诺；未提供 acceptance 字段时按已
    接受处理（样本即如此）。重叠时先接受/先开始的安排先取得承诺。
    """

    book = CommitmentBook()
    candidates: list[Shift] = []
    for shift in shifts:
        if shift.cancelled:
            continue
        if shift.accepted is False:
            book.declined.append(shift.shift)
            continue
        candidates.append(shift)
    for shift in sorted(candidates, key=acceptance_order):
        book.add(
            Commitment(
                rider=shift.rider,
                station=shift.station,
                shift=shift.shift,
                interval=shift.interval,
                accepted_at=shift.accepted_at or shift.starts,
            )
        )
    return book


def revise(
    shift: Shift,
    *,
    new_id: str,
    station: str | None = None,
    rider: str | None = None,
    starts: datetime | None = None,
    until: datetime | None = None,
    cancelled: bool = False,
) -> Shift:
    """站长生成一条修订（是否生效由 effective_shifts 判断）。"""

    return replace(
        shift,
        shift=new_id,
        station=station if station is not None else shift.station,
        rider=rider if rider is not None else shift.rider,
        starts=to_utc(starts) if starts is not None else shift.starts,
        until=to_utc(until) if until is not None else shift.until,
        revision=shift.revision + 1,
        origin_shift=shift.root,
        cancelled=cancelled,
        accepted=None,
        accepted_at=None,
    )
