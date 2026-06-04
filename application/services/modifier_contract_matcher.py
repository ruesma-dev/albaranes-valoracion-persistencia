# application/services/modifier_contract_matcher.py
"""Macheo determinista de líneas sintéticas (modificadores) contra la
tabla de líneas de contrato de Sigrid.

CONTEXTO (Bloque 1 — may 2026)
------------------------------
Hasta ahora las líneas sintéticas de hormigón (M1–M7: incremento por
año, consistencia, árido, aditivo/fibra, gestión de residuos, exceso de
tiempo, carga incompleta) tomaban su precio del PDF del contrato
(``precio_unitario_pdf_inferido``) y NUNCA casaban con una línea de la
tabla de Sigrid. Resultado: ``matched_contrato_line_id = NULL`` y, en el
front, ninguna caja de conciliación para esas líneas.

Pero para muchos proveedores (p.ej. hormigoneras) esos incrementos SÍ
existen como líneas propias del contrato en Sigrid, REPLICADAS POR CADA
PARTIDA igual que el producto base. Ejemplo real (contrato CTSU24/0476,
partida 03.03.07):

    HORMIGON PREPARADOS EN CENTRAL HA-25.            96.50   (base)
    INCREMENTO POR CONSISTENCIA FLUIDA EN HORMIGON    4.00
    INCREMENTO POR ÁRIDO 12 EN HORMIGÓN               4.00
    INCREMENTO POR SIN ADITIVO                        5.00
    INCREMENTO POR FIBRAS                             6.00
    INCREMENTO POR GESTION DE RESIDUOS EN HORMIGON    0.75
    INCREMENTO POR AÑO 2025 EN HORMIGON               3.00
    INCREMENTO POR AÑO 2026 EN HORMIGON              11.00
    HORAS POR EXCESO EN DESCARGA                     85.00

Lo correcto es que cada sintética CONCILIE con SU línea de incremento EN
LA MISMA PARTIDA QUE LA BASE (igual que ya ocurre con la línea base vía
``PartidaMatcher``). El re-apuntado por partida es responsabilidad de
svc6 —el LLM tiene prohibido elegir partida—.

IMPORTANTE — este servicio NO decide el precio. Solo localiza la línea
de Sigrid contra la que conciliar. El precio lo decide el builder con
prioridad al PDF (Fase 1B); el precio de la línea de Sigrid que este
matcher devuelve solo se usa como FALLBACK cuando el PDF no trae tarifa
para el modificador (p.ej. INCREMENTO POR AÑO 2026, ausente en el PDF
pero presente como línea en Sigrid).

DISEÑO
------
- Determinista, sin IA: la lista de incrementos es finita y sus
  descripciones son canónicas. El LLM solo decide QUÉ modificadores
  aplican (reglas M1–M7); aquí decidimos contra QUÉ línea de Sigrid
  casan y a qué precio.
- Match restringido a la partida de la base (``base_partida``). Si la
  base no tiene partida resuelta, no se intenta (sin match).
- Match por descripción NORMALIZADA (mayúsculas, sin acentos, espacios
  colapsados) tolerante a las erratas reales de Sigrid
  ("INFERIPOR", "ÁRIDO"/"ARIDO", "AÑO"/"ANO", "FIBRAS"/"FIBRA").
- Excluye variantes de MORTERO cuando la base es hormigón.
- Si hay duplicados (misma descripción y partida), coge el primero
  estable — mismo criterio que la base.
- Backward-compatible: si no hay línea de incremento en la tabla
  (contratos donde los recargos solo viven en el PDF), no casa y el
  builder cae al precio del PDF como antes.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Optional

from domain.models.valuation_envelope import (
    ContratoLineContextDto,
    LineValuationDto,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModifierMatchResult:
    """Resultado del macheo de una sintética contra la tabla."""

    matched_line: Optional[ContratoLineContextDto]
    reasons: list[str] = field(default_factory=list)
    # True cuando el match exige confirmación humana: bien porque se
    # localizó fuera de la partida de la base (fallback cross-partida),
    # bien porque es un incremento de tiempo cuya condición de
    # aplicación (p.ej. "descarga superior a 1 hora") NO puede verificar
    # el matcher. El builder lo propaga a review_required de la línea.
    requires_review: bool = False


def _normalize(text: str | None) -> str:
    """Normaliza para comparar: mayúsculas, sin acentos (Ñ→N),
    espacios colapsados.
    """
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_ = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", ascii_.upper()).strip()


class ModifierContractMatcher:
    """Casa una línea sintética (modificador) con su línea de incremento
    en la tabla de contrato, dentro de la partida de la base.
    """

    # Token que descarta candidatas de MORTERO cuando la base es hormigón
    # (evita casar el incremento de mortero con un albarán de hormigón).
    _MORTERO_TOKEN = "MORTERO"

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled

    def match(
        self,
        *,
        synthetic_line: LineValuationDto,
        base_partida: str | None,
        contrato_lines: list[ContratoLineContextDto],
    ) -> ModifierMatchResult:
        if not self._enabled:
            return ModifierMatchResult(
                matched_line=None,
                reasons=["modifier_table_match_disabled"],
            )

        predicate = self._build_predicate(synthetic_line)
        if predicate is None:
            return ModifierMatchResult(
                matched_line=None,
                reasons=["modifier_kind_not_recognized"],
            )

        rol = (synthetic_line.rol_linea or "").strip().lower()
        is_time = rol == "incremento_tiempo"

        # Universo de candidatas: todas menos las de MORTERO (evita casar
        # el incremento de mortero con un albarán de hormigón).
        non_mortero = [
            cl
            for cl in contrato_lines
            if self._MORTERO_TOKEN not in _normalize(cl.descripcion)
        ]

        # ------------------------------------------------------------- #
        # Paso 1 — match ESTRICTO en la partida de la base (sin cambios
        # respecto al comportamiento previo: si la base resolvió partida
        # y esa partida tiene líneas, casamos solo dentro de ella).
        # ------------------------------------------------------------- #
        if base_partida is not None:
            part_norm = _normalize(base_partida)
            in_partida = [
                cl for cl in non_mortero
                if _normalize(cl.codigo_partida) == part_norm
            ]
            for cl in in_partida:
                if predicate(_normalize(cl.descripcion)):
                    reasons = ["modifier_matched_in_contract_partida"]
                    if is_time:
                        reasons.append(
                            "modifier_time_condition_needs_manual_check"
                        )
                    logger.info(
                        "[modifier-matcher] sintética rol=%s desc=%r casó "
                        "EN PARTIDA con contrato_line_id=%s (%r) partida=%s "
                        "precio=%s",
                        synthetic_line.rol_linea,
                        synthetic_line.descripcion_linea,
                        cl.contrato_line_id, cl.descripcion,
                        cl.codigo_partida, cl.precio_unitario,
                    )
                    return ModifierMatchResult(
                        matched_line=cl,
                        reasons=reasons,
                        requires_review=is_time,
                    )
            # Si la partida de la base SÍ tenía líneas pero ninguna casó,
            # NO hacemos fallback: la partida estaba poblada y el
            # incremento simplemente no está en ella (mismo criterio
            # conservador de siempre).
            if in_partida:
                return ModifierMatchResult(
                    matched_line=None,
                    reasons=["modifier_not_in_contract_partida"],
                )

        # ------------------------------------------------------------- #
        # Paso 2 — FALLBACK cross-partida. Solo se llega aquí si la base
        # no resolvió partida, o si su partida no tiene NINGUNA línea de
        # contrato (estructura de partidas no alineada con el albarán,
        # típico de proveedores que replican los incrementos en otras
        # partidas con precio uniforme — p.ej. SPECIAL CONCRETE).
        #
        # Política (acordada): casamos por tipo en TODO el contrato y
        # usamos el precio SOLO si es uniforme entre las coincidencias.
        # Si varía, NO casamos y dejamos a revisión. Todo match por esta
        # vía exige confirmación (requires_review=True).
        # ------------------------------------------------------------- #
        cross = [
            cl for cl in non_mortero
            if predicate(_normalize(cl.descripcion))
        ]
        if not cross:
            return ModifierMatchResult(
                matched_line=None,
                reasons=["modifier_no_match_cross_partida"],
            )

        precios = {
            round(float(cl.precio_unitario), 4)
            for cl in cross
            if cl.precio_unitario is not None
        }
        if len(precios) > 1:
            return ModifierMatchResult(
                matched_line=None,
                reasons=[
                    "modifier_cross_partida_price_not_uniform:"
                    f"{sorted(precios)}"
                ],
            )

        chosen = cross[0]
        reasons = ["modifier_matched_cross_partida_uniform_price"]
        if is_time:
            reasons.append("modifier_time_condition_needs_manual_check")
        logger.info(
            "[modifier-matcher] sintética rol=%s desc=%r casó CROSS-PARTIDA "
            "con contrato_line_id=%s (%r) partida=%s precio=%s "
            "(precio uniforme=%s; a revisión)",
            synthetic_line.rol_linea,
            synthetic_line.descripcion_linea,
            chosen.contrato_line_id, chosen.descripcion,
            chosen.codigo_partida, chosen.precio_unitario,
            sorted(precios) if precios else "n/a",
        )
        return ModifierMatchResult(
            matched_line=chosen,
            reasons=reasons,
            requires_review=True,
        )

    # ----------------------------------------------------------------- #
    # Predicados por tipo de modificador.
    # ----------------------------------------------------------------- #
    def _build_predicate(
        self,
        line: LineValuationDto,
    ) -> Optional[Callable[[str], bool]]:
        """Devuelve un predicado sobre la descripción NORMALIZADA de la
        línea de contrato, según el ``rol_linea`` del modificador.

        Devuelve None si el rol no se reconoce (no se intenta casar).
        """
        rol = (line.rol_linea or "").strip().lower()
        desc = _normalize(line.descripcion_linea)

        if rol == "incremento_consistencia":
            # Distingue líquida de fluida cuando la sintética lo indica;
            # por defecto basta con CONSISTENCIA (el contrato de este
            # proveedor solo tarifa FLUIDA).
            if "LIQUIDA" in desc:
                return lambda d: "CONSISTENCIA" in d and "LIQUIDA" in d
            return lambda d: "CONSISTENCIA" in d

        if rol == "incremento_arido":
            return lambda d: "ARIDO" in d

        if rol == "incremento_residuos":
            return lambda d: "RESIDUOS" in d

        if rol == "incremento_tiempo":
            # Variantes reales del mismo concepto:
            #   "HORAS POR EXCESO EN DESCARGA"          (Suministros)
            #   "EXCESO DE TIEMPO DE DESCARGA"
            #   "INCREMENTO DESCARGA SUPERIOR A 1 HORA" (Special Concrete)
            # Núcleo común: habla de DESCARGA y de un EXCESO/SUPERACIÓN.
            return lambda d: "DESCARGA" in d and (
                "EXCESO" in d or "SUPERIOR" in d
            )

        if rol == "incremento_carga_incompleta":
            # Tolera la errata real "INFERIPOR" (basta el núcleo).
            return lambda d: "CARGA INCOMPLETA" in d

        if rol == "incremento_aditivo":
            # El mismo rol cubre 'SIN ADITIVO' y fibras: desambiguamos
            # por la descripción de la propia sintética.
            if "SIN ADITIVO" in desc:
                return lambda d: "SIN ADITIVO" in d
            if "FIBRA" in desc:
                return lambda d: "FIBRA" in d
            return lambda d: "ADITIVO" in d

        if rol == "incremento_year":
            year = self._extract_year(desc) or self._extract_year(
                _normalize(line.modifier_reason)
            )
            if year is None:
                return None
            # El año aparece como "AÑO 2025" (Suministros) o como
            # "INCREMENTO PRECIO 2023" (Special Concrete). Exigimos el año
            # + que sea claramente un incremento de precio/año, para no
            # casar una línea base que lleve el número por casualidad.
            return lambda d: (year in d) and ("ANO" in d or "PRECIO" in d)

        # 'incremento_otro' u otros roles no canónicos: no arriesgamos
        # un match incorrecto. El builder caerá al precio del PDF.
        return None

    @staticmethod
    def _extract_year(text: str) -> Optional[str]:
        match = re.search(r"\b(20\d{2})\b", text or "")
        return match.group(1) if match else None
