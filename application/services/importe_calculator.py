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
    descuento_aplicado: float | None  # 0-100, None si no se aplicó


class ImporteCalculator:
    """Calcula el importe final de la línea.

    Estrategia (sin descuento):
      1. Cantidad efectiva = ``cantidad_convertida`` si existe; si no,
         ``cantidad_albaran`` como fallback.
      2. Si hay cantidad efectiva y precio unitario final, calculamos
         importe = cantidad_efectiva * precio_unitario_final.
      3. Si además hay importe declarado en el albarán:
           - coincide con el calculado dentro de tolerancia →
             se usa el DECLARADO (más fiel al documento fuente).
           - no coincide → se usa el calculado y se marca mismatch.
      4. Si no hay cantidad efectiva y sí declarado → declarado.
      5. Si no hay nada → None.

    -------------------------------------------------------------------
    Tanda descuento — abr 2026
    -------------------------------------------------------------------
    Cuando se pasa ``descuento_pct`` (0-100), la fórmula del paso 2
    aplica el descuento al precio del contrato:

        importe = cantidad_efectiva
                * precio_unitario_final
                * (1 - descuento_pct / 100)

    Decisión de negocio (Construcciones Ruesma): el descuento del
    albarán se aplica al precio del contrato para obtener el importe
    valorado. Las líneas sintéticas (M1-M7) heredan el descuento de
    su línea base padre — esa herencia la gestiona el ValuationBuilder
    pasando el descuento ya resuelto del padre a este calculator.

    Compatibilidad retroactiva: ``descuento_pct`` es opcional con
    default None. Si es None, 0 o fuera de rango [0,100], el cálculo
    no aplica descuento y se comporta exactamente como antes.
    -------------------------------------------------------------------
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
        descuento_pct: float | None = None,
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

        # Saneamiento del descuento: solo lo aplicamos en rango (0,100].
        descuento_aplicable = self._sanitize_descuento(
            descuento_pct, reasons,
        )

        if cantidad_efectiva is None or precio_unitario_final is None:
            if importe_albaran_declarado is not None:
                return ImporteResult(
                    importe_calculado=float(importe_albaran_declarado),
                    importe_source="declared_albaran",
                    reasons=["no_calc_possible_using_declared"],
                    descuento_aplicado=None,
                )
            return ImporteResult(
                importe_calculado=None,
                importe_source="none",
                reasons=["no_calc_possible_and_no_declared"],
                descuento_aplicado=None,
            )

        # Cálculo base
        calc_bruto = float(cantidad_efectiva) * float(precio_unitario_final)

        # Aplicación de descuento (si procede)
        if descuento_aplicable is not None and descuento_aplicable > 0.0:
            calc = calc_bruto * (1.0 - descuento_aplicable / 100.0)
            reasons.append(
                f"descuento_aplicado:{descuento_aplicable}%"
            )
        else:
            calc = calc_bruto

        calc = round(calc, 2)

        if used_albaran_fallback:
            reasons.append("importe_using_albaran_quantity_fallback")

        if importe_albaran_declarado is None:
            return ImporteResult(
                importe_calculado=calc,
                importe_source="calculated",
                reasons=reasons,
                descuento_aplicado=descuento_aplicable,
            )

        if self._close_enough(calc, float(importe_albaran_declarado)):
            # Pero ojo: si el declarado es 0 y el calculado no, eso no
            # es "match" aceptable — _close_enough con 0 y 0 da True,
            # pero con 0 y N>0 no. El propio método ya lo gestiona.
            return ImporteResult(
                importe_calculado=float(importe_albaran_declarado),
                importe_source="declared_albaran",
                reasons=reasons,
                descuento_aplicado=descuento_aplicable,
            )

        reasons.append(
            f"declared_vs_calculated_mismatch:"
            f"{importe_albaran_declarado}!={calc}"
        )
        logger.info(
            "[importe] mismatch declarado=%s calculado=%s "
            "(descuento=%s%%); se usa calculado.",
            importe_albaran_declarado, calc, descuento_aplicable,
        )
        return ImporteResult(
            importe_calculado=calc,
            importe_source="calculated",
            reasons=reasons,
            descuento_aplicado=descuento_aplicable,
        )

    @staticmethod
    def _sanitize_descuento(
        descuento_pct: float | None,
        reasons: list[str],
    ) -> float | None:
        """Devuelve el descuento si es aplicable, None si no.

        - None → None (sin descuento).
        - 0    → 0 (sin descuento, pero registramos que vino).
        - <0 o >100 → None y reason de aviso.
        - dentro de rango → el valor.
        """
        if descuento_pct is None:
            return None
        d = float(descuento_pct)
        if d == 0.0:
            return 0.0
        if d < 0.0 or d > 100.0:
            reasons.append(
                f"descuento_fuera_de_rango_ignorado:{d}"
            )
            logger.warning(
                "[importe] descuento_pct fuera de rango [0,100]: %s. "
                "Se ignora.",
                d,
            )
            return None
        return d

    def _close_enough(self, a: float, b: float) -> bool:
        if a == 0 and b == 0:
            return True
        denom = max(abs(a), abs(b), 1e-9)
        diff_pct = abs(a - b) / denom * 100.0
        return diff_pct <= self._tolerance_pct
