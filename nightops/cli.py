"""夜间共配值守交班与薪资的命令行入口。

用法示例::

    python3 -m nightops.cli init --store data
    python3 -m nightops.cli import --store data --from-dir samples
    python3 -m nightops.cli register --store data --by admin --actor r-a --role rider
    python3 -m nightops.cli rider --store data --as r-a --period 2026-11-23
    python3 -m nightops.cli issue --store data --as pay-1 --period 2026-11-23

所有子命令都读写同一资料库；不同终端重复接入得到同一组事实。
"""

from __future__ import annotations

import argparse
import json
import sys


from .exchange import load_exchange_dir
from .service import AccessError, Service
from .store import Store, StoreError


def _emit(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def _service(args) -> Service:
    store = Store(args.store)
    return Service(store, rate=args.rate)


def cmd_init(args) -> int:
    store = Store(args.store)
    if args.actor:
        store.append(
            "actors",
            {
                "actor": args.actor,
                "role": args.role or "coordinator",
                "station": args.station,
                "shop": args.shop,
            },
        )
    _emit({"store": str(store.root), "files": store.snapshot()})
    return 0


def cmd_import(args) -> int:
    store = Store(args.store)
    data = load_exchange_dir(args.from_dir)
    counts = {}
    for name in ("shifts", "attendance", "parcels", "handoffs", "rates", "continuations", "corrections"):
        rows = getattr(data, name)
        for row in rows:
            store.append(name, dict(row))
        counts[name] = len(rows)
    _emit(counts)
    return 0


def cmd_register(args) -> int:
    service = _service(args)
    actor = service.register_actor(
        actor=args.actor,
        role=args.role,
        station=args.station,
        shop=args.shop,
        by=args.by,
    )
    _emit({
        "actor": actor.actor,
        "role": actor.role,
        "station": actor.station,
        "shop": actor.shop,
    })
    return 0


def cmd_rider(args) -> int:
    service = _service(args)
    if args.for_rider:
        payload = service.rider_view_for(args.as_actor, args.for_rider, args.period)
    else:
        payload = service.rider_view(args.as_actor, args.period)
    _emit(payload)
    return 0


def cmd_shop(args) -> int:
    service = _service(args)
    _emit(service.shop_view(args.as_actor))
    return 0


def cmd_schedule(args) -> int:
    service = _service(args)
    _emit(service.scheduler_view(args.as_actor))
    return 0


def cmd_payroll(args) -> int:
    service = _service(args)
    _emit(service.payroll_view(args.as_actor, args.period))
    return 0


def cmd_coordinator(args) -> int:
    service = _service(args)
    _emit(service.coordinator_view(args.as_actor))
    return 0


def cmd_issue(args) -> int:
    service = _service(args)
    issued = service.issue_period(
        by=args.as_actor,
        period_start=args.period,
        riders=args.riders,
        dry_run=args.dry_run,
    )
    _emit(issued)
    return 0


def cmd_parcel(args) -> int:
    service = _service(args)
    _emit(service.ledger.parcel_status(args.parcel))
    return 0


def cmd_accept(args) -> int:
    service = _service(args)
    _emit(
        service.accept_invite(
            by=args.as_actor, shift=args.shift, accepted=not args.decline
        )
    )
    return 0


def cmd_attendance(args) -> int:
    service = _service(args)
    _emit(
        service.check_in(
            by=args.as_actor,
            shift=args.shift,
            event=args.event,
            kind=args.kind,
            earliest=args.earliest,
            latest=args.latest,
            device=args.device,
        )
    )
    return 0


def cmd_appeal(args) -> int:
    service = _service(args)
    _emit(
        service.file_appeal(
            by=args.as_actor,
            kind=args.kind,
            reason=args.reason,
            shift=args.shift,
            parcel=args.parcel,
            starts=args.starts,
            until=args.until,
        )
    )
    return 0


def cmd_resolve(args) -> int:
    service = _service(args)
    _emit(
        service.resolve_appeal(
            by=args.as_actor,
            appeal=args.appeal,
            kind=args.kind,
            decision=args.decision,
            reason=args.reason,
            shift=args.shift,
            parcel=args.parcel,
            starts=args.starts,
            until=args.until,
            status=args.status,
            move=args.move,
            occurred_at=args.occurred_at,
        )
    )
    return 0


def cmd_correct(args) -> int:
    service = _service(args)
    _emit(
        service.correction_delta(
            by=args.as_actor,
            rider=args.for_rider,
            period=args.period,
            affected_shifts=args.shift,
            reason=args.reason,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nightops", description=__doc__)
    parser.add_argument("--store", default="data", help="资料库目录")
    parser.add_argument("--rate", default=None, help="默认费率编号")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="初始化资料库")
    p.add_argument("--actor")
    p.add_argument("--role", default=None)
    p.add_argument("--station")
    p.add_argument("--shop")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("import", help="导入交换资料")
    p.add_argument("--from-dir", required=True)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("register", help="登记身份")
    p.add_argument("--by", required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--station")
    p.add_argument("--shop")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("rider", help="骑手查询自己的记录")
    p.add_argument("--as", dest="as_actor", required=True)
    p.add_argument("--for-rider", dest="for_rider")
    p.add_argument("--period")
    p.set_defaults(func=cmd_rider)

    p = sub.add_parser("shop", help="门店查询")
    p.add_argument("--as", dest="as_actor", required=True)
    p.set_defaults(func=cmd_shop)

    p = sub.add_parser("schedule", help="排班员查询")
    p.add_argument("--as", dest="as_actor", required=True)
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("payroll", help="发薪查询")
    p.add_argument("--as", dest="as_actor", required=True)
    p.add_argument("--period", required=True)
    p.set_defaults(func=cmd_payroll)

    p = sub.add_parser("coordinator", help="协调员查询")
    p.add_argument("--as", dest="as_actor", required=True)
    p.set_defaults(func=cmd_coordinator)

    p = sub.add_parser("issue", help="签发工资期")
    p.add_argument("--as", dest="as_actor", required=True)
    p.add_argument("--period", required=True)
    p.add_argument("--riders", nargs="*")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_issue)

    p = sub.add_parser("parcel", help="查单")
    p.add_argument("--as", dest="as_actor")
    p.add_argument("parcel")
    p.set_defaults(func=cmd_parcel)

    p = sub.add_parser("accept", help="接受邀请")
    p.add_argument("--as", dest="as_actor", required=True)
    p.add_argument("--shift", required=True)
    p.add_argument("--decline", action="store_true")
    p.set_defaults(func=cmd_accept)

    p = sub.add_parser("attendance", help="签到/签退回传")
    p.add_argument("--as", dest="as_actor", required=True)
    p.add_argument("--shift", required=True)
    p.add_argument("--event", required=True)
    p.add_argument("--kind", choices=["in", "out"], required=True)
    p.add_argument("--earliest", required=True)
    p.add_argument("--latest")
    p.add_argument("--device", default="")
    p.set_defaults(func=cmd_attendance)

    p = sub.add_parser("appeal", help="骑手申诉")
    p.add_argument("--as", dest="as_actor", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--shift")
    p.add_argument("--parcel")
    p.add_argument("--starts")
    p.add_argument("--until")
    p.set_defaults(func=cmd_appeal)

    p = sub.add_parser("resolve", help="协调员处理申诉")
    p.add_argument("--as", dest="as_actor", required=True)
    p.add_argument("--appeal", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--decision", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--shift")
    p.add_argument("--parcel")
    p.add_argument("--starts")
    p.add_argument("--until")
    p.add_argument("--status")
    p.add_argument("--move")
    p.add_argument("--occurred-at", dest="occurred_at")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("correct", help="生成已签发工资的差额")
    p.add_argument("--as", dest="as_actor", required=True)
    p.add_argument("--for-rider", dest="for_rider", required=True)
    p.add_argument("--period", required=True)
    p.add_argument("--shift", action="append", default=[])
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_correct)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (AccessError, StoreError, KeyError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
