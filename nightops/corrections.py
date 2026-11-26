"""核对与更正：只改争议范围，不改其他事实。

协调员核实申诉后，用一条更正记录覆盖某个具体范围：

* ``attendance``：把某班某区间强制记为已确认或从确认中剔除；
* ``switch``：修正一次换手的实际时刻（交班时刻申诉）；
* ``delivery``：补记一次被漏掉的送达（未计入的配送）。

更正必须带原申诉号、协调员和理由。同一段工时或计件只会在纠正后
各出现一次，不会叠加。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .exchange import Record
from .timeutil import Interval, TimeError, parse_time, to_utc


class CorrectionError(ValueError):
    """更正记录不符合约定。"""


KINDS = ("attendance", "switch", "delivery")


@dataclass(frozen=True)
class Correction:
    correction: str
    kind: str
    appeal: str | None
    reason: str
    coordinator: str
    created_at: datetime
    shift: str | None = None
    move: str | None = None
    parcel: str | None = None
    interval: Interval | None = None
    status: str | None = None  # attendance: confirmed / excluded
    occurred_at: datetime | None = None  # switch / delivery
    rider: str | None = None  # delivery
    proof: tuple[str, ...] = ()
    source_index: int = -1

    def describe(self) -> dict:
        data = {
            "correction": self.correction,
            "kind": self.kind,
            "appeal": self.appeal,
            "reason": self.reason,
            "coordinator": self.coordinator,
            "created_at": to_utc(self.created_at).isoformat(),
            "shift": self.shift,
            "move": self.move,
            "parcel": self.parcel,
            "status": self.status,
            "rider": self.rider,
        }
        if self.interval:
            data["interval"] = self.interval.describe()
        if self.occurred_at:
            data["occurred_at"] = to_utc(self.occurred_at).isoformat()
        if self.proof:
            data["proof"] = list(self.proof)
        return data


def read_corrections(rows: Iterable[Record]) -> list[Correction]:
    result: list[Correction] = []
    for index, row in enumerate(rows):
        for key in ("correction", "kind", "reason", "coordinator", "created_at"):
            if key not in row:
                raise CorrectionError(f"corrections[{index}] 缺少 {key}")
        kind = row["kind"]
        if kind not in KINDS:
            raise CorrectionError(f"corrections[{index}] 未知 kind: {kind}")
        try:
            created_at = to_utc(parse_time(row["created_at"]))
            interval = None
            if row.get("from") is not None or row.get("until") is not None:
                if not row.get("from") or not row.get("until"):
                    raise CorrectionError(
                        f"corrections[{index}] 区间必须同时给出 from 与 until"
                    )
                interval = Interval(parse_time(row["from"]), parse_time(row["until"]))
            occurred = (
                to_utc(parse_time(row["occurred_at"]))
                if row.get("occurred_at")
                else None
            )
        except TimeError as exc:
            raise CorrectionError(str(exc)) from exc
        if kind == "attendance":
            if not row.get("shift") or interval is None:
                raise CorrectionError(
                    f"corrections[{index}] attendance 更正需要 shift 与区间"
                )
            if row.get("status") not in ("confirmed", "excluded"):
                raise CorrectionError(
                    f"corrections[{index}] attendance 更正 status 必须是 confirmed/excluded"
                )
        if kind == "switch" and (not row.get("move") or occurred is None):
            raise CorrectionError(
                f"corrections[{index}] switch 更正需要 move 与 occurred_at"
            )
        if kind == "delivery" and (
            not row.get("parcel") or not row.get("rider") or occurred is None
        ):
            raise CorrectionError(
                f"corrections[{index}] delivery 更正需要 parcel、rider 与 occurred_at"
            )
        result.append(
            Correction(
                correction=str(row["correction"]),
                kind=kind,
                appeal=str(row["appeal"]) if row.get("appeal") else None,
                reason=str(row["reason"]),
                coordinator=str(row["coordinator"]),
                created_at=created_at,
                shift=str(row["shift"]) if row.get("shift") else None,
                move=str(row["move"]) if row.get("move") else None,
                parcel=str(row["parcel"]) if row.get("parcel") else None,
                interval=interval,
                status=row.get("status"),
                occurred_at=occurred,
                rider=str(row["rider"]) if row.get("rider") else None,
                proof=tuple(str(p) for p in row.get("proof", [])),
                source_index=index,
            )
        )
    return result
