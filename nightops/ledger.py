"""把交换资料解释成可复核的事实。

台账保留三类事实：

* 值守事实：班次、唯一承诺、签到/签退证据、可计保底的已确认与待核区间；
* 交接事实：包裹在谁手里、换手是否双方确认、收件人是否有本站有效资格；
* 计件事实：每一分钱计给谁、计入哪个班、是否因未知区间暂不能确认。

未知区间只覆盖受影响的部分；已能分清且不相交的工时与计件仍可确认。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from .attendance import AttendanceEvidence, read_attendance
from .corrections import Correction, CorrectionError, read_corrections
from .exchange import ExchangeData
from .handoffs import (
    Move,
    ParcelChain,
    PieceShare,
    build_chains,
    read_moves,
)
from .rates import Rate, read_rates
from .shifts import (
    CommitmentBook,
    Shift,
    build_commitments,
    effective_shifts,
    evidence_for_shift,
    read_shifts,
)
from .timeutil import (
    Interval,
    clip_intervals,
    format_time,
    merge_intervals,
    parse_time,
    total_minutes,
    to_utc,
)


@dataclass
class ShiftFacts:
    shift: Shift
    commitment: list[Interval] = field(default_factory=list)
    evidence: list[AttendanceEvidence] = field(default_factory=list)
    confirmed: list[Interval] = field(default_factory=list)
    pending: list[Interval] = field(default_factory=list)
    raw_possible: list[Interval] = field(default_factory=list)
    flags: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    continuation: list[Interval] = field(default_factory=list)
    status: str = "confirmed"

    @property
    def confirmed_minutes(self) -> int:
        return total_minutes(self.confirmed)

    @property
    def pending_minutes(self) -> int:
        return total_minutes(self.pending)

    @property
    def possible_minutes(self) -> int:
        return total_minutes(self.raw_possible)

    def add_flag(self, code: str, **details) -> None:
        self.flags.append({"code": code, **details})

    def describe(self) -> dict:
        return {
            **self.shift.describe(),
            "business_day": self.shift.business_day.isoformat(),
            "commitment": [
                {"from": format_time(i.start), "until": format_time(i.end)}
                for i in self.commitment
            ],
            "status": self.status,
            "confirmed_floor": _intervals(self.confirmed),
            "pending_floor": _intervals(self.pending),
            "confirmed_minutes": self.confirmed_minutes,
            "pending_minutes": self.pending_minutes,
            "continuation": _intervals(self.continuation),
            "flags": list(self.flags),
            "conflicts": list(self.conflicts),
            "evidence": [e.describe() for e in self.evidence],
        }


def _intervals(items: Iterable[Interval]) -> list[dict]:
    return [
        {"from": format_time(i.start), "until": format_time(i.end)}
        for i in merge_intervals(items)
    ]


def _window(rows: list[AttendanceEvidence]) -> tuple[datetime, datetime] | None:
    if not rows:
        return None
    return min(r.earliest for r in rows), max(r.latest for r in rows)


@dataclass
class Continuation:
    original_shift: str
    successor_shift: str
    interval: Interval
    proof: tuple[str, ...]

    @property
    def agreed(self) -> bool:
        """双方确认：原班编号与接班人（或其证明身份）都在 proof 中。"""

        return self.original_shift in self.proof and bool(self.successor_shift)


def read_continuations(rows: Iterable[dict]) -> list[Continuation]:
    """读取续守确认；同一对班次的多份确认按 proof 合并。"""

    merged: dict[tuple[str, str], Continuation] = {}
    order: list[tuple[str, str]] = []
    for index, row in enumerate(rows):
        for key in ("original_shift", "successor_shift", "from", "until"):
            if key not in row:
                raise ValueError(f"continuations[{index}] 缺少 {key}")
        original = str(row["original_shift"])
        successor = str(row["successor_shift"])
        interval = Interval(parse_time(row["from"]), parse_time(row["until"]))
        proofs = tuple(str(p) for p in row.get("proof", []))
        key = (original, successor)
        if key in merged:
            prior = merged[key]
            merged[key] = Continuation(
                original_shift=prior.original_shift,
                successor_shift=prior.successor_shift,
                interval=prior.interval,
                proof=tuple(sorted(set(prior.proof) | set(proofs))),
            )
        else:
            merged[key] = Continuation(
                original_shift=original,
                successor_shift=successor,
                interval=interval,
                proof=proofs,
            )
            order.append(key)
    return [merged[key] for key in order]


class Ledger:
    """一次交换资料（可叠加更正）解释后的事实集合。"""

    def __init__(
        self,
        data: ExchangeData,
        continuations: Iterable[dict] | None = None,
        corrections: Iterable[dict] | None = None,
    ):
        self.data = data
        self.rates: dict[str, Rate] = read_rates(data.rates)
        self.parcels: dict[str, dict] = {
            str(row["parcel"]): row for row in data.parcels if "parcel" in row
        }
        self.evidence = read_attendance(data.attendance)
        all_shifts = read_shifts(data.shifts)
        revisions = effective_shifts(all_shifts, self.evidence)
        self.shifts: list[Shift] = revisions.effective
        self.rejected_revisions: list[dict] = revisions.rejected
        self.commitments: CommitmentBook = build_commitments(self.shifts)
        self.continuations = read_continuations(
            list(continuations or []) + list(data.continuations)
        )
        self.corrections = read_corrections(
            list(corrections or []) + list(data.corrections)
        )
        self.facts: dict[str, ShiftFacts] = {}
        self.handover_gaps: list[dict] = []
        self.moves: list[Move] = read_moves(data.handoffs)
        self.corrections_applied: list[dict] = []
        self.corrections_rejected: list[dict] = []

        self._compute_attendance()
        self._compute_handover_gaps()
        self._apply_continuations()
        self._apply_same_rider_conflicts()
        self._apply_corrections()
        self.chains: list[ParcelChain] = build_chains(
            self._effective_moves(), self.parcels
        )
        self.pieces: list[PieceShare] = []
        self.parcel_disputes: list[dict] = []
        self._compute_pieces()
        self._finalize()

    # ------------------------------------------------------------- 基本访问
    def shift_facts(self, shift_id: str) -> ShiftFacts:
        try:
            return self.facts[shift_id]
        except KeyError as exc:
            raise KeyError(f"没有生效班次: {shift_id}") from exc

    def rate_for_shift(self, shift_id: str) -> Rate | None:
        facts = self.facts.get(shift_id)
        if facts is None:
            return None
        if facts.shift.rate and facts.shift.rate in self.rates:
            return self.rates[facts.shift.rate]
        used = {
            self.parcels.get(share.parcel, {}).get("rate")
            for share in self.pieces
            if share.shift == shift_id
        }
        used.discard(None)
        resolved = [self.rates[r] for r in used if r in self.rates]
        if len(resolved) == 1:
            return resolved[0]
        if facts.shift.rate:
            return self.rates.get(facts.shift.rate)
        if len(self.rates) == 1:
            return next(iter(self.rates.values()))
        return None

    # ---------------------------------------------------------------- 在岗
    def _compute_attendance(self) -> None:
        for shift in self.shifts:
            evidence = evidence_for_shift(shift, self.evidence)
            in_rows = [e for e in evidence if e.kind == "in"]
            out_rows = [e for e in evidence if e.kind == "out"]
            facts = ShiftFacts(
                shift=shift,
                commitment=self.commitments.segments(shift.shift),
                evidence=evidence,
            )

            in_win = _window(in_rows)
            out_win = _window(out_rows)

            possible_start = shift.starts
            possible_end = shift.until
            if in_win:
                possible_start = max(possible_start, in_win[0])
            if out_win:
                possible_end = min(possible_end, out_win[1])
            raw_possible: Interval | None = None
            if possible_start < possible_end:
                raw_possible = Interval(possible_start, possible_end)
                facts.raw_possible = [raw_possible]

            confirmed: list[Interval] = []
            if in_win and out_win:
                # 迟到最多到 in_win[1]，早退最早到 out_win[0]；这之间必在岗
                start = max(shift.starts, in_win[1])
                end = min(shift.until, out_win[0])
                if start < end:
                    confirmed = [Interval(start, end)]
                else:
                    facts.add_flag(
                        "in_out_contradiction",
                        detail="签到最晚时刻不早于签退最早时刻，无法确认在岗区间",
                    )
            elif in_win:
                facts.add_flag("missing_out", detail="固定站点班缺签退，保底暂不能确认")
            elif out_win:
                facts.add_flag("missing_in", detail="固定站点班缺签到，保底暂不能确认")
            else:
                facts.add_flag("missing_attendance", detail="固定站点班无签到与签退")

            if len(in_rows) > 1:
                facts.add_flag(
                    "multiple_in",
                    detail="同一班有多条签到证据，按最晚到达保守确认",
                    rows=[e.describe() for e in in_rows],
                )
            if len(out_rows) > 1:
                facts.add_flag(
                    "multiple_out",
                    detail="同一班有多条签退证据，按最早离开保守确认",
                    rows=[e.describe() for e in out_rows],
                )
            if in_win and in_win[0] < shift.starts:
                facts.add_flag(
                    "in_before_shift",
                    earliest=format_time(in_win[0]),
                    detail="签到范围起点早于班次，超出部分不计",
                )
            if out_win and out_win[1] > shift.until:
                facts.add_flag(
                    "out_after_shift",
                    latest=format_time(out_win[1]),
                    detail="签退范围终点晚于班次，超出部分不计",
                )

            # 只有承诺覆盖的时段才计保底
            committed = facts.commitment
            facts.confirmed = clip_intervals(confirmed, committed) if committed else []
            possible_clipped = (
                clip_intervals([raw_possible], committed)
                if committed and raw_possible is not None
                else []
            )
            pending: list[Interval] = []
            for piece in possible_clipped:
                remaining = [piece]
                for done in facts.confirmed:
                    remaining = [
                        part
                        for chunk in remaining
                        for part in chunk.subtract(done)
                    ]
                pending.extend(remaining)
            facts.pending = merge_intervals(pending)

            if not committed:
                facts.add_flag(
                    "no_commitment",
                    detail="该班没有取得该骑手在此时段的值守承诺，不计保底",
                )
            self.facts[shift.shift] = facts

    # ------------------------------------------------------------ 更正/申诉
    def _apply_corrections(self) -> None:
        for correction in self.corrections:
            try:
                if correction.kind == "attendance":
                    self._apply_attendance_correction(correction)
                elif correction.kind == "switch":
                    self._apply_switch_correction(correction)
                elif correction.kind == "delivery":
                    self._apply_delivery_correction(correction)
            except CorrectionError as exc:
                self.corrections_rejected.append(
                    {"correction": correction.correction, "reason": str(exc)}
                )

    def _apply_attendance_correction(self, correction: Correction) -> None:
        facts = self.facts.get(correction.shift or "")
        if facts is None:
            raise CorrectionError(f"更正指向不存在的班次: {correction.shift}")
        interval = correction.interval
        assert interval is not None
        if not facts.shift.interval.overlaps(interval):
            raise CorrectionError(
                f"更正的区间与班次 {correction.shift} 不相交"
            )
        if correction.status == "confirmed":
            facts.confirmed = merge_intervals(list(facts.confirmed) + [interval])
            facts.pending = _subtract_all(facts.pending, [interval])
        else:
            facts.confirmed = _subtract_all(facts.confirmed, [interval])
            facts.pending = merge_intervals(list(facts.pending) + [interval])
        facts.add_flag(
            "corrected",
            correction=correction.correction,
            appeal=correction.appeal,
            interval=interval.describe(),
            status=correction.status,
            detail=correction.reason,
        )
        self.corrections_applied.append(correction.describe())

    def _apply_switch_correction(self, correction: Correction) -> None:
        rows = [m for m in self.moves if m.move == correction.move]
        if not rows:
            raise CorrectionError(f"更正指向不存在的换手: {correction.move}")
        assert correction.occurred_at is not None
        # 同一次换手可能有两份确认行；一起改，避免一份旧时刻主导
        for move in rows:
            move.at = correction.occurred_at
            move.occurred_at = to_utc(correction.occurred_at).isoformat()
        self.corrections_applied.append(correction.describe())

    def _apply_delivery_correction(self, correction: Correction) -> None:
        parcel = correction.parcel or ""
        if parcel not in self.parcels:
            raise CorrectionError(f"更正指向未登记的包裹: {parcel}")
        existing = next(
            (m for m in self.moves if m.parcel == parcel and m.kind == "delivery"),
            None,
        )
        if existing is not None:
            # 已计入的送达只更正时刻，不重复计件
            assert correction.occurred_at is not None
            existing.at = correction.occurred_at
            existing.occurred_at = to_utc(correction.occurred_at).isoformat()
            existing.sender = correction.rider or existing.sender
            self.corrections_applied.append(correction.describe())
            return
        if correction.rider is None or correction.occurred_at is None:
            raise CorrectionError("补记送达需要 rider 与 occurred_at")
        pickup = next(
            (m for m in self.moves if m.parcel == parcel and m.kind == "pickup"),
            None,
        )
        if pickup is None:
            raise CorrectionError(f"包裹 {parcel} 没有揽收记录，不能补记送达")
        move_id = correction.move or f"correction-{correction.correction}"
        if any(m.move == move_id for m in self.moves):
            raise CorrectionError(f"补记送达的动作编号已存在: {move_id}")
        from .handoffs import Move as HandoffMove

        self.moves.append(
            HandoffMove(
                parcel=parcel,
                move=move_id,
                kind="delivery",
                sender=correction.rider,
                receiver="buyer",
                proof=correction.proof
                or (pickup.receiver, correction.rider),
                occurred_at=to_utc(correction.occurred_at).isoformat(),
                at=correction.occurred_at,
                source_index=10_000 + correction.source_index,
            )
        )
        self.corrections_applied.append(correction.describe())

    def _effective_moves(self) -> list[Move]:
        return sorted(self.moves, key=lambda m: (m.at, m.source_index))

    # ------------------------------------------------------------ 交班空档
    def _compute_handover_gaps(self) -> None:
        by_station: dict[str, list[Shift]] = {}
        for shift in self.shifts:
            by_station.setdefault(shift.station, []).append(shift)
        for station, shifts in by_station.items():
            ordered = sorted(shifts, key=lambda s: (s.starts, s.until, s.shift))
            for previous, successor in zip(ordered, ordered[1:]):
                if previous.rider == successor.rider:
                    continue
                in_rows = [
                    e
                    for e in evidence_for_shift(successor, self.evidence)
                    if e.kind == "in"
                ]
                if not in_rows:
                    continue
                latest_arrival = max(e.latest for e in in_rows)
                if latest_arrival <= previous.until:
                    continue
                out_rows = [
                    e
                    for e in evidence_for_shift(previous, self.evidence)
                    if e.kind == "out"
                ]
                # 原骑手最多值守到其签退允许的最晚时刻；交接责任只可能落在
                # [原班结束, min(接班人最晚到达, 原骑手最晚离开)]。
                original_limit = max(
                    (e.latest for e in out_rows), default=latest_arrival
                )
                gap_end = min(latest_arrival, original_limit)
                if gap_end <= previous.until:
                    continue
                gap = Interval(previous.until, gap_end)
                record = {
                    "station": station,
                    "original_shift": previous.shift,
                    "original_rider": previous.rider,
                    "successor_shift": successor.shift,
                    "successor_rider": successor.rider,
                    **gap.describe(),
                    "latest_successor_arrival": format_time(latest_arrival),
                    "original_out_limit": format_time(original_limit),
                    "status": "pending_continuation",
                    "reason": "接班人到班时刻可能晚于原班结束，需原骑手确认是否继续值守",
                }
                self.handover_gaps.append(record)
                facts = self.facts[previous.shift]
                facts.pending = merge_intervals(list(facts.pending) + [gap])
                facts.add_flag(
                    "successor_arrival_uncertain",
                    successor=successor.shift,
                    latest_arrival=format_time(latest_arrival),
                    interval=gap.describe(),
                    detail="接班人到班时刻可能晚于本班结束；未确认续守前该段暂不能确认",
                )
                self.facts[successor.shift].add_flag(
                    "successor_late_possible",
                    interval=gap.describe(),
                    detail="到班时刻范围跨过交班点，跨点部分暂不能确认",
                )

    def _apply_continuations(self) -> None:
        for record in self.continuations:
            original = self.facts.get(record.original_shift)
            successor = self.facts.get(record.successor_shift)
            if original is None or successor is None:
                continue
            if not record.agreed:
                original.add_flag(
                    "continuation_unconfirmed",
                    detail="续守缺少双方确认，空档仍待核",
                    interval=record.interval.describe(),
                )
                continue
            # 同一承诺区间只能有一位责任人：归原骑手
            original.confirmed = merge_intervals(
                list(original.confirmed) + [record.interval]
            )
            original.continuation.append(record.interval)
            original.pending = _subtract_all(original.pending, [record.interval])
            successor.pending = merge_intervals(
                list(successor.pending) + [record.interval]
            )
            successor.confirmed = _subtract_all(successor.confirmed, [record.interval])
            detail = {
                "kind": "continuation",
                "interval": {"from": format_time(record.interval.start), "until": format_time(record.interval.end)},
                "responsible": original.shift.rider,
                "other": successor.shift.rider,
                "shifts": [original.shift.shift, successor.shift.shift],
            }
            original.conflicts.append({**detail, "responsibility": "this"})
            successor.conflicts.append({**detail, "responsibility": "other"})
            original.add_flag(
                "continuation_confirmed",
                detail="原骑手已同意继续值守，该段责任归原骑手",
                interval=record.interval.describe(),
            )
            successor.add_flag(
                "continuation_assigned_elsewhere",
                detail="该段已由原骑手继续值守，不重复计保底",
                interval=record.interval.describe(),
            )
            for gap in self.handover_gaps:
                if (
                    gap["original_shift"] == original.shift.shift
                    and gap["successor_shift"] == successor.shift.shift
                ):
                    gap["status"] = "resolved_by_continuation"

    # ------------------------------------------------------ 同人不同站冲突
    def _apply_same_rider_conflicts(self) -> None:
        shifts = list(self.shifts)
        for index, first in enumerate(shifts):
            for second in shifts[index + 1 :]:
                if first.rider != second.rider or first.station == second.station:
                    continue
                a = self.facts[first.shift]
                b = self.facts[second.shift]
                if not a.raw_possible or not b.raw_possible:
                    continue
                a_possible = merge_intervals(a.raw_possible)
                b_possible = merge_intervals(b.raw_possible)
                overlap = _intersect_many(a_possible, b_possible)
                if not overlap:
                    continue
                detail = {
                    "kind": "same_rider_different_station",
                    "rider": first.rider,
                    "stations": [first.station, second.station],
                    "shifts": [first.shift, second.shift],
                    "interval": {
                        "from": format_time(overlap[0].start),
                        "until": format_time(overlap[-1].end),
                    },
                    "reason": "同一人在重叠时段出现在不同站点，重叠区间暂停结算",
                }
                for facts in (a, b):
                    facts.confirmed = _subtract_all(facts.confirmed, overlap)
                    facts.pending = merge_intervals(
                        list(facts.pending) + _intersect_many(facts.raw_possible, overlap)
                    )
                    facts.conflicts.append(detail)
                    facts.add_flag(
                        "same_rider_different_station",
                        detail=detail["reason"],
                        interval=detail["interval"],
                        shifts=detail["shifts"],
                    )

    # ------------------------------------------------------------------ 计件
    def _compute_pieces(self) -> None:
        for chain in self.chains:
            self.parcel_disputes.extend(chain.disputes)
            rate = self.rates.get(chain.rate)
            if rate is None:
                self.parcel_disputes.append(
                    {"parcel": chain.parcel, "reason": "缺少适用费率", "rate": chain.rate}
                )
                continue
            self.pieces.extend(self._pieces_for(chain, rate))

    def _pieces_for(self, chain: ParcelChain, rate: Rate) -> list[PieceShare]:
        if not chain.moves:
            return []
        pickup = chain.pickup
        if pickup is None:
            return []
        # 有一方确认但另一方未确认的换手：实物不移动，整单归属待核
        disputed = bool(chain.disputes) or any(
            not switch.confirmed for switch in chain.switches
        )
        if chain.returned and not chain.delivered:
            # 退回门店的未送达件不发计件
            return []
        if chain.delivered and chain.returned:
            disputed = True

        custody = pickup.receiver
        # 每位骑手承接此单时的班；跨班尾段仍归这里
        accepted_shift = {pickup.receiver: self._active_shift(pickup.receiver, pickup.at)}
        shares: list[PieceShare] = []
        confirmed_switches = []
        broken = False

        for switch in chain.switches:
            if not switch.confirmed:
                continue
            if switch.sender != custody:
                broken = True
                self.parcel_disputes.append(
                    {
                        "parcel": chain.parcel,
                        "move": switch.move,
                        "reason": "换手时交件人不是当前持件人",
                        "custody": custody,
                        "sender": switch.sender,
                        "occurred_at": switch.occurred_at,
                    }
                )
                continue
            # 交接站点由当前持件人承接此单的班决定；收件人须在同一站
            # 持有已生效的接班资格。
            handover_station = switch.station or self._station_of(
                accepted_shift.get(switch.sender)
            )
            if not self._qualified(switch.receiver, switch.at, handover_station):
                broken = True
                self.parcel_disputes.append(
                    {
                        "parcel": chain.parcel,
                        "move": switch.move,
                        "reason": "收件人没有本站已生效的接班资格",
                        "receiver": switch.receiver,
                        "station": handover_station,
                        "occurred_at": switch.occurred_at,
                    }
                )
                continue
            custody = switch.receiver
            accepted_shift[switch.receiver] = self._active_shift(
                switch.receiver, switch.at
            )
            confirmed_switches.append(switch)

        delivered = chain.delivered and chain.delivery is not None
        if delivered:
            delivery = chain.delivery
            assert delivery is not None
            deliverer = delivery.sender
            custody_intact = deliverer == custody
            if not custody_intact:
                broken = True
                self.parcel_disputes.append(
                    {
                        "parcel": chain.parcel,
                        "move": delivery.move,
                        "reason": "送达人不是当前持件人",
                        "custody": custody,
                        "deliverer": deliverer,
                        "occurred_at": delivery.occurred_at,
                    }
                )
        else:
            delivery = None
            deliverer = None
            custody_intact = False

        base_confirmed = delivered and custody_intact and not disputed and not broken
        if not delivered:
            status = "pending"
            reason = "包裹尚未送达，计件暂不确认"
        elif base_confirmed:
            status = "confirmed"
            reason = None
        else:
            status = "pending"
            reason = "持件链或交接确认不完整，计件归属待核"

        def add(component, rider, amount, at, fallback, move_id=None):
            """记一段计件。

            归属班优先取骑手承接此单时（揽收或换手）的有效承诺班；
            若动作时刻另有有效承诺班且与承接班不同，则说明这是跨班尾段，
            仍归承接班。找不到承接班时记待核，不丢弃。
            """

            shift_id = accepted_shift.get(rider)
            active = self._active_shift(rider, at) if rider else None
            source = "custody_shift"
            if shift_id is None:
                shift_id = active
                source = "active_commitment" if active else "none"
            share_status = status
            share_reason = reason
            if shift_id is None:
                share_status = "pending"
                share_reason = share_reason or "计件无法归入有效承诺班"
            elif share_status == "confirmed" and self._in_conflict(shift_id, at):
                share_status = "pending"
                share_reason = "动作时刻落在同人不同站的待核区间，计件归属可能改变"
            shares.append(
                PieceShare(
                    parcel=chain.parcel,
                    component=component,
                    rider=rider or "",
                    amount_cents=amount,
                    occurred_at=at,
                    shift=shift_id,
                    status=share_status,
                    reason=share_reason,
                    move=move_id,
                    fallback_shift=fallback,
                    shift_source=source,
                    active_shift=active,
                )
            )

        if not confirmed_switches:
            add(
                "direct",
                pickup.receiver,
                rate.direct,
                pickup.at,
                None,
                pickup.move,
            )
            if not delivered:
                for share in shares:
                    share.status = "pending"
                    share.reason = "包裹尚未送达"
            return shares

        add("pickup", pickup.receiver, rate.pickup, pickup.at, None, pickup.move)
        for switch in confirmed_switches:
            add(
                "relay",
                switch.receiver,
                rate.relay,
                switch.at,
                None,
                switch.move,
            )
        if delivered:
            add(
                "delivery",
                deliverer,
                rate.delivery,
                delivery.at,
                None,
                delivery.move,
            )
        return shares

    def _custody_before(self, chain: ParcelChain, switch) -> str | None:
        custody = chain.pickup.receiver if chain.pickup else None
        for item in chain.switches:
            if item.move == switch.move:
                return custody
            if item.confirmed:
                custody = item.receiver
        return custody

    def _qualified(self, rider: str, at: datetime, station: str | None) -> bool:
        commitment = self.commitments.commitment_at(rider, at)
        if commitment is None:
            return False
        if station is not None and commitment.station != station:
            return False
        return True

    def _station_of(self, shift_id: str | None) -> str | None:
        if shift_id is None:
            return None
        facts = self.facts.get(shift_id)
        return facts.shift.station if facts else None

    def _active_shift(self, rider: str, at: datetime) -> str | None:
        commitment = self.commitments.commitment_at(rider, at)
        return commitment.shift if commitment else None

    def _in_conflict(self, shift_id: str, at: datetime) -> bool:
        facts = self.facts.get(shift_id)
        if facts is None:
            return False
        for conflict in facts.conflicts:
            interval = conflict.get("interval")
            if interval and parse_time(interval["from"]) <= at < parse_time(interval["until"]):
                return True
        return False

    # ------------------------------------------------------------------ 收尾
    def _finalize(self) -> None:
        for shift_id, facts in self.facts.items():
            shares = [s for s in self.pieces if s.shift == shift_id]
            if not facts.commitment:
                facts.status = "unassigned"
            elif facts.pending or facts.conflicts or any(
                f["code"] in {"missing_in", "missing_out", "missing_attendance", "in_out_contradiction"}
                for f in facts.flags
            ):
                facts.status = "pending_review"
            else:
                facts.status = "confirmed"

    # ------------------------------------------------------------------ 查询
    def pieces_for_shift(self, shift_id: str) -> list[PieceShare]:
        return [share for share in self.pieces if share.shift == shift_id]

    def pieces_for_rider(self, rider: str) -> list[PieceShare]:
        return [share for share in self.pieces if share.rider == rider]

    def parcel_status(self, parcel: str) -> dict:
        chain = next((c for c in self.chains if c.parcel == parcel), None)
        if chain is None:
            return {"parcel": parcel, "stage": "unknown", "reason": "没有交接记录"}
        view = self._chain_view(chain)
        view["disputes"] = list(chain.disputes)
        view["pieces"] = [
            share.describe() for share in self.pieces if share.parcel == parcel
        ]
        return view

    def _chain_view(self, chain: ParcelChain) -> dict:
        """按台账规则回答单号所处阶段与当前持件人。

        与计件归属共用同一套判断：换手需双方确认、交件人必须是当前
        持件人、收件人须有本站有效接班资格；任一条不满足，实物不动。
        """

        if not chain.moves:
            return {
                "parcel": chain.parcel,
                "stage": "unknown",
                "reason": "没有交接记录",
                "switches": [],
            }
        pickup = chain.pickup
        custody = pickup.receiver if pickup else None
        accepted: list[dict] = []
        for switch in chain.switches:
            ok = switch.confirmed and switch.sender == custody
            if ok:
                station = switch.station or self._station_of(
                    self._active_shift(switch.sender, switch.at)
                )
                ok = self._qualified(switch.receiver, switch.at, station)
            if ok:
                custody = switch.receiver
                accepted.append(switch.describe())
        unconfirmed = [s.describe() for s in chain.switches if not s.confirmed]
        delivered = chain.delivered and chain.delivery is not None
        view = {
            "parcel": chain.parcel,
            "shop": chain.shop,
            "rate": chain.rate,
            "delivered_at": format_time(chain.delivered_at) if chain.delivered_at else None,
            "returned_at": format_time(chain.returned_at) if chain.returned_at else None,
            "switches": accepted,
            "unconfirmed_switches": unconfirmed,
            "custodian": custody,
        }
        if delivered:
            deliverer = chain.delivery.sender if chain.delivery else None
            return {
                **view,
                "stage": "delivered",
                "delivered_by": deliverer,
                "switched": bool(accepted),
                "pending_handoff": bool(unconfirmed),
            }
        if chain.returned:
            return {**view, "stage": "returned", "switched": bool(accepted)}
        if unconfirmed and not accepted:
            return {
                **view,
                "stage": "pending_handoff",
                "pending_handoff": True,
                "reason": "换手确认不完整，实物仍归原骑手",
            }
        if accepted:
            return {
                **view,
                "stage": "switched",
                "switched": True,
                "pending_handoff": bool(unconfirmed),
            }
        return {
            **view,
            "stage": "with_original",
            "switched": False,
            "pending_handoff": bool(unconfirmed),
        }

    def all_parcel_status(self) -> list[dict]:
        return [self.parcel_status(parcel) for parcel in sorted(self.parcels)]

    def rider_shifts(self, rider: str) -> list[ShiftFacts]:
        return [self.facts[s.shift] for s in self.shifts if s.rider == rider]

    def describe(self) -> dict:
        return {
            "shifts": [self.facts[s.shift].describe() for s in self.shifts],
            "commitments": self.commitments.describe(),
            "rejected_revisions": list(self.rejected_revisions),
            "handover_gaps": list(self.handover_gaps),
            "parcels": self.all_parcel_status(),
            "parcel_disputes": list(self.parcel_disputes),
            "pieces": [share.describe() for share in self.pieces],
            "rates": [rate.describe() for rate in self.rates.values()],
        }


def _subtract_all(base: Iterable[Interval], *cuts: Iterable[Interval]) -> list[Interval]:
    pieces = list(base)
    for cut_group in cuts:
        for cut in cut_group:
            pieces = [part for chunk in pieces for part in chunk.subtract(cut)]
    return merge_intervals(pieces)


def _intersect_many(
    first: Iterable[Interval], second: Iterable[Interval]
) -> list[Interval]:
    result: list[Interval] = []
    for a in first:
        for b in second:
            overlap = a.intersection(b)
            if overlap is not None:
                result.append(overlap)
    return merge_intervals(result)


def build_ledger(
    data: ExchangeData,
    continuations: Iterable[dict] | None = None,
    corrections: Iterable[dict] | None = None,
) -> Ledger:
    return Ledger(data, continuations=continuations, corrections=corrections)
