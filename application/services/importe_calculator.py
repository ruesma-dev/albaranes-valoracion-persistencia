# application/services/importe_calculator.py
from __future__ import annotations

import logging
from dataclasses import dataclass

from domain.models.valuation_records import ImporteSource

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImporteResult:
    importe_calculado: float | None
    importe_source: ImporteSource
    reasons: list[str]


class ImporteCalculator:
    """Calcula el importe final de la línea.

    Estrategia:
      1. Cantidad efectiva = ``cantidad_convertida`` si existe; si no,
         ``cantidad_albaran`` como fallback. Así cubrimos los casos en
         que la unidad es 'unknown' y no se pudo convertir, pero sí
         tenemos la cantidad nativa del albarán y un precio unitario.
      2. Si hay cantidad efectiva y precio unitario final, calculamos
         importe = cantidad_efectiva * precio_unitario_final.
      3. Si además hay importe declarado en el albarán:
           - coincide con el calculado dentro de tolerancia →
             se usa el DECLARADO (más fiel al documento fuente),
             ``importe_source='declared_albaran'``.
           - no coincide → se usa el calculado y se marca mismatch
             en reasons.
      4. Si no hay cantidad efectiva y sí declarado → declarado.
      5. Si no hay nada → None.

    Cuando se usa el fallback a cantidad_albaran (por falta de
    cantidad_convertida), se añade un reason para dejar rastro. Lo
    consume el flag de review_required si procede.
    """

    def __init__(self, *, tolerance_pct: float) -> None:
        self._tolerance_pct = float(tolerance_pct)

    def compute(
        self,
        *,
        cantidad_convertida: float | None,
        cantidad_albaran: float | None = None,
        precio_unitario_final: float | None,
        importe_albaran_declarado: float | None,
    ) -> ImporteResult:
        # Cantidad efectiva: primero la convertida; si no, la del
        # albarán (ojo: puede ser en unidades distintas al precio; el
        # review ya marcará ``ia_unit_category_mismatch`` aparte).
        used_albaran_fallback = False
        cantidad_efectiva: float | None = cantidad_convertida
        if cantidad_efectiva is None and cantidad_albaran is not None:
            cantidad_efectiva = cantidad_albaran
            used_albaran_fallback = True

        reasons: list[str] = []

        if cantidad_efectiva is None or precio_unitario_final is None:
            if importe_albaran_declarado is not None:
                return ImporteResult(
                    importe_calculado=float(importe_albaran_declarado),
                    importe_source="declared_albaran",
                    reasons=["no_calc_possible_using_declared"],
                )
            return ImporteResult(
                importe_calculado=None,
                importe_source="none",
                reasons=["no_calc_possible_and_no_declared"],
            )

        calc = float(cantidad_efectiva) * float(precio_unitario_final)
        calc = round(calc, 2)

        if used_albaran_fallback:
            reasons.append("importe_using_albaran_quantity_fallback")

        if importe_albaran_declarado is None:
            return ImporteResult(
                importe_calculado=calc,
                importe_source="calculated",
                reasons=reasons,
            )

        if self._close_enough(calc, float(importe_albaran_declarado)):
            # Pero ojo: si el declarado es 0 y el calculado no, eso no
            # es "match" aceptable — _close_enough con 0 y 0 da True,
            # pero con 0 y N>0 no. El propio método ya lo gestiona.
            return ImporteResult(
                importe_calculado=float(importe_albaran_declarado),
                importe_source="declared_albaran",
                reasons=reasons,
            )

        reasons.append(
            f"declared_vs_calculated_mismatch:"
            f"{importe_albaran_declarado}!={calc}"
        )
        logger.info(
            "[importe] mismatch declarado=%s calculado=%s; se usa calculado.",
            importe_albaran_declarado, calc,
        )
        return ImporteResult(
            importe_calculado=calc,
            importe_source="calculated",
            reasons=reasons,
        )

    def _close_enough(self, a: float, b: float) -> bool:
        if a == 0 and b == 0:
            return True
        denom = max(abs(a), abs(b), 1e-9)
        diff_pct = abs(a - b) / denom * 100.0
        return diff_pct <= self._tolerance_pct
