"""在岗证据：签到、签退与终端钟差。

attendance 每行是一条终端证据，`earliest`/`latest` 是设备允许的实际
时刻范围。签到和签退各取 [earliest, latest]。缺少签到或签退、同一
事件被重复补传、或证据落在班次之外时，保留核对，不猜测。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .exchange import Record
from .timeutil import TimeError, parse_time, to_utc


class AttendanceError(ValueError):
    """签到证据不符合约定。"""


@dataclass(frozen=True)
class AttendanceEvidence:
    event: str
    shift: str
    device: str
    kind: str  # in / out
    earliest: datetime
    latest: datetime
    source_index: int

    @property
    def is_exact(self) -> bool:
        return self.earliest == self.latest

    @property
    def width_seconds(self) -> float:
        return (self.latest - self.earliest).total_seconds()

    def describe(self) -> dict:
        return {
            "event": self.event,
            "shift": self.shift,
            "device": self.device,
            "kind": self.kind,
            "earliest": self.earliest.isoformat(),
            "latest": self.latest.isoformat(),
            "exact": self.is_exact,
            "source_index": self.source_index,
        }


def read_attendance(rows: Iterable[Record]) -> list[AttendanceEvidence]:
    result: list[AttendanceEvidence] = []
    for index, row in enumerate(rows):
        for key in ("event", "shift", "kind", "earliest", "latest"):
            if key not in row:
                raise AttendanceError(f"attendance[{index}] 缺少 {key}")
        kind = row["kind"]
        if kind not in ("in", "out"):
            raise AttendanceError(f"attendance[{index}] kind 必须是 in/out")
        try:
            earliest = parse_time(row["earliest"])
            latest = parse_time(row["latest"])
        except TimeError as exc:
            raise AttendanceError(str(exc)) from exc
        if latest < earliest:
            raise AttendanceError(f"attendance[{index}] latest 早于 earliest")
        result.append(
            AttendanceEvidence(
                event=str(row["event"]),
                shift=str(row["shift"]),
                device=str(row.get("device", "")),
                kind=kind,  # type: ignore[arg-type]
                earliest=to_utc(earliest),
                latest=to_utc(latest),
                source_index=index,
            )
        )
    return result


def group_by_shift(
    evidence: Iterable[AttendanceEvidence],
) -> dict[str, list[AttendanceEvidence]]:
    grouped: dict[str, list[AttendanceEvidence]] = {}
    for item in evidence:
        grouped.setdefault(item.shift, []).append(item)
    return grouped
