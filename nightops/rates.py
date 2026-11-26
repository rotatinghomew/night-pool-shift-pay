"""费率：整数分。保底按分钟求和后按班取整到分。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from .exchange import Record


class RateError(ValueError):
    """费率不符合约定。"""


@dataclass(frozen=True)
class Rate:
    rate: str
    floor_per_hour: int
    direct: int
    pickup: int
    relay: int
    delivery: int

    def floor_for_minutes(self, minutes: int) -> int:
        """保底按分钟求和后按班取整到分（四舍五入）。"""

        if minutes <= 0:
            return 0
        raw = Decimal(self.floor_per_hour) * Decimal(minutes) / Decimal(60)
        return int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def describe(self) -> dict:
        return {
            "rate": self.rate,
            "floor_per_hour": self.floor_per_hour,
            "direct": self.direct,
            "pickup": self.pickup,
            "relay": self.relay,
            "delivery": self.delivery,
            "unit": "cent",
        }


def read_rates(rows: Iterable[Record]) -> dict[str, Rate]:
    rates: dict[str, Rate] = {}
    for index, row in enumerate(rows):
        for key in ("rate", "floor_per_hour", "direct", "pickup", "relay", "delivery"):
            if key not in row:
                raise RateError(f"rates[{index}] 缺少 {key}")
        values = {}
        for key in ("floor_per_hour", "direct", "pickup", "relay", "delivery"):
            value = row[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise RateError(f"rates[{index}].{key} 必须是非负整数分")
            values[key] = value
        rate_id = str(row["rate"])
        if rate_id in rates:
            raise RateError(f"费率编号重复: {rate_id}")
        rates[rate_id] = Rate(rate=rate_id, **values)
    return rates
