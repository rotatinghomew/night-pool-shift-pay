"""读取交换资料。

每一行是一条独立记录，行序按原始提供顺序保留。读取时只做结构校验，
不替调用方推断业务事实。除约定的五份资料外，可选读取续守确认
（continuations.ndjson），它表示原骑手与接班人双方对交班空档的确认。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

REQUIRED_FILES = (
    "shifts",
    "attendance",
    "parcels",
    "handoffs",
    "rates",
)

OPTIONAL_FILES = ("continuations", "corrections")

FILES = REQUIRED_FILES + OPTIONAL_FILES

Record = dict[str, Any]


class ExchangeError(ValueError):
    """交换资料不符合约定格式。"""


def _read_rows(path: Path) -> list[tuple[int, Record]]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 直接由调用方处理
        raise ExchangeError(f"无法读取 {path}: {exc}") from exc
    rows: list[tuple[int, Record]] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExchangeError(f"{path.name}:{lineno} 不是合法 JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise ExchangeError(f"{path.name}:{lineno} 不是 JSON 对象")
        rows.append((lineno, row))
    return rows


@dataclass(frozen=True)
class ExchangeData:
    """按文件分组的原始记录，保留行序。"""

    shifts: tuple[Record, ...] = ()
    attendance: tuple[Record, ...] = ()
    parcels: tuple[Record, ...] = ()
    handoffs: tuple[Record, ...] = ()
    rates: tuple[Record, ...] = ()
    continuations: tuple[Record, ...] = ()
    corrections: tuple[Record, ...] = ()
    source: Mapping[str, tuple[Record, ...]] = field(default_factory=dict)

    def with_source(self) -> "ExchangeData":
        return ExchangeData(
            shifts=self.shifts,
            attendance=self.attendance,
            parcels=self.parcels,
            handoffs=self.handoffs,
            rates=self.rates,
            continuations=self.continuations,
            corrections=self.corrections,
            source={
                "shifts": self.shifts,
                "attendance": self.attendance,
                "parcels": self.parcels,
                "handoffs": self.handoffs,
                "rates": self.rates,
                "continuations": self.continuations,
                "corrections": self.corrections,
            },
        )


def _coerce_rows(value: Iterable[Record] | None) -> tuple[Record, ...]:
    if value is None:
        return ()
    rows = tuple(value)
    for row in rows:
        if not isinstance(row, dict):
            raise ExchangeError("记录必须是 JSON 对象")
    return rows


def load_exchange(
    *,
    shifts: Iterable[Record] | None = None,
    attendance: Iterable[Record] | None = None,
    parcels: Iterable[Record] | None = None,
    handoffs: Iterable[Record] | None = None,
    rates: Iterable[Record] | None = None,
    continuations: Iterable[Record] | None = None,
    corrections: Iterable[Record] | None = None,
) -> ExchangeData:
    """从内存中的记录构造交换资料。"""

    return ExchangeData(
        shifts=_coerce_rows(shifts),
        attendance=_coerce_rows(attendance),
        parcels=_coerce_rows(parcels),
        handoffs=_coerce_rows(handoffs),
        rates=_coerce_rows(rates),
        continuations=_coerce_rows(continuations),
        corrections=_coerce_rows(corrections),
    ).with_source()


def load_exchange_dir(directory: str | Path) -> ExchangeData:
    """从目录读取交换资料；续守确认为可选文件。"""

    base = Path(directory)
    if not base.is_dir():
        raise ExchangeError(f"{base} 不是目录")
    data: dict[str, tuple[Record, ...]] = {}
    for name in FILES:
        path = base / f"{name}.ndjson"
        if path.exists():
            data[name] = tuple(row for _, row in _read_rows(path))
        elif name in REQUIRED_FILES:
            raise ExchangeError(f"缺少 {path.name}")
        else:
            data[name] = ()
    return ExchangeData(**data).with_source()
