"""配送件、持件链与计件归属。

* pickup 是门店交货，switch 是两名骑手交接，delivery 是交给买家，
  return 是退回门店。
* switch 必须有同一次 from 和 to 双方的确认；只有一方确认时不改变实物
  归属，仍记在待交班或仍在原骑手手里。
* 收件人须有本站已生效的接班资格（在该站承诺覆盖交接时刻）。
* 无换手：揽收骑手得全程 direct 计件。有换手：按 pickup / relay /
  delivery 分给最初揽收人、每次收件的换手骑手、最终送达者。同一次换手
  只计一次。退回门店的未送达件不发计件。
* 计件只对已交付的单确认；未送达的展示可能归属但暂不付款。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from .exchange import Record
from .timeutil import TimeError, format_time, parse_time, to_utc


class HandoffError(ValueError):
    """交接记录不符合约定。"""


KINDS = ("pickup", "switch", "delivery", "return")
MOVE_ORDER = {"pickup": 0, "switch": 1, "delivery": 2, "return": 3}


@dataclass
class Move:
    parcel: str
    move: str
    kind: str
    sender: str
    receiver: str
    proof: tuple[str, ...]
    occurred_at: str
    at: datetime
    station: str | None = None
    source_index: int = -1

    @property
    def proof_complete(self) -> bool:
        return self.sender in self.proof and self.receiver in self.proof

    def describe(self) -> dict:
        return {
            "parcel": self.parcel,
            "move": self.move,
            "kind": self.kind,
            "from": self.sender,
            "to": self.receiver,
            "proof": list(self.proof),
            "proof_complete": self.proof_complete,
            "occurred_at": self.occurred_at,
            "station": self.station,
        }


def read_moves(rows: Iterable[Record]) -> list[Move]:
    moves: list[Move] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        for key in ("parcel", "move", "kind", "from", "to", "proof", "occurred_at"):
            if key not in row:
                raise HandoffError(f"handoffs[{index}] 缺少 {key}")
        kind = row["kind"]
        if kind not in KINDS:
            raise HandoffError(f"handoffs[{index}] 未知 kind: {kind}")
        proofs = row["proof"]
        if not isinstance(proofs, list) or not all(isinstance(p, str) for p in proofs):
            raise HandoffError(f"handoffs[{index}] proof 必须是字符串数组")
        move_id = str(row["move"])
        if move_id in seen and kind != "switch":
            # 同一次换手允许 from/to 各交一份确认，由 collect_switches 合并；
            # 其他动作编号重复说明资料自相矛盾。
            raise HandoffError(f"动作编号重复: {move_id}")
        if move_id in seen and kind == "switch":
            prior = next(m for m in moves if m.move == move_id)
            if prior.kind != "switch" or prior.parcel != str(row["parcel"]):
                raise HandoffError(f"动作编号重复且类型或包裹不一致: {move_id}")
        seen.add(move_id)
        try:
            at = to_utc(parse_time(row["occurred_at"]))
        except TimeError as exc:
            raise HandoffError(str(exc)) from exc
        moves.append(
            Move(
                parcel=str(row["parcel"]),
                move=move_id,
                kind=kind,
                sender=str(row["from"]),
                receiver=str(row["to"]),
                proof=tuple(proofs),
                occurred_at=str(row["occurred_at"]),
                at=at,
                station=str(row["station"]) if row.get("station") else None,
                source_index=index,
            )
        )
    return moves


@dataclass
class Switch:
    """一次换手。确认不完整的也算一个 Switch，但 confirmed 为假。"""

    move: str
    parcel: str
    sender: str
    receiver: str
    at: datetime
    occurred_at: str
    proofs: tuple[str, ...]
    confirmed: bool
    missing: tuple[str, ...] = ()
    station: str | None = None

    def describe(self) -> dict:
        return {
            "move": self.move,
            "parcel": self.parcel,
            "from": self.sender,
            "to": self.receiver,
            "occurred_at": self.occurred_at,
            "proof": list(self.proofs),
            "confirmed": self.confirmed,
            "missing": list(self.missing),
        }


def collect_switches(
    moves: Iterable[Move],
) -> tuple[list[Switch], list[Move]]:
    """把同一 switch 编号的两份确认合并成一次交接。

    返回 (按时间排序的 switch 列表, 不属于 switch 的其他动作)。
    """

    groups: dict[str, list[Move]] = {}
    others: list[Move] = []
    for move in moves:
        if move.kind == "switch":
            groups.setdefault(move.move, []).append(move)
        else:
            others.append(move)

    switches: list[Switch] = []
    for move_id, items in groups.items():
        senders = {m.sender for m in items}
        receivers = {m.receiver for m in items}
        proofs = {p for m in items for p in m.proof}
        occurred = {m.occurred_at for m in items}
        stations = {m.station for m in items if m.station}
        station = next(iter(stations)) if len(stations) == 1 else None
        if len(senders) != 1 or len(receivers) != 1:
            # 同一编号指向不同的交接双方：无法成立，记一次有争议的换手
            first = items[0]
            switches.append(
                Switch(
                    move=move_id,
                    parcel=first.parcel,
                    sender=first.sender,
                    receiver=first.receiver,
                    at=min(m.at for m in items),
                    occurred_at=first.occurred_at,
                    proofs=tuple(sorted(proofs)),
                    confirmed=False,
                    missing=(),
                    station=station,
                )
            )
            continue
        sender = next(iter(senders))
        receiver = next(iter(receivers))
        missing = tuple(who for who in (sender, receiver) if who not in proofs)
        switches.append(
            Switch(
                move=move_id,
                parcel=items[0].parcel,
                sender=sender,
                receiver=receiver,
                at=min(m.at for m in items),
                occurred_at=min(occurred),
                proofs=tuple(sorted(proofs)),
                confirmed=not missing,
                missing=missing,
                station=station,
            )
        )
    switches.sort(key=lambda s: (s.at, s.move))
    return switches, others


@dataclass
class PieceShare:
    """一段计件责任。amount 为整数分。"""

    parcel: str
    component: str  # direct / pickup / relay / delivery
    rider: str
    amount_cents: int
    occurred_at: datetime
    shift: str | None
    status: str  # confirmed / pending
    reason: str | None = None
    move: str | None = None
    fallback_shift: str | None = None
    shift_source: str | None = None
    active_shift: str | None = None

    def describe(self) -> dict:
        return {
            "parcel": self.parcel,
            "component": self.component,
            "rider": self.rider,
            "amount_cents": self.amount_cents,
            "occurred_at": format_time(self.occurred_at),
            "shift": self.shift,
            "shift_source": self.shift_source,
            "active_shift": self.active_shift,
            "fallback_shift": self.fallback_shift,
            "status": self.status,
            "reason": self.reason,
            "move": self.move,
        }


@dataclass
class ParcelChain:
    parcel: str
    shop: str
    rate: str
    moves: list[Move]
    switches: list[Switch]
    delivered_at: datetime | None
    returned_at: datetime | None
    disputes: list[dict] = field(default_factory=list)

    @property
    def delivered(self) -> bool:
        return self.delivered_at is not None

    @property
    def returned(self) -> bool:
        return self.returned_at is not None

    @property
    def pickup(self) -> Move | None:
        return next((m for m in self.moves if m.kind == "pickup"), None)

    @property
    def delivery(self) -> Move | None:
        return next((m for m in self.moves if m.kind == "delivery"), None)

    def custody_timeline(self) -> list[tuple[Move, str]]:
        """[(动作, 动作后的持件人)]。确认不完整的换手不改变持件人。"""

        timeline: list[tuple[Move, str]] = []
        custody: str | None = None
        switch_by_move = {s.move: s for s in self.switches}
        for move in self.moves:
            if move.kind == "pickup":
                custody = move.receiver
            elif move.kind == "switch":
                switch = switch_by_move.get(move.move)
                if switch and switch.confirmed and switch.sender == custody:
                    custody = switch.receiver
            timeline.append((move, custody or ""))
        return timeline

    def current_custodian(self) -> str | None:
        timeline = self.custody_timeline()
        return timeline[-1][1] if timeline else None


def build_chain(parcel_id: str, moves: list[Move], meta: dict) -> ParcelChain:
    ordered = sorted(moves, key=lambda m: (m.at, MOVE_ORDER.get(m.kind, 9), m.source_index))
    switches, _ = collect_switches(ordered)
    deliveries = [m for m in ordered if m.kind == "delivery"]
    returns = [m for m in ordered if m.kind == "return"]
    pickups = [m for m in ordered if m.kind == "pickup"]
    disputes: list[dict] = []
    if len(pickups) != 1:
        disputes.append(
            {
                "parcel": parcel_id,
                "reason": "揽收记录不是恰好一条",
                "moves": [m.describe() for m in pickups],
            }
        )
    if len(deliveries) > 1:
        disputes.append(
            {
                "parcel": parcel_id,
                "reason": "同一单出现多次送达",
                "moves": [m.describe() for m in deliveries],
            }
        )
    if deliveries and returns:
        disputes.append({"parcel": parcel_id, "reason": "同一单既送达又退回"})
    return ParcelChain(
        parcel=parcel_id,
        shop=str(meta.get("shop", "")),
        rate=str(meta.get("rate", "")),
        moves=ordered,
        switches=switches,
        delivered_at=min((m.at for m in deliveries), default=None),
        returned_at=min((m.at for m in returns), default=None),
        disputes=disputes,
    )


def build_chains(
    moves: Iterable[Move], parcels: dict[str, dict]
) -> list[ParcelChain]:
    by_parcel: dict[str, list[Move]] = {}
    for move in moves:
        by_parcel.setdefault(move.parcel, []).append(move)
    chains = [
        build_chain(parcel_id, items, parcels.get(parcel_id, {}))
        for parcel_id, items in by_parcel.items()
    ]
    # 有包裹登记但没有交接记录的，也给一条空链，查询时能说明原因
    for parcel_id, meta in parcels.items():
        if parcel_id not in by_parcel:
            chains.append(
                ParcelChain(
                    parcel=parcel_id,
                    shop=str(meta.get("shop", "")),
                    rate=str(meta.get("rate", "")),
                    moves=[],
                    switches=[],
                    delivered_at=None,
                    returned_at=None,
                    disputes=[{"parcel": parcel_id, "reason": "没有交接记录"}],
                )
            )
    return sorted(chains, key=lambda c: c.parcel)
