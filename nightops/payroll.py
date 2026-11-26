"""计薪：保底、计件、待核款、签发与更正。

规则：

* 每个班以已确认在岗工时折出的保底，与该班已确认计件之和比较，取高；
  不是先加总再取高。
* 跨午夜的班归开始营业日；计件记给骑手承接此单时的有效承诺班。
* 未确认的工时或计件不计入实发，只列为暂不能发的款并说明原因。
* 已签发的薪资保存当时依据；更正形成独立差额，关联原签发号，列在下一期。
* 未签发范围按新事实重新计算。
* 双周工资期以周一为起点，可配置锚点（默认 2026-01-05）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

from .ledger import Ledger, PieceShare
from .rates import Rate
from .timeutil import PERIOD_ANCHOR, next_period, period_of, to_utc


CENTS = "cent"


def cents(value: int) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}{value // 100}.{value % 100:02d}"


@dataclass
class ShiftPay:
    shift: str
    rider: str
    station: str
    business_day: date
    period_start: date
    period_end: date
    status: str
    rate: str | None
    confirmed_minutes: int
    pending_minutes: int
    floor_confirmed: int
    floor_possible: int
    pieces_confirmed: int
    pieces_pending: int
    base_pay: int
    potential_pay: int
    holdback: int
    confirmed_lines: list[dict] = field(default_factory=list)
    pending_lines: list[dict] = field(default_factory=list)
    held: list[dict] = field(default_factory=list)
    flags: list[dict] = field(default_factory=list)

    def describe(self) -> dict:
        return {
            "shift": self.shift,
            "rider": self.rider,
            "station": self.station,
            "business_day": self.business_day.isoformat(),
            "period": f"{self.period_start.isoformat()}~{self.period_end.isoformat()}",
            "status": self.status,
            "rate": self.rate,
            "confirmed_minutes": self.confirmed_minutes,
            "pending_minutes": self.pending_minutes,
            "floor_confirmed_cents": self.floor_confirmed,
            "floor_possible_cents": self.floor_possible,
            "pieces_confirmed_cents": self.pieces_confirmed,
            "pieces_pending_cents": self.pieces_pending,
            "base_pay_cents": self.base_pay,
            "potential_pay_cents": self.potential_pay,
            "holdback_cents": self.holdback,
            "confirmed_lines": list(self.confirmed_lines),
            "pending_lines": list(self.pending_lines),
            "held": list(self.held),
            "flags": list(self.flags),
        }


@dataclass
class HoldSet:
    """未决申诉造成的暂缓范围。"""

    shifts: set[str] = field(default_factory=set)
    parcels: set[str] = field(default_factory=set)
    reasons: list[dict] = field(default_factory=list)

    def shift_held(self, shift_id: str) -> dict | None:
        for item in self.reasons:
            if shift_id in item.get("shifts", ()):
                return item
        return None

    def parcel_held(self, parcel: str) -> dict | None:
        for item in self.reasons:
            if parcel in item.get("parcels", ()):
                return item
        return None


class PayrollEngine:
    def __init__(
        self,
        ledger: Ledger,
        *,
        default_rate: str | None = None,
        period_anchor: date = PERIOD_ANCHOR,
        holds: HoldSet | None = None,
    ):
        self.ledger = ledger
        self.default_rate = default_rate
        self.period_anchor = period_anchor
        self.holds = holds or HoldSet()

    # ------------------------------------------------------------------ 基本
    def rate_for(self, shift_id: str) -> Rate | None:
        rate = self.ledger.rate_for_shift(shift_id)
        if rate is not None:
            return rate
        if self.default_rate and self.default_rate in self.ledger.rates:
            return self.ledger.rates[self.default_rate]
        if len(self.ledger.rates) == 1:
            return next(iter(self.ledger.rates.values()))
        return None

    def period_for(self, day: date) -> tuple[date, date]:
        return period_of(day)

    # ------------------------------------------------------------------ 班薪
    def shift_pay(self, shift_id: str) -> ShiftPay:
        facts = self.ledger.shift_facts(shift_id)
        shift = facts.shift
        rate = self.rate_for(shift_id)
        start, end = self.period_for(shift.business_day)
        shares = self.ledger.pieces_for_shift(shift_id)

        held_shift = self.holds.shift_held(shift_id)
        confirmed_pieces: list[PieceShare] = []
        pending_pieces: list[PieceShare] = []
        held_pieces: list[PieceShare] = []
        for share in shares:
            if self.holds.parcel_held(share.parcel):
                held_pieces.append(share)
                pending_pieces.append(share)
            elif share.status == "confirmed":
                confirmed_pieces.append(share)
            else:
                pending_pieces.append(share)

        confirmed_minutes = facts.confirmed_minutes
        pending_minutes = facts.pending_minutes
        if held_shift is not None:
            # 在岗证据被申诉：保底整体暂缓，计件仍按各自证据
            confirmed_minutes, pending_minutes = 0, (
                facts.confirmed_minutes + facts.pending_minutes
            )

        rate_id = rate.rate if rate else None
        floor_confirmed = (
            rate.floor_for_minutes(confirmed_minutes) if rate else 0
        )
        floor_possible = (
            rate.floor_for_minutes(confirmed_minutes + pending_minutes)
            if rate
            else 0
        )
        pieces_confirmed = sum(share.amount_cents for share in confirmed_pieces)
        pieces_pending = sum(share.amount_cents for share in pending_pieces)
        base_pay = max(floor_confirmed, pieces_confirmed)
        if facts.status == "unassigned":
            # 没有有效值守承诺的班不计保底，也不能把它的“可能保底”
            # 当成待发款；否则同人重叠的两班会显示两份保底。
            floor_possible = floor_confirmed
            potential_pay = base_pay
            holdback = 0
        else:
            potential_pay = max(floor_possible, pieces_confirmed + pieces_pending)
            holdback = max(0, potential_pay - base_pay)

        held: list[dict] = []
        if held_shift is not None:
            held.append(
                {
                    "scope": "shift",
                    "shift": shift_id,
                    "appeal": held_shift.get("appeal"),
                    "reason": held_shift.get("reason", "在岗证据待核"),
                }
            )
        for share in held_pieces:
            held.append(
                {
                    "scope": "piece",
                    "parcel": share.parcel,
                    "component": share.component,
                    "rider": share.rider,
                    "amount_cents": share.amount_cents,
                    "appeal": (self.holds.parcel_held(share.parcel) or {}).get("appeal"),
                    "reason": (self.holds.parcel_held(share.parcel) or {}).get(
                        "reason", "计件归属待核"
                    ),
                }
            )

        flags = list(facts.flags)
        if rate is None:
            flags.append(
                {
                    "code": "missing_rate",
                    "detail": "该班没有可适用的费率，保底与计件均暂不能确认",
                }
            )
        if facts.status == "unassigned":
            flags.append(
                {
                    "code": "unassigned_shift",
                    "detail": "该班没有取得有效值守承诺，不能计保底",
                }
            )

        return ShiftPay(
            shift=shift_id,
            rider=shift.rider,
            station=shift.station,
            business_day=shift.business_day,
            period_start=start,
            period_end=end,
            status=facts.status,
            rate=rate_id,
            confirmed_minutes=confirmed_minutes,
            pending_minutes=pending_minutes,
            floor_confirmed=floor_confirmed,
            floor_possible=floor_possible,
            pieces_confirmed=pieces_confirmed,
            pieces_pending=pieces_pending,
            base_pay=base_pay,
            potential_pay=potential_pay,
            holdback=holdback,
            confirmed_lines=[
                {**share.describe(), "payable": True} for share in confirmed_pieces
            ],
            pending_lines=[
                {**share.describe(), "payable": False} for share in pending_pieces
            ],
            held=held,
            flags=flags,
        )

    # ------------------------------------------------------------------ 期薪
    def statements(
        self,
        period_start: date,
        riders: Iterable[str] | None = None,
    ) -> list["Statement"]:
        start, end = period_of(period_start)
        rider_set = set(riders or [])
        all_riders = {shift.rider for shift in self.ledger.shifts}
        if rider_set:
            all_riders &= rider_set
        result: list[Statement] = []
        for rider in sorted(all_riders):
            result.append(self.statement(rider, start))
        return result

    def statement(self, rider: str, period_start: date) -> "Statement":
        start, end = period_of(period_start)
        shift_pays: list[ShiftPay] = []
        for facts in self.ledger.facts.values():
            if facts.shift.rider != rider:
                continue
            if not (start <= facts.shift.business_day <= end):
                continue
            shift_pays.append(self.shift_pay(facts.shift.shift))

        confirmed_total = sum(pay.base_pay for pay in shift_pays)
        pending_total = sum(pay.holdback for pay in shift_pays)
        pending_items: list[dict] = []
        for pay in shift_pays:
            if pay.holdback:
                pending_items.append(
                    {
                        "shift": pay.shift,
                        "amount_cents": pay.holdback,
                        "status": pay.status,
                        "reasons": [
                            flag["detail"] for flag in pay.flags if "detail" in flag
                        ]
                        + [line.get("reason") for line in pay.pending_lines if line.get("reason")],
                        "held": pay.held,
                    }
                )
        unattributed = [
            share
            for share in self.ledger.pieces_for_rider(rider)
            if share.status == "pending" and share.shift is None
        ]
        for share in unattributed:
            pending_total += share.amount_cents
            pending_items.append(
                {
                    "shift": None,
                    "parcel": share.parcel,
                    "amount_cents": share.amount_cents,
                    "reasons": [share.reason or "计件无法归入有效承诺班"],
                    "component": share.component,
                }
            )
        return Statement(
            rider=rider,
            period_start=start,
            period_end=end,
            shift_pays=shift_pays,
            confirmed_total=confirmed_total,
            pending_total=pending_total,
            pending_items=pending_items,
        )


@dataclass
class Statement:
    rider: str
    period_start: date
    period_end: date
    shift_pays: list[ShiftPay]
    confirmed_total: int
    pending_total: int
    pending_items: list[dict] = field(default_factory=list)
    issuance: dict | None = None
    adjustments: list[dict] = field(default_factory=list)

    @property
    def period(self) -> str:
        return f"{self.period_start.isoformat()}~{self.period_end.isoformat()}"

    @property
    def adjustments_total(self) -> int:
        return sum(item["amount_cents"] for item in self.adjustments)

    @property
    def payable_total(self) -> int:
        if self.issuance is not None:
            return self.issuance["total_cents"] + self.adjustments_total
        return self.confirmed_total + self.adjustments_total

    def describe(self) -> dict:
        return {
            "rider": self.rider,
            "period": self.period,
            "issued": self.issuance is not None,
            "issuance": self.issuance,
            "confirmed_total_cents": self.confirmed_total,
            "confirmed_total": cents(self.confirmed_total),
            "pending_total_cents": self.pending_total,
            "pending_note": "待核金额按班列示，合计为上限，实际以核实后的归属为准",
            "pending_items": self.pending_items,
            "adjustments": self.adjustments,
            "adjustments_total_cents": self.adjustments_total,
            "payable_total_cents": self.payable_total,
            "shifts": [pay.describe() for pay in self.shift_pays],
        }


def issued_basis(issuance: dict, shift_id: str) -> int | None:
    for line in issuance.get("lines", []):
        if line["shift"] == shift_id:
            return int(line["base_pay_cents"])
    return None


def build_correction(
    *,
    correction_id: str,
    original: dict,
    new_ledger: Ledger,
    affected_shifts: Iterable[str],
    prior_corrections: Iterable[dict] = (),
    reason: str,
    created_at: datetime,
    created_by: str,
    scope: dict | None = None,
) -> dict | None:
    """对已签发薪水生成独立差额；返回 None 表示没有变化。"""

    shifts = sorted(set(affected_shifts))
    if not shifts:
        return None
    engine = PayrollEngine(new_ledger)
    lines: list[dict] = []
    total_delta = 0
    for shift_id in shifts:
        old_basis = _latest_basis(original, prior_corrections, shift_id)
        new_pay = engine.shift_pay(shift_id)
        new_basis = new_pay.base_pay
        delta = new_basis - old_basis
        total_delta += delta
        lines.append(
            {
                "shift": shift_id,
                "old_base_pay_cents": old_basis,
                "new_base_pay_cents": new_basis,
                "delta_cents": delta,
                "new_floor_cents": new_pay.floor_confirmed,
                "new_pieces_cents": new_pay.pieces_confirmed,
                "new_status": new_pay.status,
            }
        )
    if total_delta == 0 and all(line["old_base_pay_cents"] == line["new_base_pay_cents"] for line in lines):
        # 依据变化但金额未变时仍记录，便于查账
        pass
    start = _period_start(original["period"])
    nxt_start, nxt_end = next_period(start)
    return {
        "correction": correction_id,
        "issuance": original["issuance"],
        "rider": original["rider"],
        "original_period": original["period"],
        "scheduled_period": f"{nxt_start.isoformat()}~{nxt_end.isoformat()}",
        "scheduled_period_start": nxt_start.isoformat(),
        "amount_cents": total_delta,
        "lines": lines,
        "scope": scope or {"shifts": shifts},
        "reason": reason,
        "created_at": to_utc(created_at).isoformat(),
        "created_by": created_by,
    }


def _latest_basis(
    original: dict, prior_corrections: Iterable[dict], shift_id: str
) -> int:
    basis = issued_basis(original, shift_id) or 0
    for correction in prior_corrections:
        for line in correction.get("lines", []):
            if line["shift"] == shift_id:
                basis = int(line["new_base_pay_cents"])
    return basis


def _period_start(label: str) -> date:
    return date.fromisoformat(label.split("~")[0])


def apply_adjustments(
    statement: Statement,
    corrections: Iterable[dict],
    issuance: dict | None,
) -> Statement:
    """把本期应列示的更正差额挂到工资单上。"""

    statement.issuance = issuance
    statement.adjustments = [
        {
            "correction": item["correction"],
            "issuance": item["issuance"],
            "amount_cents": item["amount_cents"],
            "original_period": item["original_period"],
            "reason": item["reason"],
            "scope": item["scope"],
            "lines": item["lines"],
        }
        for item in corrections
    ]
    return statement
