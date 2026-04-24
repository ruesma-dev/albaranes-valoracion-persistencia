# application/services/unit_converter.py
from __future__ import annotations

import logging
from dataclasses import dataclass

from domain.models.unit_models import ConversionResult
from domain.ports.unit_registry_port import UnitRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConvertedQuantity:
    cantidad_convertida: float | None
    factor: float | None
    ambiguous: bool
    reasons: list[str]


class UnitConverter:
    """Fachada sobre el ``UnitRegistry`` para convertir cantidades."""

    def __init__(self, registry: UnitRegistry) -> None:
        self._registry = registry

    def convert(
        self,
        *,
        cantidad: float | None,
        unidad_albaran: str | None,
        unidad_contrato: str | None,
    ) -> ConvertedQuantity:
        if cantidad is None:
            return ConvertedQuantity(
                cantidad_convertida=None,
                factor=None,
                ambiguous=False,
                reasons=["no_quantity_in_albaran"],
            )

        # Si no tenemos unidad de contrato, devolvemos la cantidad tal
        # cual (factor 1). El guard del paso anterior ya marcará
        # review_required si hace falta.
        if unidad_contrato is None or not unidad_contrato.strip():
            return ConvertedQuantity(
                cantidad_convertida=float(cantidad),
                factor=1.0,
                ambiguous=False,
                reasons=["no_contract_unit_assumed_same"],
            )

        if unidad_albaran is None or not unidad_albaran.strip():
            return ConvertedQuantity(
                cantidad_convertida=float(cantidad),
                factor=1.0,
                ambiguous=False,
                reasons=["no_albaran_unit_assumed_same"],
            )

        result: ConversionResult = self._registry.convert(
            quantity=float(cantidad),
            from_unit=unidad_albaran,
            to_unit=unidad_contrato,
        )
        reasons: list[str] = []
        if result.reason:
            reasons.append(result.reason)
        if result.ambiguous:
            reasons.append("ambiguous_unit_conversion")
        if not result.category_match:
            reasons.append("unit_category_mismatch_in_conversion")
            return ConvertedQuantity(
                cantidad_convertida=None,
                factor=None,
                ambiguous=False,
                reasons=reasons,
            )
        return ConvertedQuantity(
            cantidad_convertida=result.converted_quantity,
            factor=result.factor,
            ambiguous=result.ambiguous,
            reasons=reasons,
        )
