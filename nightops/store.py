"""追加式资料库：让不同角色重复接入同一组记录。

目录布局（均为 NDJSON，追加写入，读取时保持原顺序）：

* shifts / attendance / parcels / handoffs / rates —— 与交换约定一致；
* acceptances —— 骑手接受邀请；
* continuations —— 交班空档的双方续守确认；
* corrections —— 协调员核实后的更正；
* appeals —— 骑手申诉；
* issuances —— 已签发的工资；
* events —— 审计流水，记录谁在何时做了什么。

记录一旦写入不再改写；更正、申诉和签发都是新记录。快照通过重放得到，
因此任何一台终端重新接入都会看到同一组事实。
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .exchange import ExchangeData

try:  # pragma: no cover - 非 POSIX 平台退回无锁写法
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


FILES = (
    "shifts",
    "attendance",
    "parcels",
    "handoffs",
    "rates",
    "acceptances",
    "continuations",
    "corrections",
    "adjustments",
    "appeals",
    "issuances",
    "events",
    "actors",
)


class StoreError(RuntimeError):
    """资料库读写失败。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.root / ".lock"

    # ------------------------------------------------------------------ 路径
    def path(self, name: str) -> Path:
        if name not in FILES:
            raise StoreError(f"未知资料: {name}")
        return self.root / f"{name}.ndjson"

    # ------------------------------------------------------------------ 读
    def read(self, name: str) -> list[dict[str, Any]]:
        path = self.path(name)
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise StoreError(f"{path.name}:{lineno} 不是合法 JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise StoreError(f"{path.name}:{lineno} 不是 JSON 对象")
            rows.append(row)
        return rows

    # ------------------------------------------------------------------ 写
    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        handle = open(self._lock_path, "a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def append(self, name: str, record: dict[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        payload.setdefault("recorded_at", _utcnow().isoformat())
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._locked():
            with open(self.path(name), "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return payload

    def append_many(self, name: str, records: Iterable[dict[str, Any]]) -> list[dict]:
        return [self.append(name, record) for record in records]

    # --------------------------------------------------------------- 审计
    def record_event(
        self,
        *,
        actor: str,
        role: str,
        action: str,
        target: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        return self.append(
            "events",
            {
                "event": f"evt-{self.next_sequence('events')}",
                "actor": actor,
                "role": role,
                "action": action,
                "at": (at or _utcnow()).isoformat(),
                "target": target or {},
            },
        )

    def next_sequence(self, name: str) -> int:
        return len(self.read(name)) + 1

    # ----------------------------------------------------------------- 摘要
    def digest(self) -> str:
        """对全部业务资料做内容摘要，用于说明签发依据。"""

        sha = hashlib.sha256()
        for name in FILES:
            if name in ("events", "actors"):
                continue
            path = self.path(name)
            sha.update(name.encode("utf-8"))
            if path.exists():
                sha.update(path.read_bytes())
        return sha.hexdigest()

    def snapshot(self) -> dict[str, int]:
        return {name: len(self.read(name)) for name in FILES}

    # ------------------------------------------------------------- 交换资料
    def exchange(self) -> ExchangeData:
        acceptances = {row["shift"]: row for row in self.read("acceptances")}
        shifts: list[dict[str, Any]] = []
        for row in self.read("shifts"):
            merged = dict(row)
            accepted = acceptances.get(row.get("shift"))
            if accepted is not None and accepted.get("rider") == row.get("rider"):
                merged["accepted"] = accepted.get("accepted", True)
                if accepted.get("accepted_at"):
                    merged["accepted_at"] = accepted["accepted_at"]
            elif accepted is not None:
                merged["accepted"] = False
            shifts.append(merged)
        return ExchangeData(
            shifts=tuple(shifts),
            attendance=tuple(self.read("attendance")),
            parcels=tuple(self.read("parcels")),
            handoffs=tuple(self.read("handoffs")),
            rates=tuple(self.read("rates")),
            continuations=tuple(self.read("continuations")),
            corrections=tuple(self.read("corrections")),
        ).with_source()

    def exchange_rows(
        self, **overrides: Iterable[dict[str, Any]] | None
    ) -> ExchangeData:
        """在现有资料上叠加临时记录，用于“如果更正生效”的试算。"""

        data = self.exchange()
        merged = {
            "shifts": list(data.shifts),
            "attendance": list(data.attendance),
            "parcels": list(data.parcels),
            "handoffs": list(data.handoffs),
            "rates": list(data.rates),
            "continuations": list(data.continuations),
            "corrections": list(data.corrections),
        }
        for key, rows in overrides.items():
            if rows is not None:
                merged[key] = merged[key] + list(rows)
        return ExchangeData(**{k: tuple(v) for k, v in merged.items()}).with_source()

    # ------------------------------------------------------------- 角色登记
    def actors(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in self.read("actors"):
            actor = row.get("actor")
            if actor is not None:
                result[str(actor)] = row
        return result
