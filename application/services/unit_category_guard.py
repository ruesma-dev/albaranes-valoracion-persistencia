# application/services/unit_category_guard.py
from __future__ import annotations

import logging

from domain.models.valuation_envelope import LineValuationDto
from domain.ports.unit_registry_port import UnitRegistry

logger = logging.getLogger(__name__)


class UnitCategoryGuard:
    """Valida que la categoría de unidad declarada por la IA es real.

    La IA devuelve ``unidad_categoria_albaran`` y ``unidad_category_match``.
    Nosotros re-clasificamos la unidad real del albarán con el registro
    determinista para:
      - Confirmar que la IA no inventó una categoría.
      - Clasificar la unidad del contrato y chequear que son de la
        misma categoría (sin esto nos fiamos solo de la IA).
      - Detectar casos de "unknown" para forzar ``review_required``.
    """

    def __init__(self, unit_registry: UnitRegistry) -> None:
        self._registry = unit_registry

    def resolve(
        self,
        *,
        line: LineValuationDto,
        unidad_albaran: str | None,
        unidad_contrato: str | None,
    ) -> tuple[str, bool, list[str]]:
        """Devuelve (categoria_final, category_match, reasons)."""
        reasons: list[str] = []
        info_albaran = self._registry.classify(unidad_albaran)
        info_contrato = self._registry.classify(unidad_contrato)

        categoria = info_albaran.category

        # Caso 1: la IA ya dijo que no hay match de categoría → respeta.
        if not line.unidad_category_match:
            return categoria, False, ["ia_unit_category_mismatch"]

        # Caso 2: no hay unidad de contrato y no hay línea contrato casada.
        # La IA puede haber hecho match solo por descripción + precio PDF.
        if info_contrato.category == "unknown" and unidad_contrato is None:
            if info_albaran.category == "unknown":
                reasons.append("both_units_unknown")
                return "unknown", True, reasons
            # Pasamos: la categoría la define el albarán, sin contraste.
            reasons.append("contract_unit_unknown")
            return categoria, True, reasons

        # Caso 3: categorías distintas de verdad.
        if info_albaran.category != info_contrato.category:
            if (
                info_albaran.category == "unknown"
                or info_contrato.category == "unknown"
            ):
                reasons.append("unit_category_partially_unknown")
                return categoria, False, reasons
            reasons.append(
                "unit_category_hard_mismatch:"
                f"{info_albaran.category}!={info_contrato.category}"
            )
            logger.warning(
                "Desacuerdo categoría unidad. albaran=%s (%s) contrato=%s (%s). "
                "La IA decía category_match=true; se fuerza a false.",
                unidad_albaran, info_albaran.category,
                unidad_contrato, info_contrato.category,
            )
            return categoria, False, reasons

        return categoria, True, reasons
