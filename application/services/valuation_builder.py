# application/services/valuation_builder.py
from __future__ import annotations

import logging
from typing import Dict

from application.services.importe_calculator import ImporteCalculator
from application.services.partida_matcher import PartidaMatcher, PartidaMatchResult
from application.services.price_reconciler import PriceReconciler
from application.services.unit_category_guard import UnitCategoryGuard
from application.services.unit_converter import UnitConverter
from domain.models.valuation_envelope import (
    AlbaranLineContextDto,
    ContratoLineContextDto,
    LineValuationDto,
    ValuationEnvelope,
)
from domain.models.valuation_records import (
    LineValuationRecord,
    MatchMethod,
    ValuationHeaderRecord,
)

logger = logging.getLogger(__name__)


class ValuationBuilder:
    """Transforma el envelope del servicio 5 en los records a persistir.

    Este es el punto donde se aplica TODA la lógica determinista en
    orden:
      1. UnitCategoryGuard — confirma categoría de unidad.
      2. PriceReconciler — decide precio_unitario_final.
      3. PartidaMatcher — decide matched_contrato_line_id o crea derived.
         Sub-tanda 2C: si la línea es complementaria, en vez de hacer
         match contra el contrato, hereda la partida de su línea base
         (que se resolvió en la primera pasada).
      4. UnitConverter — calcula cantidad convertida.
      5. ImporteCalculator — calcula importe final.

    ORDEN DE PROCESADO (sub-tanda 2C):
      Primero resolvemos TODAS las líneas base (rol_linea == 'base' o
      sin contexto_linea). Después resolvemos las complementarias, que
      pueden consultar el ``codigo_partida_final`` ya fijado de su base.
      Esto es necesario porque la herencia de partida requiere que la
      base esté resuelta antes.
    """

    def __init__(
        self,
        *,
        unit_category_guard: UnitCategoryGuard,
        price_reconciler: PriceReconciler,
        partida_matcher: PartidaMatcher,
        unit_converter: UnitConverter,
        importe_calculator: ImporteCalculator,
    ) -> None:
        self._guard = unit_category_guard
        self._reconciler = price_reconciler
        self._partida_matcher = partida_matcher
        self._converter = unit_converter
        self._importe_calc = importe_calculator

    def build(
        self,
        *,
        envelope: ValuationEnvelope,
        existing_document_already_valued: bool,
    ) -> tuple[ValuationHeaderRecord, list[LineValuationRecord]]:
        albaran_by_id: Dict[int, AlbaranLineContextDto] = {
            line.merge_line_id: line for line in envelope.context.lineas_albaran
        }
        contrato_by_id: Dict[int, ContratoLineContextDto] = {
            line.contrato_line_id: line
            for line in envelope.context.lineas_contrato
        }
        contrato_lines = envelope.context.lineas_contrato

        # -----------------------------------------------------------------
        # Mapa line_index (del OCR) → merge_line_id. Usado para traducir
        # ``contexto_linea.ref_linea_base`` (que es un line_index) al
        # merge_line_id real con el que trabaja el resto del sistema.
        # -----------------------------------------------------------------
        merge_id_by_line_index: Dict[int, int] = {
            line.line_index: line.merge_line_id
            for line in envelope.context.lineas_albaran
        }

        # -----------------------------------------------------------------
        # Separamos líneas base de complementarias para procesarlas en
        # dos pasadas. Criterio:
        #   - Sin contexto_linea → base (producto simple).
        #   - Con contexto_linea.rol_linea == 'base' → base.
        #   - Con contexto_linea.rol_linea == otro → complementaria.
        # -----------------------------------------------------------------
        base_lines: list[LineValuationDto] = []
        complementarias: list[LineValuationDto] = []
        for line in envelope.data.lineas:
            ctx = self._get_albaran_ctx(line, albaran_by_id)
            if ctx is None or ctx.rol_linea in (None, "base"):
                base_lines.append(line)
            else:
                complementarias.append(line)

        # -----------------------------------------------------------------
        # Pasada 1: líneas base. Matching normal.
        # -----------------------------------------------------------------
        records_by_merge_id: Dict[int, LineValuationRecord] = {}
        for line in base_lines:
            record = self._build_line(
                line=line,
                albaran_by_id=albaran_by_id,
                contrato_by_id=contrato_by_id,
                contrato_lines=contrato_lines,
                line_already_valued=existing_document_already_valued,
                partida_override=None,
                ref_linea_base_merge_id=None,
                merge_id_by_line_index=merge_id_by_line_index,
            )
            records_by_merge_id[record.merge_line_id] = record

        # -----------------------------------------------------------------
        # Pasada 2: líneas complementarias. Heredan la partida de la base.
        # -----------------------------------------------------------------
        for line in complementarias:
            ctx = self._get_albaran_ctx(line, albaran_by_id)
            # ctx nunca es None aquí (las complementarias por definición
            # tienen contexto_linea). Pero por defensivo:
            if ctx is None:
                # Raro: lo tratamos como línea base.
                record = self._build_line(
                    line=line,
                    albaran_by_id=albaran_by_id,
                    contrato_by_id=contrato_by_id,
                    contrato_lines=contrato_lines,
                    line_already_valued=existing_document_already_valued,
                    partida_override=None,
                    ref_linea_base_merge_id=None,
                    merge_id_by_line_index=merge_id_by_line_index,
                )
                records_by_merge_id[record.merge_line_id] = record
                continue

            # Resolvemos el merge_line_id de la base traduciendo
            # ref_linea_base (line_index) al merge_id.
            ref_base_merge_id: int | None = None
            partida_base: str | None = None
            if ctx.ref_linea_base is not None:
                ref_base_merge_id = merge_id_by_line_index.get(
                    ctx.ref_linea_base
                )
                if ref_base_merge_id is not None:
                    base_record = records_by_merge_id.get(ref_base_merge_id)
                    if base_record is not None:
                        partida_base = base_record.codigo_partida_final
                    else:
                        logger.warning(
                            "[builder] línea complementaria merge_id=%s "
                            "apunta a ref_linea_base=%s (merge_id=%s) "
                            "que aún no está resuelta; partida quedará null.",
                            line.merge_line_id,
                            ctx.ref_linea_base,
                            ref_base_merge_id,
                        )
                else:
                    logger.warning(
                        "[builder] línea complementaria merge_id=%s con "
                        "ref_linea_base=%s no encontrado en "
                        "lineas_albaran; no se puede heredar partida.",
                        line.merge_line_id,
                        ctx.ref_linea_base,
                    )

            record = self._build_line(
                line=line,
                albaran_by_id=albaran_by_id,
                contrato_by_id=contrato_by_id,
                contrato_lines=contrato_lines,
                line_already_valued=existing_document_already_valued,
                partida_override=partida_base,
                ref_linea_base_merge_id=ref_base_merge_id,
                merge_id_by_line_index=merge_id_by_line_index,
            )
            records_by_merge_id[record.merge_line_id] = record

        # Preservamos el orden original de envelope.data.lineas para
        # que el resultado final no dependa de si es base o complementaria.
        records = [
            records_by_merge_id[line.merge_line_id]
            for line in envelope.data.lineas
            if line.merge_line_id in records_by_merge_id
        ]

        header = self._build_header(
            envelope=envelope,
            records=records,
        )
        return header, records

    @staticmethod
    def _get_albaran_ctx(
        line: LineValuationDto,
        albaran_by_id: Dict[int, AlbaranLineContextDto],
    ):
        """Devuelve el ContextoLinea de la línea, o None."""
        albaran_line = albaran_by_id.get(line.merge_line_id)
        if albaran_line is None:
            return None
        return albaran_line.contexto_linea

    def _build_line(
        self,
        *,
        line: LineValuationDto,
        albaran_by_id: Dict[int, AlbaranLineContextDto],
        contrato_by_id: Dict[int, ContratoLineContextDto],
        contrato_lines: list[ContratoLineContextDto],
        line_already_valued: bool,
        partida_override: str | None,
        ref_linea_base_merge_id: int | None,
        merge_id_by_line_index: Dict[int, int],
    ) -> LineValuationRecord:
        albaran_line = albaran_by_id.get(line.merge_line_id)
        contrato_line = (
            contrato_by_id.get(line.matched_contrato_line_id)
            if line.matched_contrato_line_id is not None
            else None
        )

        unidad_albaran = albaran_line.unidad_medida if albaran_line else None
        unidad_contrato = (
            contrato_line.unidad_medida if contrato_line is not None else None
        )

        ctx = albaran_line.contexto_linea if albaran_line is not None else None
        rol_linea = ctx.rol_linea if ctx is not None else None

        # ---- 1. Unit guard ----
        categoria, category_match, guard_reasons = self._guard.resolve(
            line=line,
            unidad_albaran=unidad_albaran,
            unidad_contrato=unidad_contrato,
        )

        # ---- 2. Price reconciliation ----
        reconciliation = self._reconciler.reconcile(
            precio_1a=line.precio_unitario_contrato_db,
            precio_1b=line.precio_unitario_pdf_inferido,
            precio_albaran_declarado=(
                albaran_line.precio_unitario_albaran if albaran_line else None
            ),
            cantidad_albaran=(
                albaran_line.cantidad if albaran_line else None
            ),
            importe_albaran=(
                albaran_line.importe_albaran if albaran_line else None
            ),
            line_already_valued=line_already_valued,
        )

        # ---- 3. Partida matching ----
        # Si es complementaria, hereda la partida de su base (regla
        # nueva sub-tanda 2C). Si es base, matching normal.
        if partida_override is not None or ref_linea_base_merge_id is not None:
            partida_result: PartidaMatchResult = (
                self._partida_matcher.resolve_partida_for_complementaria(
                    codigo_partida_base=partida_override,
                )
            )
        else:
            partida_result = self._partida_matcher.match(
                line=line,
                codigo_partida_albaran=(
                    albaran_line.codigo_partida_albaran if albaran_line else None
                ),
                precio_unitario_final=reconciliation.final_price,
                unidad_albaran=unidad_albaran,
                contrato_lines=contrato_lines,
                albaran_descripcion=(
                    albaran_line.descripcion if albaran_line else None
                ),
                albaran_codigo_producto=(
                    albaran_line.codigo if albaran_line else None
                ),
            )

        # Si el partida matcher encontró una línea existente distinta a
        # la que sugirió la IA, actualizamos matched_contrato_line_id.
        effective_matched_id = partida_result.matched_contrato_line_id
        if effective_matched_id is None and partida_result.derived_line is None:
            # Sin partida ni matched: nos quedamos con lo que diga la IA
            # (por si hay línea casada en el contrato pero no por partida).
            effective_matched_id = line.matched_contrato_line_id

        # ---- 4. Unit conversion ----
        if category_match and partida_result.derived_line is not None:
            unidad_contrato_para_conversion = unidad_albaran
        else:
            unidad_contrato_para_conversion = unidad_contrato

        if category_match:
            converted = self._converter.convert(
                cantidad=albaran_line.cantidad if albaran_line else None,
                unidad_albaran=unidad_albaran,
                unidad_contrato=unidad_contrato_para_conversion,
            )
        else:
            converted = self._converter.convert(
                cantidad=None,
                unidad_albaran=unidad_albaran,
                unidad_contrato=unidad_contrato_para_conversion,
            )

        # ---- 5. Importe calculation ----
        importe_result = self._importe_calc.compute(
            cantidad_convertida=converted.cantidad_convertida,
            cantidad_albaran=(
                albaran_line.cantidad if albaran_line else None
            ),
            precio_unitario_final=reconciliation.final_price,
            importe_albaran_declarado=(
                albaran_line.importe_albaran if albaran_line else None
            ),
        )

        # ---- Review reasons agregadas ----
        reasons: list[str] = []
        reasons.extend(guard_reasons)
        reasons.extend(reconciliation.reasons)
        reasons.extend(partida_result.reasons)
        reasons.extend(converted.reasons)
        reasons.extend(importe_result.reasons)
        if line.match_method == "no_match":
            reasons.append("ia_no_match")

        # -----------------------------------------------------------------
        # Derivación del flag tarifa_pdf_encontrada (sub-tanda 2C):
        #   - None si la línea no tiene contexto_linea (no aplica).
        #   - True si tiene contexto Y precio_unitario_pdf_inferido != null
        #     (el LLM encontró tarifa en el PDF, regla Forma A o B).
        #   - False si tiene contexto y pdf_inferido == null (Forma C:
        #     modificador no tarifado en contrato).
        #
        # Si no se encontró tarifa, añadimos una razón de review
        # específica para que el revisor la destaque.
        # -----------------------------------------------------------------
        if ctx is None:
            tarifa_pdf_encontrada: bool | None = None
        else:
            tarifa_pdf_encontrada = line.precio_unitario_pdf_inferido is not None
            if not tarifa_pdf_encontrada:
                # Solo añadimos la razón si además reconciliación no
                # encontró la tarifa por 1a (match de producto en BBDD).
                if reconciliation.source not in (
                    "contract_line_match", "both_agreed",
                ):
                    reasons.append("modifier_not_in_contract")

        review_required = (
            not category_match
            or reconciliation.agreement == "mismatch"
            or reconciliation.source == "none"
            or converted.ambiguous
            or importe_result.importe_source == "none"
            or line.match_method == "no_match"
            or line.match_confidence_pct < 60.0
            or (ctx is not None and tarifa_pdf_encontrada is False
                and reconciliation.source not in (
                    "contract_line_match", "both_agreed",
                ))
        )

        return LineValuationRecord(
            merge_line_id=line.merge_line_id,
            matched_contrato_line_id=effective_matched_id,
            derived_contrato_line_record=partida_result.derived_line,
            precio_unitario_contrato_db=line.precio_unitario_contrato_db,
            precio_unitario_pdf_inferido=line.precio_unitario_pdf_inferido,
            precio_unitario_final=reconciliation.final_price,
            precio_unitario_source=reconciliation.source,
            precio_unitario_agreement=reconciliation.agreement,
            unidad_albaran=unidad_albaran,
            unidad_contrato=unidad_contrato,
            unidad_categoria=categoria,
            unidad_category_match=category_match,
            cantidad_albaran=(
                albaran_line.cantidad if albaran_line else None
            ),
            cantidad_convertida=converted.cantidad_convertida,
            factor_conversion=converted.factor,
            importe_calculado=importe_result.importe_calculado,
            importe_albaran_declarado=(
                albaran_line.importe_albaran if albaran_line else None
            ),
            importe_source=importe_result.importe_source,
            codigo_partida_albaran=(
                albaran_line.codigo_partida_albaran if albaran_line else None
            ),
            codigo_partida_final=partida_result.codigo_partida_final,
            partida_action=partida_result.partida_action,
            match_confidence_pct=float(line.match_confidence_pct),
            match_method=line.match_method,  # type: ignore[arg-type]
            review_required=review_required,
            review_reasons=reasons,
            ia_reasoning=line.razon_corta,
            # ---- campos nuevos sub-tanda 2C ----
            rol_linea=rol_linea,
            ref_linea_base_merge_id=ref_linea_base_merge_id,
            tarifa_pdf_encontrada=tarifa_pdf_encontrada,
            modifiers_applied=None,  # reservado para futuro
        )

    @staticmethod
    def _build_header(
        *,
        envelope: ValuationEnvelope,
        records: list[LineValuationRecord],
    ) -> ValuationHeaderRecord:
        total_valorado = sum(
            float(r.importe_calculado or 0.0) for r in records
        )
        match_counts = {
            "exact_concept": 0,
            "semantic": 0,
            "price_only": 0,
            "no_match": 0,
        }
        for r in records:
            mm: MatchMethod = r.match_method
            if mm in match_counts:
                match_counts[mm] += 1

        header_review_required = any(r.review_required for r in records)
        header_reasons: list[str] = []
        if header_review_required:
            header_reasons.append("at_least_one_line_requires_review")
        if match_counts["no_match"] > 0:
            header_reasons.append(
                f"lines_without_match:{match_counts['no_match']}"
            )

        return ValuationHeaderRecord(
            document_id=envelope.meta.document_id,
            contrato_codigo=envelope.meta.codigo_contrato,
            status="ok",
            provider_ia=envelope.meta.primary_provider,
            model_name=envelope.meta.model,
            prompt_key=envelope.meta.prompt_key,
            total_valorado=round(total_valorado, 2),
            total_lines=len(records),
            lines_matched_exact=match_counts["exact_concept"],
            lines_matched_semantic=match_counts["semantic"],
            lines_matched_price_only=match_counts["price_only"],
            lines_unmatched=match_counts["no_match"],
            review_required=header_review_required,
            review_reasons=header_reasons,
            raw_ia_envelope_json=None,  # lo rellena el pipeline
        )
