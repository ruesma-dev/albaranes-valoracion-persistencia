# domain/models/unit_models.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

UnitCategory = Literal[
    "mass", "volume", "length", "area", "count", "time", "lump_sum", "unknown",
]


@dataclass(frozen=True)
class UnitInfo:
    alias: str            # la unidad tal cual venía (normalizada)
    category: UnitCategory
    factor_to_base: float
    ambiguous: bool       # True para caja/saco/pallet... (revisión manual)


@dataclass(frozen=True)
class ConversionResult:
    category: UnitCategory
    category_match: bool
    factor: float | None   # factor de FROM_unit a TO_unit
    converted_quantity: float | None
    ambiguous: bool
    reason: str | None
