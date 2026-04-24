# application/services/price_reconciler.py
from __future__ import annotations

import logging
from dataclasses import dataclass

from domain.models.valuation_records import (
    PrecioUnitarioAgreement,
    PrecioUnitarioSource,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceReconciliation:
    final_price: float | None
    source: PrecioUnitarioSource
    agreement: PrecioUnitarioAgreement
    reasons: list[str]


class PriceReconciler:
    """Reconcilia los precios obtenidos por la IA (1a + 1b) con el
    precio que pueda venir en el propio albarán.

    Orden de prioridad cuando NO hay coincidencia 1a↔1b:
      1. contract_line_match (fase 1a)  ← regla del cliente
      2. pdf_inference (fase 1b)
      3. albaran_declared (precio unitario que venía en el albarán)
      4. albaran_calculated (importe_albaran / cantidad_albaran)
    """

    def __init__(self, *, tolerance_pct: float) -> None:
        self._tolerance_pct = float(tolerance_pct)

    def reconcile(
        self,
        *,
        precio_1a: float | None,
        precio_1b: float | None,
        precio_albaran_declarado: float | None,
        cantidad_albaran: float | None,
        importe_albaran: float | None,
        line_already_valued: bool,
    ) -> PriceReconciliation:
        reasons: list[str] = []

        albaran_calc: float | None = None
        if (
            cantidad_albaran is not None
            and importe_albaran is not None
            and cantidad_albaran != 0
        ):
            albaran_calc = float(importe_albaran) / float(cantidad_albaran)

        # Caso especial: la línea ya se valoró antes y solo necesitamos
        # buscar línea de contrato. Regla del cliente: usar el valor
        # que viene en el albarán (declarado o calculado).
        if line_already_valued:
            if precio_albaran_declarado is not None:
                return PriceReconciliation(
                    final_price=float(precio_albaran_declarado),
                    source="albaran_declared",
                    agreement="neither",
                    reasons=["line_already_valued_using_albaran_declared"],
                )
            if albaran_calc is not None:
                return PriceReconciliation(
                    final_price=albaran_calc,
                    source="albaran_calculated",
                    agreement="neither",
                    reasons=[
                        "line_already_valued_using_albaran_calculated",
                    ],
                )

        # Camino normal (línea sin valorar previamente).
        if precio_1a is not None and precio_1b is not None:
            if self._match(precio_1a, precio_1b):
                # Promediamos para atenuar redondeos.
                avg = (float(precio_1a) + float(precio_1b)) / 2.0
                return PriceReconciliation(
                    final_price=avg,
                    source="both_agreed",
                    agreement="match",
                    reasons=[],
                )
            reasons.append(
                f"price_1a_vs_1b_mismatch:{precio_1a}!={precio_1b}"
            )
            logger.info(
                "[price-reconciler] mismatch 1a=%s 1b=%s; prevalece 1a.",
                precio_1a, precio_1b,
            )
            return PriceReconciliation(
                final_price=float(precio_1a),
                source="contract_line_match",
                agreement="mismatch",
                reasons=reasons,
            )

        if precio_1a is not None:
            return PriceReconciliation(
                final_price=float(precio_1a),
                source="contract_line_match",
                agreement="only_1a",
                reasons=["only_1a_available"],
            )

        if precio_1b is not None:
            return PriceReconciliation(
                final_price=float(precio_1b),
                source="pdf_inference",
                agreement="only_1b",
                reasons=["only_1b_available"],
            )

        if precio_albaran_declarado is not None:
            return PriceReconciliation(
                final_price=float(precio_albaran_declarado),
                source="albaran_declared",
                agreement="neither",
                reasons=["no_contract_price_using_albaran_declared"],
            )

        if albaran_calc is not None:
            return PriceReconciliation(
                final_price=albaran_calc,
                source="albaran_calculated",
                agreement="neither",
                reasons=["no_contract_price_using_albaran_calculated"],
            )

        return PriceReconciliation(
            final_price=None,
            source="none",
            agreement="neither",
            reasons=["no_price_available"],
        )

    def _match(self, a: float, b: float) -> bool:
        if a == 0 and b == 0:
            return True
        denom = max(abs(float(a)), abs(float(b)), 1e-9)
        diff_pct = abs(float(a) - float(b)) / denom * 100.0
        return diff_pct <= self._tolerance_pct
