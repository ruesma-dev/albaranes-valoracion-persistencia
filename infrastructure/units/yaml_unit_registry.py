# infrastructure/units/yaml_unit_registry.py
from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml

from domain.models.unit_models import ConversionResult, UnitCategory, UnitInfo
from domain.ports.unit_registry_port import UnitRegistry

logger = logging.getLogger(__name__)


class YamlUnitRegistry(UnitRegistry):
    def __init__(self, yaml_path: str | Path) -> None:
        self._path = Path(yaml_path)
        self._by_alias: dict[str, UnitInfo] = {}
        self._ambiguous: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            raise FileNotFoundError(
                f"No existe unit_registry YAML: {self._path}"
            )
        raw = self._path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
        if not isinstance(data, dict):
            raise ValueError(
                f"Formato inválido en {self._path}; se esperaba mapping."
            )

        ambiguous_list = data.get("ambiguous_units") or []
        self._ambiguous = {
            self._normalize(str(u)) for u in ambiguous_list
        }

        categories = data.get("categories") or {}
        if not isinstance(categories, dict):
            raise ValueError("Sección 'categories' inválida.")

        for cat_name, cat_payload in categories.items():
            if not isinstance(cat_payload, dict):
                continue
            units = cat_payload.get("units") or {}
            if not isinstance(units, dict):
                continue
            for alias, factor in units.items():
                norm = self._normalize(str(alias))
                if not norm:
                    continue
                try:
                    factor_float = float(factor)
                except Exception:
                    logger.warning(
                        "Factor inválido para %s en %s: %r (ignorado)",
                        alias, cat_name, factor,
                    )
                    continue
                self._by_alias[norm] = UnitInfo(
                    alias=norm,
                    category=cat_name,  # type: ignore[arg-type]
                    factor_to_base=factor_float,
                    ambiguous=norm in self._ambiguous,
                )

        logger.info(
            "[unit-registry] cargadas %s unidades; ambiguas=%s",
            len(self._by_alias), len(self._ambiguous),
        )

    # ---------------------------------------------------------------- #
    # API
    # ---------------------------------------------------------------- #
    def classify(self, unit: str | None) -> UnitInfo:
        if unit is None:
            return UnitInfo(
                alias="",
                category="unknown",
                factor_to_base=1.0,
                ambiguous=False,
            )
        norm = self._normalize(unit)
        info = self._by_alias.get(norm)
        if info is not None:
            return info
        if norm.endswith("s") and norm[:-1] in self._by_alias:
            return self._by_alias[norm[:-1]]
        return UnitInfo(
            alias=norm,
            category="unknown",
            factor_to_base=1.0,
            ambiguous=False,
        )

    def convert(
        self,
        *,
        quantity: float | None,
        from_unit: str | None,
        to_unit: str | None,
    ) -> ConversionResult:
        if quantity is None:
            return ConversionResult(
                category="unknown",
                category_match=False,
                factor=None,
                converted_quantity=None,
                ambiguous=False,
                reason="no_quantity",
            )
        info_from = self.classify(from_unit)
        info_to = self.classify(to_unit)

        if info_from.category == "unknown" or info_to.category == "unknown":
            return ConversionResult(
                category=(
                    info_from.category
                    if info_from.category != "unknown"
                    else info_to.category
                ),
                category_match=False,
                factor=None,
                converted_quantity=None,
                ambiguous=info_from.ambiguous or info_to.ambiguous,
                reason="unit_unknown",
            )

        if info_from.category != info_to.category:
            return ConversionResult(
                category=info_from.category,
                category_match=False,
                factor=None,
                converted_quantity=None,
                ambiguous=False,
                reason=(
                    f"category_mismatch:{info_from.category}!={info_to.category}"
                ),
            )

        # quantity_base = quantity * factor_from
        # converted = quantity_base / factor_to
        if info_to.factor_to_base == 0:
            return ConversionResult(
                category=info_from.category,
                category_match=False,
                factor=None,
                converted_quantity=None,
                ambiguous=False,
                reason="zero_factor_to_base_in_destination_unit",
            )

        factor = info_from.factor_to_base / info_to.factor_to_base
        converted = float(quantity) * factor
        ambiguous = info_from.ambiguous or info_to.ambiguous

        return ConversionResult(
            category=info_from.category,
            category_match=True,
            factor=factor,
            converted_quantity=converted,
            ambiguous=ambiguous,
            reason=None,
        )

    # ---------------------------------------------------------------- #
    # Helpers
    # ---------------------------------------------------------------- #
    @staticmethod
    def _normalize(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value)
        ascii_ = "".join(
            ch for ch in normalized if not unicodedata.combining(ch)
        )
        lowered = ascii_.lower()
        cleaned = re.sub(r"[\s./\\\-]+", "", lowered)
        return cleaned
