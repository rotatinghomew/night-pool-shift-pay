"""角色接入与查询边界。

四种身份：排班员（scheduler）、门店（shop）、骑手（rider）、发薪人员
（payroll）。每个身份只能看到与其职责相关的记录：

* 骑手只能看到自己的在岗时段、送件分摊和发放明细；看不到别人的工资、
  也看不到未归属于自己的计件；
* 门店只看到本店的包裹与交接，以及相关骑手的当前持件状态，不看到
  个人工资明细；
* 排班员看到班次、承诺与在岗证据，不看到金额；
* 发薪人员看到工资与签发，但更正仍须协调员（coordinator）核实。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .ledger import Ledger
from .payroll import PayrollEngine
from .store import Store, StoreError


class AccessError(PermissionError):
    """身份与请求的范围不匹配。"""


ROLES = ("scheduler", "shop", "rider", "payroll", "coordinator")


@dataclass(frozen=True)
class Actor:
    actor: str
    role: str
    station: str | None = None
    shop: str | None = None

    def require(self, *roles: str) -> None:
        if self.role not in roles:
            raise AccessError(
                f"{self.actor} 的身份为 {self.role}，不能执行该操作"
            )


class Service:
    """把资料库、台账与计薪组合成对各角色的操作。"""

    def __init__(self, store: Store, *, rate: str | None = None):
        self.store = store
        self.rate = rate
        self.ledger = Ledger(self.store.exchange())
        self.payroll = PayrollEngine(self.ledger, default_rate=rate)
        self._issuances = self._load_issuances()
        self._corrections = self.store.read("corrections")
        self._adjustments = self.store.read("adjustments")

    # ------------------------------------------------------------------ 载入
    def reload(self) -> "Service":
        self.ledger = Ledger(self.store.exchange())
        self.payroll = PayrollEngine(self.ledger, default_rate=self.rate)
        self._issuances = self._load_issuances()
        self._corrections = self.store.read("corrections")
        self._adjustments = self.store.read("adjustments")
        return self

    def _load_issuances(self) -> dict[tuple[str, str], dict[str, Any]]:
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for row in self.store.read("issuances"):
            if row.get("revoked"):
                result.pop((row["rider"], row["period"]), None)
                continue
            result[(row["rider"], row["period"])] = row
        return result

    # -------------------------------------------------------------- 身份
    def actor(self, actor_id: str) -> Actor:
        rows = self.store.actors()
        if actor_id not in rows:
            raise AccessError(f"未登记的身份: {actor_id}")
        row = rows[actor_id]
        return Actor(
            actor=actor_id,
            role=str(row["role"]),
            station=row.get("station"),
            shop=row.get("shop"),
        )

    def register_actor(
        self,
        *,
        actor: str,
        role: str,
        station: str | None = None,
        shop: str | None = None,
        by: str,
    ) -> Actor:
        operator = self.actor(by)
        operator.require("coordinator", "scheduler")
        if role not in ROLES:
            raise AccessError(f"未知身份: {role}")
        self.store.append(
            "actors",
            {
                "actor": actor,
                "role": role,
                "station": station,
                "shop": shop,
                "registered_by": by,
                "recorded_at": self._now(),
            },
        )
        self.store.record_event(
            actor=by, role=operator.role, action="register_actor", target={"actor": actor, "role": role}
        )
        return self.actor(actor)

    # -------------------------------------------------------------- 排班员
    def schedule_shift(
        self,
        *,
        by: str,
        shift: str,
        station: str,
        rider: str,
        starts: str,
        until: str,
        rate: str | None = None,
        origin_shift: str | None = None,
    ) -> dict[str, Any]:
        scheduler = self.actor(by)
        scheduler.require("scheduler", "coordinator")
        existing = {row.get("shift") for row in self.store.read("shifts")}
        if shift in existing:
            raise StoreError(f"班次编号已存在: {shift}")
        record = {
            "shift": shift,
            "station": station,
            "rider": rider,
            "from": starts,
            "until": until,
            "rate": rate,
            "origin_shift": origin_shift,
            "revision": 1 if origin_shift else 0,
            "recorded_at": self._now(),
        }
        self.store.append("shifts", record)
        self.store.record_event(
            actor=by,
            role=scheduler.role,
            action="schedule_shift",
            target={"shift": shift, "origin_shift": origin_shift},
        )
        self.reload()
        return record

    def revise_shift(
        self,
        *,
        by: str,
        base_shift: str,
        new_shift: str,
        station: str | None = None,
        rider: str | None = None,
        starts: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        scheduler = self.actor(by)
        scheduler.require("scheduler", "coordinator")
        base = next(
            (row for row in self.store.read("shifts") if row.get("shift") == base_shift),
            None,
        )
        if base is None:
            raise StoreError(f"找不到班次: {base_shift}")
        # 是否生效由台账按在岗凭证判断；这里只写入修订
        record = self.schedule_shift(
            by=by,
            shift=new_shift,
            station=station or base["station"],
            rider=rider or base["rider"],
            starts=starts or base["from"],
            until=until or base["until"],
            rate=base.get("rate"),
            origin_shift=base.get("origin_shift") or base_shift,
        )
        return record

    def accept_invite(
        self,
        *,
        by: str,
        shift: str,
        accepted: bool = True,
        at: str | None = None,
    ) -> dict[str, Any]:
        rider = self.actor(by)
        rider.require("rider", "coordinator")
        shift_row = next(
            (row for row in self.store.read("shifts") if row.get("shift") == shift),
            None,
        )
        if shift_row is None:
            raise StoreError(f"找不到班次: {shift}")
        if rider.role == "rider" and shift_row.get("rider") != rider.actor:
            raise AccessError("骑手只能接受安排给自己的邀请")
        record = {
            "shift": shift,
            "rider": shift_row["rider"],
            "accepted": accepted,
            "accepted_at": at or self._now(),
            "recorded_by": by,
        }
        self.store.append("acceptances", record)
        self.store.record_event(
            actor=by,
            role=rider.role,
            action="accept_invite" if accepted else "decline_invite",
            target={"shift": shift},
        )
        self.reload()
        return record

    def check_in(
        self,
        *,
        by: str,
        shift: str,
        event: str,
        kind: str,
        earliest: str,
        latest: str | None = None,
        device: str = "",
    ) -> dict[str, Any]:
        actor = self.actor(by)
        actor.require("rider", "scheduler", "coordinator")
        shift_row = next(
            (row for row in self.store.read("shifts") if row.get("shift") == shift),
            None,
        )
        if shift_row is None:
            raise StoreError(f"找不到班次: {shift}")
        if actor.role == "rider" and shift_row.get("rider") != actor.actor:
            raise AccessError("骑手只能为自己的班签到")
        record = {
            "event": event,
            "shift": shift,
            "kind": kind,
            "earliest": earliest,
            "latest": latest or earliest,
            "device": device,
        }
        self.store.append("attendance", record)
        self.store.record_event(
            actor=by, role=actor.role, action="attendance", target={"shift": shift, "kind": kind}
        )
        self.reload()
        return record

    def confirm_continuation(
        self,
        *,
        by: str,
        original_shift: str,
        successor_shift: str,
        starts: str,
        until: str,
    ) -> dict[str, Any]:
        actor = self.actor(by)
        actor.require("rider", "coordinator")
        if actor.role == "rider":
            rows = {
                row.get("shift"): row for row in self.store.read("shifts")
            }
            if original_shift not in rows or rows[original_shift].get("rider") != actor.actor:
                raise AccessError("只能确认自己的续守")
        record = {
            "original_shift": original_shift,
            "successor_shift": successor_shift,
            "from": starts,
            "until": until,
            "proof": [original_shift, actor.actor],
            "confirmed_by": by,
        }
        self.store.append("continuations", record)
        self.store.record_event(
            actor=by,
            role=actor.role,
            action="confirm_continuation",
            target={"original_shift": original_shift, "successor_shift": successor_shift},
        )
        self.reload()
        return record

    def confirm_handover(
        self,
        *,
        by: str,
        move: str,
        parcel: str,
        sender: str,
        receiver: str,
        occurred_at: str,
        station: str | None = None,
    ) -> dict[str, Any]:
        actor = self.actor(by)
        actor.require("rider", "coordinator")
        # 骑手只能确认自己参与的换手；协调员可代录纸面确认，
        # 但改交班时刻仍走更正流程以留下依据。
        if actor.role == "rider" and actor.actor not in (sender, receiver):
            raise AccessError("只能确认自己参与的换手")
        record = {
            "parcel": parcel,
            "move": move,
            "kind": "switch",
            "from": sender,
            "to": receiver,
            "proof": [actor.actor],
            "occurred_at": occurred_at,
            "station": station,
        }
        self.store.append("handoffs", record)
        self.store.record_event(
            actor=by,
            role=actor.role,
            action="confirm_handover",
            target={"parcel": parcel, "move": move},
        )
        self.reload()
        return record

    # --------------------------------------------------------------- 骑手
    def _owns_parcel(self, rider: str, parcel: str) -> bool:
        """骑手有资格申诉该包裹。

        有经手记录、在出货站有班次，或（未计入的配送）在该单涉及的
        营业日当班——最后一种正是“系统里还没有这条配送”的情形，是否
        成立仍由协调员核实。
        """

        moves = [
            move for move in self.store.read("handoffs") if move.get("parcel") == parcel
        ]
        if any(rider in (move.get("from"), move.get("to")) for move in moves):
            return True
        meta = next(
            (row for row in self.store.read("parcels") if row.get("parcel") == parcel),
            None,
        )
        if meta is None:
            return False
        shop = meta.get("shop")
        shifts = [
            row for row in self.store.read("shifts") if row.get("rider") == rider
        ]
        if any(row.get("station") == shop for row in shifts):
            return True
        if not moves:
            # 还没有任何交接记录：只要该骑手有排班就可以主张，是否成立
            # 仍由协调员核实
            return bool(shifts)
        return False

    def file_appeal(
        self,
        *,
        by: str,
        kind: str,
        reason: str,
        shift: str | None = None,
        parcel: str | None = None,
        starts: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        rider = self.actor(by)
        rider.require("rider", "coordinator")
        if kind not in ("handoff_time", "missing_delivery", "attendance"):
            raise AccessError(f"未知申诉类型: {kind}")
        shift_row = None
        if shift:
            shift_row = next(
                (row for row in self.store.read("shifts") if row.get("shift") == shift),
                None,
            )
            if shift_row is None:
                raise StoreError(f"找不到班次: {shift}")
        if rider.role == "rider":
            owns_shift = shift_row is not None and shift_row.get("rider") == rider.actor
            owns_parcel = parcel is not None and self._owns_parcel(
                rider.actor, parcel
            )
            if not (owns_shift or owns_parcel):
                raise AccessError("只能申诉自己的班次或经手的包裹")
        if kind == "missing_delivery" and not parcel:
            raise AccessError("未计入的配送申诉需要指出包裹")
        if kind == "handoff_time" and not (shift or parcel):
            raise AccessError("交班时刻申诉需要指出班次或包裹")
        record = {
            "appeal": f"appeal-{self.store.next_sequence('appeals')}",
            "rider": rider.actor if rider.role == "rider" else by,
            "kind": kind,
            "reason": reason,
            "shift": shift,
            "parcel": parcel,
            "from": starts,
            "until": until,
            "status": "open",
            "filed_by": by,
            "filed_at": self._now(),
        }
        self.store.append("appeals", record)
        self.store.record_event(
            actor=by, role=rider.role, action="file_appeal", target=record
        )
        return record

    # -------------------------------------------------------------- 协调员
    def resolve_appeal(
        self,
        *,
        by: str,
        appeal: str,
        kind: str,
        decision: str,
        reason: str,
        shift: str | None = None,
        parcel: str | None = None,
        starts: str | None = None,
        until: str | None = None,
        status: str | None = None,
        move: str | None = None,
        occurred_at: str | None = None,
        rider: str | None = None,
        proof: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        coordinator = self.actor(by)
        coordinator.require("coordinator")
        appeal_row = next(
            (row for row in self.store.read("appeals") if row.get("appeal") == appeal),
            None,
        )
        if appeal_row is None:
            raise StoreError(f"找不到申诉: {appeal}")
        if appeal_row.get("status") != "open":
            raise StoreError(f"申诉已处理: {appeal}")
        if decision not in ("upheld", "rejected", "corrected"):
            raise AccessError("决定必须是 upheld/rejected/corrected")
        correction = None
        if decision == "corrected":
            correction = {
                "correction": f"corr-{self.store.next_sequence('corrections')}",
                "kind": kind,
                "appeal": appeal,
                "reason": reason,
                "coordinator": by,
                "created_at": self._now(),
                "shift": shift,
                "parcel": parcel,
                "status": status,
                "move": move,
                "occurred_at": occurred_at,
                "rider": rider or appeal_row.get("rider") if kind == "delivery" else rider,
            }
            if proof:
                correction["proof"] = list(proof)
            if starts is not None:
                correction["from"] = starts
            if until is not None:
                correction["until"] = until
            self.store.append("corrections", correction)
        self.store.append(
            "appeals",
            {
                **appeal_row,
                "appeal": appeal,
                "status": "resolved",
                "decision": decision,
                "resolution": reason,
                "resolved_by": by,
                "resolved_at": self._now(),
            },
        )
        self.store.record_event(
            actor=by,
            role=coordinator.role,
            action="resolve_appeal",
            target={"appeal": appeal, "decision": decision, "correction": (correction or {}).get("correction")},
        )
        self.reload()
        return {"appeal": appeal, "decision": decision, "correction": correction}

    # -------------------------------------------------------------- 发薪
    def issue_period(
        self,
        *,
        by: str,
        period_start: str,
        riders: Iterable[str] | None = None,
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        paymaster = self.actor(by)
        paymaster.require("payroll", "coordinator")
        statements = self.payroll.statements(
            _date(period_start), riders=riders
        )
        issued: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for statement in statements:
            key = (statement.rider, statement.period)
            key_str = statement.period
            if key in self._issuances:
                skipped.append(
                    {
                        "rider": statement.rider,
                        "period": key_str,
                        "reason": "该期已签发；更正请走差额流程",
                        "issuance": self._issuances[key]["issuance"],
                    }
                )
                continue
            if statement.confirmed_total == 0 and statement.pending_total == 0:
                skipped.append(
                    {
                        "rider": statement.rider,
                        "period": key_str,
                        "reason": "本期没有可签发金额",
                    }
                )
                continue
            record = {
                "issuance": f"iss-{self.store.next_sequence('issuances')}",
                "rider": statement.rider,
                "period": statement.period,
                "period_start": statement.period_start.isoformat(),
                "period_end": statement.period_end.isoformat(),
                "total_cents": statement.confirmed_total,
                "pending_total_cents": statement.pending_total,
                "lines": [
                    {
                        "shift": pay.shift,
                        "base_pay_cents": pay.base_pay,
                        "floor_confirmed_cents": pay.floor_confirmed,
                        "pieces_confirmed_cents": pay.pieces_confirmed,
                        "confirmed_minutes": pay.confirmed_minutes,
                        "status": pay.status,
                    }
                    for pay in statement.shift_pays
                ],
                "pending_lines": [
                    {
                        "shift": pay.shift,
                        "holdback_cents": pay.holdback,
                    }
                    for pay in statement.shift_pays
                    if pay.holdback
                ],
                "basis_digest": self.store.digest(),
                "issued_by": by,
                "issued_at": self._now(),
            }
            if not dry_run:
                self.store.append("issuances", record)
                self.store.record_event(
                    actor=by,
                    role=paymaster.role,
                    action="issue_period",
                    target={"issuance": record["issuance"], "rider": statement.rider, "period": key_str},
                )
            issued.append(record)
        if not dry_run:
            self.reload()
        if skipped:
            # 已签发和不需签发的对象不再重复写入，但仍然报告
            issued.append({"skipped": skipped})
        return issued

    def correction_delta(
        self,
        *,
        by: str,
        rider: str,
        period: str,
        affected_shifts: Iterable[str],
        reason: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """对已签发工资生成独立差额；未签发范围不受影响。"""

        coordinator = self.actor(by)
        coordinator.require("coordinator")
        original = self._issuances.get((rider, period))
        if original is None:
            raise StoreError(f"{rider} 的 {period} 尚未签发，按新事实重算即可")
        prior = [
            row
            for row in self._adjustments
            if row.get("issuance") == original["issuance"]
        ]
        from .payroll import build_correction

        correction = build_correction(
            correction_id=f"corr-{self.store.next_sequence('adjustments')}",
            original=original,
            new_ledger=self.ledger,
            affected_shifts=affected_shifts,
            prior_corrections=prior,
            reason=reason,
            created_at=_parse_now(self._now()),
            created_by=by,
            scope={"shifts": list(affected_shifts), "note": note},
        )
        if correction is None:
            return {"rider": rider, "period": period, "changed": False}
        self.store.append("adjustments", correction)
        self.store.record_event(
            actor=by,
            role=coordinator.role,
            action="correction_delta",
            target={"correction": correction["correction"], "issuance": original["issuance"]},
        )
        self.reload()
        return {**correction, "changed": True}

    # -------------------------------------------------------------- 查询
    def rider_view(self, actor_id: str, period_start: str | None = None) -> dict[str, Any]:
        actor = self.actor(actor_id)
        actor.require("rider", "coordinator", "payroll")
        rider = actor.actor if actor.role == "rider" else None
        if rider is None:
            raise AccessError("查询骑手视图需要骑手身份或指定骑手")
        return self._rider_payload(rider, period_start)

    def rider_view_for(
        self, actor_id: str, rider: str, period_start: str | None = None
    ) -> dict[str, Any]:
        actor = self.actor(actor_id)
        actor.require("payroll", "coordinator")
        if actor.role == "rider" and actor.actor != rider:
            raise AccessError("骑手只能查看自己的记录")
        return self._rider_payload(rider, period_start)

    def _rider_payload(self, rider: str, period_start: str | None) -> dict[str, Any]:
        facts = self.ledger.rider_shifts(rider)
        payload: dict[str, Any] = {
            "rider": rider,
            "shifts": [fact.describe() for fact in facts],
            "pieces": [
                share.describe() for share in self.ledger.pieces_for_rider(rider)
            ],
            "commitments": [
                c.describe() for c in self.ledger.commitments.for_rider(rider)
            ],
            "issuances": [
                row
                for (name, _period), row in sorted(self._issuances.items())
                if name == rider
            ],
            "adjustments": [
                row
                for row in self._adjustments
                if row.get("rider") == rider
            ],
            "appeals": [
                row
                for row in self.store.read("appeals")
                if row.get("rider") == rider
            ],
        }
        if period_start:
            statement = self.payroll.statement(rider, _date(period_start))
            from .payroll import apply_adjustments

            adjustments = [
                row
                for row in self._adjustments
                if row.get("rider") == rider
                and row.get("scheduled_period") == statement.period
            ]
            issuance = self._issuances.get((rider, statement.period))
            apply_adjustments(statement, adjustments, issuance)
            payload["statement"] = statement.describe()
        return payload

    def shop_view(self, actor_id: str) -> dict[str, Any]:
        actor = self.actor(actor_id)
        actor.require("shop", "coordinator", "scheduler")
        if actor.role == "shop":
            shop = actor.shop
        else:
            raise AccessError("查询门店视图需要门店身份")
        parcels = [
            view
            for view in self.ledger.all_parcel_status()
            if self.ledger.parcels.get(view["parcel"], {}).get("shop") == shop
        ]
        return {
            "shop": shop,
            "parcels": parcels,
            "open_switches": [
                {
                    "parcel": view["parcel"],
                    "switches": [
                        s for s in view.get("switches", []) if not s.get("confirmed")
                    ],
                }
                for view in parcels
                if view.get("pending_handoff")
            ],
        }

    def scheduler_view(self, actor_id: str) -> dict[str, Any]:
        actor = self.actor(actor_id)
        actor.require("scheduler", "coordinator")
        return {
            "shifts": [fact.describe() for fact in self.ledger.facts.values()],
            "commitments": self.ledger.commitments.describe(),
            "handover_gaps": list(self.ledger.handover_gaps),
            "rejected_revisions": list(self.ledger.rejected_revisions),
            "rejected_corrections": list(self.ledger.corrections_rejected),
        }

    def payroll_view(self, actor_id: str, period_start: str) -> dict[str, Any]:
        actor = self.actor(actor_id)
        actor.require("payroll", "coordinator")
        statements = self.payroll.statements(_date(period_start))
        views = []
        for statement in statements:
            issuance = self._issuances.get((statement.rider, statement.period))
            adjustments = [
                row
                for row in self._adjustments
                if row.get("rider") == statement.rider
                and row.get("scheduled_period") == statement.period
            ]
            from .payroll import apply_adjustments

            apply_adjustments(statement, adjustments, issuance)
            views.append(statement.describe())
        return {
            "period": statements[0].period if statements else None,
            "statements": views,
            "pending_appeals": [
                row for row in self.store.read("appeals") if row.get("status") == "open"
            ],
        }

    def coordinator_view(self, actor_id: str) -> dict[str, Any]:
        actor = self.actor(actor_id)
        actor.require("coordinator")
        return {
            "appeals": self.store.read("appeals"),
            "corrections": self.store.read("corrections"),
            "ledger": self.ledger.describe(),
        }

    def audit(self, actor_id: str) -> list[dict[str, Any]]:
        actor = self.actor(actor_id)
        actor.require("coordinator")
        return self.store.read("events")

    # ---------------------------------------------------------------- 辅助
    def _now(self) -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()


def _date(value: str) -> "date":
    from datetime import date

    return date.fromisoformat(value)


def _parse_now(value: str):
    from .timeutil import parse_time

    return parse_time(value)
