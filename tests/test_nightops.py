"""台账、计薪与服务层的规则测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from nightops.corrections import read_corrections
from nightops.exchange import load_exchange, load_exchange_dir
from nightops.handoffs import collect_switches, read_moves
from nightops.ledger import build_ledger
from nightops.payroll import PayrollEngine, cents
from nightops.service import AccessError, Service
from nightops.store import Store, StoreError
from nightops.timeutil import Interval, period_of, subtract_intervals
from nightops.shifts import build_commitments, read_shifts

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def sample_data():
    return load_exchange_dir(SAMPLES)


def fact(ledger, shift):
    return ledger.facts[shift]


def iso(value: str):
    return datetime.fromisoformat(value)


RATES = [
    {
        "rate": "r1",
        "floor_per_hour": 2600,
        "direct": 8700,
        "pickup": 2900,
        "relay": 2000,
        "delivery": 4500,
    }
]


def make_store(directory: str, **files):
    store = Store(directory)
    for name, rows in files.items():
        for row in rows:
            store.append(name, dict(row))
    store.append("actors", {"actor": "admin", "role": "coordinator"})
    return store


class IntervalTest(unittest.TestCase):
    def test_half_open_and_subtract(self):
        base = Interval(iso("2026-11-26T20:00:00+08:00"), iso("2026-11-27T00:00:00+08:00"))
        cut = Interval(iso("2026-11-26T23:00:00+08:00"), iso("2026-11-26T23:30:00+08:00"))
        pieces = subtract_intervals(base, [cut])
        self.assertEqual(len(pieces), 2)
        self.assertEqual(base.minutes(), 240)
        self.assertEqual(base.contains(iso("2026-11-27T00:00:00+08:00")), False)
        self.assertEqual(base.contains(iso("2026-11-26T20:00:00+08:00")), True)

    def test_periods_are_biweekly(self):
        self.assertEqual(period_of(date(2026, 11, 26)), (date(2026, 11, 23), date(2026, 12, 6)))
        self.assertEqual(period_of(date(2026, 11, 23)), (date(2026, 11, 23), date(2026, 12, 6)))
        self.assertEqual(period_of(date(2026, 12, 7)), (date(2026, 12, 7), date(2026, 12, 20)))


class ShiftTest(unittest.TestCase):
    def test_overlapping_invites_yield_one_commitment(self):
        ledger = build_ledger(sample_data())
        segments = ledger.commitments.segments("nite-c")
        self.assertEqual(segments, [])
        self.assertEqual(len(ledger.commitments.segments("nite-b")), 1)
        self.assertEqual(
            fact(ledger, "nite-c").status, "unassigned"
        )
        self.assertEqual(ledger.commitments.overlap_rejections[0]["rider"], "r-b")
        self.assertEqual(
            ledger.commitments.overlap_rejections[0]["granted"], []
        )

    def test_non_overlapping_invites_are_both_kept(self):
        shifts = read_shifts(
            [
                {
                    "shift": "a",
                    "station": "s1",
                    "rider": "r",
                    "from": "2026-11-26T20:00:00+08:00",
                    "until": "2026-11-26T22:00:00+08:00",
                },
                {
                    "shift": "b",
                    "station": "s2",
                    "rider": "r",
                    "from": "2026-11-26T22:00:00+08:00",
                    "until": "2026-11-27T00:00:00+08:00",
                },
            ]
        )
        book = build_commitments(shifts)
        self.assertEqual(len(book.by_rider["r"]), 2)
        self.assertEqual(book.overlap_rejections, [])

    def test_partial_overlap_grants_only_free_part(self):
        shifts = read_shifts(
            [
                {
                    "shift": "a",
                    "station": "s1",
                    "rider": "r",
                    "from": "2026-11-26T20:00:00+08:00",
                    "until": "2026-11-26T23:00:00+08:00",
                },
                {
                    "shift": "b",
                    "station": "s2",
                    "rider": "r",
                    "from": "2026-11-26T22:00:00+08:00",
                    "until": "2026-11-27T01:00:00+08:00",
                },
            ]
        )
        book = build_commitments(shifts)
        segments = book.segments("b")
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].minutes(), 120)
        self.assertEqual(segments[0].start, iso("2026-11-26T23:00:00+08:00"))


class AttendanceTest(unittest.TestCase):
    def test_clock_skew_confirms_only_certain_span(self):
        # 只有迟到范围 23:59–00:02 时，00:02 之后才确定在岗
        data = load_exchange(
            shifts=[
                {
                    "shift": "x",
                    "station": "s",
                    "rider": "r",
                    "from": "2026-11-26T23:50:00+08:00",
                    "until": "2026-11-27T02:00:00+08:00",
                }
            ],
            attendance=[
                {
                    "event": "i",
                    "shift": "x",
                    "kind": "in",
                    "earliest": "2026-11-26T23:59:00+08:00",
                    "latest": "2026-11-27T00:02:00+08:00",
                },
                {
                    "event": "o",
                    "shift": "x",
                    "kind": "out",
                    "earliest": "2026-11-27T02:00:00+08:00",
                    "latest": "2026-11-27T02:00:00+08:00",
                },
            ],
        )
        facts = build_ledger(data).facts["x"]
        self.assertEqual(facts.confirmed_minutes, 118)
        self.assertEqual(
            [i.describe() for i in facts.confirmed],
            [{"from": "2026-11-27T00:02:00+08:00", "until": "2026-11-27T02:00:00+08:00"}],
        )

    def test_sample_skew_is_further_limited_by_station_conflict(self):
        # 样本里 nite-b 与 nite-c 同人不同站，00:00–01:00 暂停结算，
        # 只剩 01:00–02:00 可确认
        facts = fact(build_ledger(sample_data()), "nite-b")
        self.assertEqual(facts.confirmed_minutes, 60)
        self.assertTrue(
            any(f["code"] == "same_rider_different_station" for f in facts.flags)
        )

    def test_exit_after_shift_is_not_paid(self):
        ledger = build_ledger(sample_data())
        facts = fact(ledger, "nite-a")
        self.assertEqual(facts.confirmed_minutes, 240)
        self.assertLessEqual(facts.confirmed[0].end, iso("2026-11-26T16:00:00+00:00"))

    def test_missing_checkout_blocks_floor_but_not_disjoint_work(self):
        data = load_exchange(
            shifts=[
                {
                    "shift": "x",
                    "station": "s",
                    "rider": "r",
                    "from": "2026-11-26T20:00:00+08:00",
                    "until": "2026-11-27T00:00:00+08:00",
                }
            ],
            attendance=[
                {
                    "event": "e1",
                    "shift": "x",
                    "kind": "in",
                    "earliest": "2026-11-26T20:00:00+08:00",
                    "latest": "2026-11-26T20:00:00+08:00",
                }
            ],
            rates=[
                {
                    "rate": "r1",
                    "floor_per_hour": 2600,
                    "direct": 8700,
                    "pickup": 2900,
                    "relay": 2000,
                    "delivery": 4500,
                }
            ],
        )
        ledger = build_ledger(data)
        facts = fact(ledger, "x")
        self.assertEqual(facts.confirmed_minutes, 0)
        self.assertEqual(facts.pending_minutes, 240)
        self.assertEqual(facts.status, "pending_review")


class RevisionTest(unittest.TestCase):
    def base(self):
        return {
            "shift": "a",
            "station": "s1",
            "rider": "r",
            "from": "2026-11-26T20:00:00+08:00",
            "until": "2026-11-27T00:00:00+08:00",
        }

    def test_revision_applies_before_check_in(self):
        data = load_exchange(
            shifts=[
                self.base(),
                {
                    **self.base(),
                    "shift": "a2",
                    "origin_shift": "a",
                    "revision": 1,
                    "until": "2026-11-27T01:00:00+08:00",
                },
            ],
            rates=[],
        )
        ledger = build_ledger(data)
        self.assertEqual([s.shift for s in ledger.shifts], ["a2"])
        self.assertEqual(ledger.shifts[0].until, iso("2026-11-27T01:00:00+08:00"))

    def test_revision_rejected_after_check_in(self):
        data = load_exchange(
            shifts=[
                self.base(),
                {
                    **self.base(),
                    "shift": "a2",
                    "origin_shift": "a",
                    "revision": 1,
                    "rider": "someone-else",
                },
            ],
            attendance=[
                {
                    "event": "e1",
                    "shift": "a",
                    "kind": "in",
                    "earliest": "2026-11-26T20:00:00+08:00",
                    "latest": "2026-11-26T20:00:00+08:00",
                }
            ],
            rates=[],
        )
        ledger = build_ledger(data)
        self.assertEqual([s.shift for s in ledger.shifts], ["a"])
        self.assertEqual(ledger.shifts[0].rider, "r")
        self.assertTrue(ledger.rejected_revisions)


class HandoffTest(unittest.TestCase):
    def test_two_sided_proof_merges_to_one_switch(self):
        moves = read_moves(
            [
                {
                    "parcel": "p",
                    "move": "m1",
                    "kind": "switch",
                    "from": "a",
                    "to": "b",
                    "proof": ["b"],
                    "occurred_at": "2026-11-27T00:02:00+08:00",
                },
                {
                    "parcel": "p",
                    "move": "m1",
                    "kind": "switch",
                    "from": "a",
                    "to": "b",
                    "proof": ["a"],
                    "occurred_at": "2026-11-27T00:02:00+08:00",
                },
            ]
        )
        switches, _ = collect_switches(moves)
        self.assertEqual(len(switches), 1)
        self.assertTrue(switches[0].confirmed)

    def test_one_sided_proof_does_not_move_custody(self):
        data = load_exchange(
            parcels=[{"parcel": "p", "shop": "s", "rate": "r1"}],
            rates=[
                {
                    "rate": "r1",
                    "floor_per_hour": 0,
                    "direct": 100,
                    "pickup": 10,
                    "relay": 20,
                    "delivery": 30,
                }
            ],
            shifts=[
                {
                    "shift": "a",
                    "station": "s",
                    "rider": "a",
                    "from": "2026-11-26T20:00:00+08:00",
                    "until": "2026-11-27T02:00:00+08:00",
                },
                {
                    "shift": "b",
                    "station": "s",
                    "rider": "b",
                    "from": "2026-11-26T23:00:00+08:00",
                    "until": "2026-11-27T03:00:00+08:00",
                },
            ],
            handoffs=[
                {
                    "parcel": "p",
                    "move": "p1",
                    "kind": "pickup",
                    "from": "s",
                    "to": "a",
                    "proof": ["s", "a"],
                    "occurred_at": "2026-11-26T22:00:00+08:00",
                },
                {
                    "parcel": "p",
                    "move": "m1",
                    "kind": "switch",
                    "from": "a",
                    "to": "b",
                    "proof": ["b"],
                    "occurred_at": "2026-11-26T23:30:00+08:00",
                },
                {
                    "parcel": "p",
                    "move": "d1",
                    "kind": "delivery",
                    "from": "a",
                    "to": "u",
                    "proof": ["a", "u"],
                    "occurred_at": "2026-11-27T00:00:00+08:00",
                },
            ],
        )
        ledger = build_ledger(data)
        view = ledger.parcel_status("p")
        self.assertEqual(view["stage"], "delivered")
        # 只有一方确认的换手不改变归属：揽收人得全程，且整单因交接证据不全暂缓
        shares = [s for s in ledger.pieces if s.parcel == "p"]
        self.assertEqual({s.rider for s in shares}, {"a"})
        self.assertEqual(shares[0].component, "direct")
        self.assertEqual(shares[0].status, "pending")

    def test_switch_requires_receiver_commitment(self):
        data = load_exchange(
            parcels=[{"parcel": "p", "shop": "s", "rate": "r1"}],
            rates=[
                {
                    "rate": "r1",
                    "floor_per_hour": 0,
                    "direct": 100,
                    "pickup": 10,
                    "relay": 20,
                    "delivery": 30,
                }
            ],
            shifts=[
                {
                    "shift": "a",
                    "station": "s",
                    "rider": "a",
                    "from": "2026-11-26T20:00:00+08:00",
                    "until": "2026-11-27T02:00:00+08:00",
                },
                {
                    "shift": "other",
                    "station": "elsewhere",
                    "rider": "b",
                    "from": "2026-11-26T20:00:00+08:00",
                    "until": "2026-11-27T02:00:00+08:00",
                },
            ],
            handoffs=[
                {
                    "parcel": "p",
                    "move": "p1",
                    "kind": "pickup",
                    "from": "s",
                    "to": "a",
                    "proof": ["s", "a"],
                    "occurred_at": "2026-11-26T22:00:00+08:00",
                },
                {
                    "parcel": "p",
                    "move": "m1",
                    "kind": "switch",
                    "from": "a",
                    "to": "b",
                    "proof": ["a", "b"],
                    "occurred_at": "2026-11-26T23:30:00+08:00",
                },
            ],
        )
        ledger = build_ledger(data)
        self.assertTrue(
            any("接班资格" in d["reason"] for d in ledger.parcel_disputes)
        )
        view = ledger.parcel_status("p")
        self.assertEqual(view["stage"], "with_original")
        self.assertEqual(view["custodian"], "a")

    def test_pieces_split_across_handoff(self):
        ledger = build_ledger(sample_data())
        ns1 = sorted(
            (s for s in ledger.pieces if s.parcel == "ns1"),
            key=lambda s: s.occurred_at,
        )
        self.assertEqual([s.component for s in ns1], ["pickup", "relay", "delivery"])
        self.assertEqual([s.rider for s in ns1], ["r-a", "r-b", "r-b"])
        self.assertEqual(sum(s.amount_cents for s in ns1), 2900 + 2000 + 4500)
        # 计件不重复：每一元只记一次
        self.assertEqual(len({(s.component, s.rider) for s in ns1}), 3)

    def test_no_switch_pays_full_direct(self):
        ledger = build_ledger(sample_data())
        ns2 = [s for s in ledger.pieces if s.parcel == "ns2"]
        self.assertEqual(len(ns2), 1)
        self.assertEqual(ns2[0].component, "direct")
        self.assertEqual(ns2[0].amount_cents, 8700)

    def test_returned_parcel_pays_nothing(self):
        data = load_exchange(
            parcels=[{"parcel": "p", "shop": "s", "rate": "r1"}],
            rates=[
                {
                    "rate": "r1",
                    "floor_per_hour": 0,
                    "direct": 100,
                    "pickup": 10,
                    "relay": 20,
                    "delivery": 30,
                }
            ],
            shifts=[
                {
                    "shift": "a",
                    "station": "s",
                    "rider": "a",
                    "from": "2026-11-26T20:00:00+08:00",
                    "until": "2026-11-27T02:00:00+08:00",
                }
            ],
            handoffs=[
                {
                    "parcel": "p",
                    "move": "p1",
                    "kind": "pickup",
                    "from": "s",
                    "to": "a",
                    "proof": ["s", "a"],
                    "occurred_at": "2026-11-26T22:00:00+08:00",
                },
                {
                    "parcel": "p",
                    "move": "r1",
                    "kind": "return",
                    "from": "a",
                    "to": "s",
                    "proof": ["a", "s"],
                    "occurred_at": "2026-11-26T23:00:00+08:00",
                },
            ],
        )
        ledger = build_ledger(data)
        self.assertEqual([s for s in ledger.pieces if s.parcel == "p"], [])

    def test_next_handoff_keeps_rider_tail_shift(self):
        # 原骑手在自己班次结束后送完：计件仍归承接该单的班
        data = load_exchange(
            parcels=[{"parcel": "p", "shop": "s", "rate": "r1"}],
            rates=[
                {
                    "rate": "r1",
                    "floor_per_hour": 0,
                    "direct": 100,
                    "pickup": 10,
                    "relay": 20,
                    "delivery": 30,
                }
            ],
            shifts=[
                {
                    "shift": "a",
                    "station": "s",
                    "rider": "a",
                    "from": "2026-11-26T20:00:00+08:00",
                    "until": "2026-11-27T00:00:00+08:00",
                },
                {
                    "shift": "b",
                    "station": "s",
                    "rider": "b",
                    "from": "2026-11-26T23:50:00+08:00",
                    "until": "2026-11-27T02:00:00+08:00",
                },
            ],
            handoffs=[
                {
                    "parcel": "p",
                    "move": "p1",
                    "kind": "pickup",
                    "from": "s",
                    "to": "a",
                    "proof": ["s", "a"],
                    "occurred_at": "2026-11-26T23:30:00+08:00",
                },
                {
                    "parcel": "p",
                    "move": "d1",
                    "kind": "delivery",
                    "from": "a",
                    "to": "u",
                    "proof": ["a", "u"],
                    "occurred_at": "2026-11-27T00:30:00+08:00",
                },
            ],
        )
        ledger = build_ledger(data)
        shares = [s for s in ledger.pieces if s.parcel == "p"]
        self.assertEqual(len(shares), 1)
        self.assertEqual(shares[0].component, "direct")
        self.assertEqual(shares[0].shift, "a")
        self.assertEqual(shares[0].shift_source, "custody_shift")


class PayrollTest(unittest.TestCase):
    def test_floor_rounds_to_cent(self):
        ledger = build_ledger(sample_data())
        engine = PayrollEngine(ledger)
        pay = engine.shift_pay("nite-a")
        # 240 分钟 × 2600 分/小时 = 10400 分
        self.assertEqual(pay.floor_confirmed, 10400)
        self.assertEqual(pay.base_pay, max(10400, 11600))

    def test_higher_of_floor_and_pieces_per_shift(self):
        ledger = build_ledger(sample_data())
        engine = PayrollEngine(ledger)
        pay = engine.shift_pay("nite-a")
        self.assertEqual(pay.pieces_confirmed, 11600)
        self.assertEqual(pay.base_pay, 11600)
        self.assertNotEqual(
            pay.base_pay, pay.floor_confirmed + pay.pieces_confirmed
        )

    def test_pending_pieces_are_holdback_not_paid(self):
        ledger = build_ledger(sample_data())
        engine = PayrollEngine(ledger)
        pay = engine.shift_pay("nite-b")
        self.assertEqual(pay.base_pay, 2600)
        self.assertEqual(pay.pieces_pending, 6500)
        self.assertGreater(pay.holdback, 0)

    def test_midnight_shift_belongs_to_start_day(self):
        ledger = build_ledger(sample_data())
        engine = PayrollEngine(ledger)
        pay = engine.shift_pay("nite-b")
        self.assertEqual(pay.business_day, date(2026, 11, 26))
        self.assertEqual(pay.period_start, date(2026, 11, 23))

    def test_statement_lists_held_and_payable(self):
        ledger = build_ledger(sample_data())
        engine = PayrollEngine(ledger)
        statement = engine.statement("r-b", date(2026, 11, 23))
        self.assertEqual(statement.confirmed_total, 2600)
        self.assertGreater(statement.pending_total, 0)
        self.assertTrue(statement.pending_items)
        self.assertEqual(statement.payable_total, 2600)

    def test_cents_formatting(self):
        self.assertEqual(cents(11600), "116.00")
        self.assertEqual(cents(-50), "-0.50")

    def test_unassigned_shift_does_not_project_second_floor(self):
        ledger = build_ledger(sample_data())
        engine = PayrollEngine(ledger)
        pay = engine.shift_pay("nite-c")
        self.assertEqual(pay.base_pay, 0)
        self.assertEqual(pay.holdback, 0)
        self.assertEqual(pay.potential_pay, 0)
        # r-b 整期只有一个保底，不会因为两个站各排一段而出现两份
        statement = engine.statement("r-b", date(2026, 11, 23))
        floors = [p.floor_confirmed for p in statement.shift_pays]
        self.assertEqual(sum(1 for f in floors if f > 0), 1)

    def test_pending_and_confirmed_do_not_overlap(self):
        ledger = build_ledger(sample_data())
        for facts in ledger.facts.values():
            for a in facts.confirmed:
                for b in facts.pending:
                    self.assertIsNone(a.intersection(b))


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        data = load_exchange_dir(SAMPLES)
        for name in ("shifts", "attendance", "parcels", "handoffs", "rates"):
            for row in getattr(data, name):
                self.store.append(name, dict(row))
        self.store.append("actors", {"actor": "admin", "role": "coordinator"})
        self.service = Service(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def _register(self, actor, role, **extra):
        return self.service.register_actor(actor=actor, role=role, by="admin", **extra)

    def test_riders_only_see_their_own_records(self):
        self._register("r-a", "rider")
        self._register("r-b", "rider")
        view = self.service.rider_view("r-a")
        self.assertEqual({row["shift"] for row in view["shifts"]}, {"nite-a"})
        self.assertEqual(view["rider"], "r-a")
        with self.assertRaises(AccessError):
            self.service.rider_view_for("r-a", "r-b")

    def test_shop_cannot_see_payroll(self):
        self._register("shop-pine", "shop", shop="store-pine")
        with self.assertRaises(AccessError):
            self.service.payroll_view("shop-pine", "2026-11-23")
        view = self.service.shop_view("shop-pine")
        self.assertEqual({p["parcel"] for p in view["parcels"]}, {"ns1", "ns2"})

    def test_scheduler_cannot_issue(self):
        self._register("sched-1", "scheduler")
        with self.assertRaises(AccessError):
            self.service.issue_period(by="sched-1", period_start="2026-11-23")

    def test_rider_cannot_appeal_someone_elses_shift(self):
        self._register("r-a", "rider")
        self._register("r-b", "rider")
        # r-a 不能申诉 nite-b
        with self.assertRaises(AccessError):
            self.service.file_appeal(
                by="r-a", kind="attendance", reason="x", shift="nite-b"
            )

    def test_handover_confirmation_requires_party(self):
        self._register("r-a", "rider")
        self._register("r-b", "rider")
        self._register("r-c", "rider")
        with self.assertRaises(AccessError):
            self.service.confirm_handover(
                by="r-c",
                move="x1",
                parcel="ns1",
                sender="r-a",
                receiver="r-b",
                occurred_at="2026-11-27T00:05:00+08:00",
            )
        # 当事人之一可以确认
        record = self.service.confirm_handover(
            by="r-b",
            move="ns1-x",
            parcel="ns1",
            sender="r-a",
            receiver="r-b",
            occurred_at="2026-11-27T00:05:00+08:00",
        )
        self.assertEqual(record["proof"], ["r-b"])

    def test_issuance_is_immutable_and_delta_is_separate(self):
        self._register("pay-1", "payroll")
        issued = self.service.issue_period(by="pay-1", period_start="2026-11-23")
        self.assertEqual(len(issued), 2)
        # 再次签发同一期不会改写已签发记录，只报告跳过
        again = self.service.issue_period(by="pay-1", period_start="2026-11-23")
        self.assertEqual(len(again), 1)
        self.assertEqual(len(again[0]["skipped"]), 2)
        # 协调员用更正生成差额，挂到下一期
        result = self.service.correction_delta(
            by="admin",
            rider="r-a",
            period="2026-11-23~2026-12-06",
            affected_shifts=["nite-a"],
            reason="申诉核实",
        )
        self.assertTrue(result["changed"])
        self.assertEqual(result["issuance"], "iss-1")
        self.assertEqual(result["scheduled_period"], "2026-12-07~2026-12-20")
        # 差额 = 新算班薪 - 原签发班薪；再更正一次不会重复计同一段
        self.assertEqual(result["lines"][0]["delta_cents"], 0)
        second = self.service.correction_delta(
            by="admin",
            rider="r-a",
            period="2026-11-23~2026-12-06",
            affected_shifts=["nite-a"],
            reason="再次核对",
        )
        self.assertEqual(second["lines"][0]["delta_cents"], 0)

    def test_appeal_and_correction_do_not_double_pay(self):
        # 一份有揽收、缺送达的记录：骑手申诉“已送达但未计入”
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = make_store(
            tmp.name,
            shifts=[
                {
                    "shift": "s1",
                    "station": "store-x",
                    "rider": "r-x",
                    "from": "2026-11-26T20:00:00+08:00",
                    "until": "2026-11-27T02:00:00+08:00",
                }
            ],
            attendance=[
                {
                    "event": "i",
                    "shift": "s1",
                    "kind": "in",
                    "earliest": "2026-11-26T20:00:00+08:00",
                    "latest": "2026-11-26T20:00:00+08:00",
                },
                {
                    "event": "o",
                    "shift": "s1",
                    "kind": "out",
                    "earliest": "2026-11-27T02:00:00+08:00",
                    "latest": "2026-11-27T02:00:00+08:00",
                },
            ],
            parcels=[{"parcel": "px", "shop": "store-x", "rate": "r1"}],
            rates=RATES,
            handoffs=[
                {
                    "parcel": "px",
                    "move": "p1",
                    "kind": "pickup",
                    "from": "store-x",
                    "to": "r-x",
                    "proof": ["store-x", "r-x"],
                    "occurred_at": "2026-11-26T21:00:00+08:00",
                }
            ],
        )
        store.append("actors", {"actor": "r-x", "role": "rider"})
        service = Service(store)
        before = [p for p in service.ledger.pieces if p.parcel == "px"]
        self.assertTrue(before)
        self.assertTrue(all(p.status == "pending" for p in before))

        appeal = service.file_appeal(
            by="r-x", kind="missing_delivery", reason="已送达未计入", parcel="px"
        )
        self.assertEqual(appeal["status"], "open")
        with self.assertRaises(StoreError):
            # 不存在的班次直接拒绝
            service.file_appeal(
                by="r-x", kind="attendance", reason="x", shift="not-mine"
            )
        result = service.resolve_appeal(
            by="admin",
            appeal=appeal["appeal"],
            kind="delivery",
            decision="corrected",
            reason="买家确认送达",
            parcel="px",
            occurred_at="2026-11-26T23:00:00+08:00",
            rider="r-x",
        )
        self.assertIsNotNone(result["correction"])
        pieces = [p for p in service.ledger.pieces if p.parcel == "px"]
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0].rider, "r-x")
        self.assertEqual(pieces[0].status, "confirmed")
        # 同一送达只产生一份计件
        self.assertEqual(
            sum(1 for p in service.ledger.pieces if p.parcel == "px"), 1
        )

    def test_continuation_resolves_gap_once(self):
        self._register("r-a", "rider")
        record = self.service.confirm_continuation(
            by="r-a",
            original_shift="nite-a",
            successor_shift="nite-b",
            starts="2026-11-27T00:00:00+08:00",
            until="2026-11-27T00:01:00+08:00",
        )
        self.assertEqual(record["proof"], ["nite-a", "r-a"])
        findings = self.service.ledger
        self.assertEqual(findings.handover_gaps[0]["status"], "resolved_by_continuation")
        nite_a = findings.facts["nite-a"]
        nite_b = findings.facts["nite-b"]
        self.assertEqual(nite_a.confirmed_minutes, 241)
        # 同一承诺只归一位责任人：接班班不再计这段
        self.assertFalse(
            any(
                i.start <= iso("2026-11-26T16:00:00+00:00") < i.end
                for i in nite_b.confirmed
            )
        )

    def test_two_terminals_see_the_same_facts(self):
        """另一台终端重新接入，重放得到同一组事实。"""

        self._register("pay-1", "payroll")
        first = self.service.issue_period(by="pay-1", period_start="2026-11-23")
        second = Service(Store(self.store.root))
        self.assertEqual(
            second.ledger.describe(), self.service.ledger.describe()
        )
        self.assertEqual(
            second.payroll.statements(date(2026, 11, 23))[0].confirmed_total,
            self.service.payroll.statements(date(2026, 11, 23))[0].confirmed_total,
        )
        self.assertEqual(
            [row["issuance"] for row in second._issuances.values()],
            [row["issuance"] for row in first],
        )


class CorrectionTest(unittest.TestCase):
    def _issued_store(self, directory):
        store = make_store(
            directory,
            shifts=[
                {
                    "shift": "s1",
                    "station": "store-x",
                    "rider": "r-x",
                    "from": "2026-11-26T21:00:00+08:00",
                    "until": "2026-11-26T22:00:00+08:00",
                }
            ],
            attendance=[
                {
                    "event": "i",
                    "shift": "s1",
                    "kind": "in",
                    "earliest": "2026-11-26T21:00:00+08:00",
                    "latest": "2026-11-26T21:00:00+08:00",
                },
                {
                    "event": "o",
                    "shift": "s1",
                    "kind": "out",
                    "earliest": "2026-11-26T22:00:00+08:00",
                    "latest": "2026-11-26T22:00:00+08:00",
                },
            ],
            parcels=[{"parcel": "px", "shop": "store-x", "rate": "r1"}],
            rates=RATES,
            handoffs=[
                {
                    "parcel": "px",
                    "move": "p1",
                    "kind": "pickup",
                    "from": "store-x",
                    "to": "r-x",
                    "proof": ["store-x", "r-x"],
                    "occurred_at": "2026-11-26T21:15:00+08:00",
                }
            ],
        )
        store.append("actors", {"actor": "r-x", "role": "rider"})
        store.append("actors", {"actor": "pay-1", "role": "payroll"})
        return store

    def test_correction_after_issuance_forms_separate_delta(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = Service(self._issued_store(tmp.name))
        issued = service.issue_period(by="pay-1", period_start="2026-11-23")
        self.assertEqual(len(issued), 1)
        original_total = issued[0]["total_cents"]

        appeal = service.file_appeal(
            by="r-x", kind="missing_delivery", reason="已送达", parcel="px"
        )
        service.resolve_appeal(
            by="admin",
            appeal=appeal["appeal"],
            kind="delivery",
            decision="corrected",
            reason="核实",
            parcel="px",
            occurred_at="2026-11-26T21:45:00+08:00",
            rider="r-x",
        )
        result = service.correction_delta(
            by="admin",
            rider="r-x",
            period=issued[0]["period"],
            affected_shifts=["s1"],
            reason="送达补记",
        )
        self.assertTrue(result["changed"])
        self.assertEqual(result["issuance"], issued[0]["issuance"])
        self.assertNotEqual(result["amount_cents"], 0)
        # 原签发记录保持不变
        stored = service.store.read("issuances")[0]
        self.assertEqual(stored["total_cents"], original_total)
        # 同一更正在下一期工资单中只列一次
        next_statement = service.payroll.statement("r-x", date(2026, 12, 7))
        from nightops.payroll import apply_adjustments

        apply_adjustments(
            next_statement,
            [
                row
                for row in service._adjustments
                if row.get("scheduled_period") == next_statement.period
            ],
            None,
        )
        self.assertEqual(len(next_statement.adjustments), 1)
        self.assertEqual(
            next_statement.adjustments_total, result["amount_cents"]
        )

    def test_attendance_correction_changes_only_scope(self):
        data = load_exchange_dir(SAMPLES)
        ledger = build_ledger(data)
        base = ledger.facts["nite-b"].confirmed_minutes
        corrected = build_ledger(
            data,
            corrections=[
                {
                    "correction": "c1",
                    "kind": "attendance",
                    "shift": "nite-b",
                    "from": "2026-11-26T23:59:00+08:00",
                    "until": "2026-11-27T00:01:00+08:00",
                    "status": "confirmed",
                    "reason": "终端对时核实",
                    "coordinator": "c",
                    "created_at": "2026-11-26T12:00:00+08:00",
                    "appeal": "a1",
                }
            ],
        )
        self.assertGreater(corrected.facts["nite-b"].confirmed_minutes, base)

    def test_switch_time_correction_moves_piece_window(self):
        rows = [
            {
                "correction": "c2",
                "kind": "switch",
                "move": "a2",
                "occurred_at": "2026-11-27T00:04:00+08:00",
                "reason": "交班时刻核实",
                "coordinator": "c",
                "created_at": "2026-11-26T12:00:00+08:00",
                "appeal": "a2",
            }
        ]
        data = load_exchange_dir(SAMPLES)
        corrected = build_ledger(data, corrections=rows)
        relay = next(
            p for p in corrected.pieces if p.parcel == "ns1" and p.component == "relay"
        )
        self.assertEqual(relay.occurred_at, iso("2026-11-27T00:04:00+08:00"))

    def test_correction_outside_shift_is_rejected(self):
        data = load_exchange_dir(SAMPLES)
        ledger = build_ledger(
            data,
            corrections=[
                {
                    "correction": "cx",
                    "kind": "attendance",
                    "shift": "nite-b",
                    "from": "2026-11-28T00:00:00+08:00",
                    "until": "2026-11-28T01:00:00+08:00",
                    "status": "confirmed",
                    "reason": "越界",
                    "coordinator": "c",
                    "created_at": "2026-11-26T12:00:00+08:00",
                }
            ],
        )
        self.assertTrue(ledger.corrections_rejected)
        self.assertEqual(ledger.corrections_applied, [])


class SampleTest(unittest.TestCase):
    def test_sample_ledger_summary(self):
        ledger = build_ledger(sample_data())
        summary = ledger.describe()
        self.assertEqual(len(summary["shifts"]), 3)
        self.assertEqual(len(summary["pieces"]), 4)
        self.assertEqual(
            [p["stage"] for p in summary["parcels"]],
            ["delivered", "delivered", "unknown"],
        )
        self.assertTrue(ledger.handover_gaps)


if __name__ == "__main__":
    unittest.main()
