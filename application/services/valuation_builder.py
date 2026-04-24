# application/services/valuation_builder.py
from __future__ import annotations

import logging
from typing import Dict

from application.services.importe_calculator import ImporteCalculator
from application.services.partida_matcher import (
    PartidaMatcher,
    PartidaMatchResult,
)
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

    Se procesa en TRES pasadas (sub-tanda 2D):

      Pasada 1: líneas base.
        - line_kind='from_albaran', rol_linea in (None, 'base').
        - Matching normal (partida_matcher.match).

      Pasada 2: líneas complementarias declaradas.
        - line_kind='from_albaran', rol_linea != 'base' (la línea está
          declarada en el albarán, no la ha generado el valorador).
        - Heredan partida de su base vía ref_linea_base_merge_id
          (sub-tanda 2C, resolve_partida_for_complementaria).

      Pasada 3: líneas sintéticas (sub-tanda 2D).
        - line_kind='synthetic_modifier'.
        - Heredan partida de su base vía parent_merge_line_id
          (resolve_partida_for_synthetic).

    Este orden garantiza que cuando una línea complementaria o sintética
    consulta la partida de su base, la base ya está resuelta.
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

        merge_id_by_line_index: Dict[int, int] = {
            line.line_index: line.merge_line_id
            for line in envelope.context.lineas_albaran
        }

        # Clasificación en TRES grupos.
        base_lines: list[tuple[int, LineValuationDto]] = []
        complementarias: list[tuple[int, LineValuationDto]] = []
        sinteticas: list[tuple[int, LineValuationDto]] = []

        for idx, line in enumerate(envelope.data.lineas):
            if line.line_kind == "synthetic_modifier":
                sinteticas.append((idx, line))
                continue
            ctx = self._get_albaran_ctx(line, albaran_by_id)
            if ctx is None or ctx.rol_linea in (None, "base"):
                base_lines.append((idx, line))
            else:
                complementarias.append((idx, line))

        # Records indexados por la posición ORIGINAL en envelope.data.lineas
        # para poder recomponer al final en el orden recibido.
        records_by_index: Dict[int, LineValuationRecord] = {}
        # Records base (from_albaran) indexados por merge_line_id para que
        # complementarias y sintéticas puedan heredar su partida.
        records_by_merge_id: Dict[int, LineValuationRecord] = {}

        # -----------------------------------------------------------------
        # Pasada 1: líneas base.
        # -----------------------------------------------------------------
        for idx, line in base_lines:
            record = self._build_line(
                line=line,
                albaran_by_id=albaran_by_id,
                contrato_by_id=contrato_by_id,
                contrato_lines=contrato_lines,
                line_already_valued=existing_document_already_valued,
                partida_override=None,
                ref_linea_base_merge_id=None,
            )
            records_by_index[idx] = record
            if record.merge_line_id is not None:
                records_by_merge_id[record.merge_line_id] = record

        # -----------------------------------------------------------------
        # Pasada 2: líneas complementarias declaradas (sub-tanda 2C).
        # -----------------------------------------------------------------
        for idx, line in complementarias:
            ctx = self._get_albaran_ctx(line, albaran_by_id)
            ref_base_merge_id: int | None = None
            partida_base: str | None = None

            if ctx is not None and ctx.ref_linea_base is not None:
                ref_base_merge_id = merge_id_by_line_index.get(ctx.ref_linea_base)
                if ref_base_merge_id is not None:
                    base_record = records_by_merge_id.get(ref_base_merge_id)
                    if base_record is not None:
                        partida_base = base_record.codigo_partida_final
                    else:
                        logger.warning(
                            "[builder] complementaria merge_id=%s apunta a "
                            "ref_linea_base=%s (merge_id=%s) no resuelta; "
                            "partida null.",
                            line.merge_line_id, ctx.ref_linea_base,
                            ref_base_merge_id,
                        )
                else:
                    logger.warning(
                        "[builder] complementaria merge_id=%s con "
                        "ref_linea_base=%s no encontrada en lineas_albaran.",
                        line.merge_line_id, ctx.ref_linea_base,
                    )

            record = self._build_line(
                line=line,
                albaran_by_id=albaran_by_id,
                contrato_by_id=contrato_by_id,
                contrato_lines=contrato_lines,
                line_already_valued=existing_document_already_valued,
                partida_override=partida_base,
                ref_linea_base_merge_id=ref_base_merge_id,
            )
            records_by_index[idx] = record
            if record.merge_line_id is not None:
                records_by_merge_id[record.merge_line_id] = record

        # -----------------------------------------------------------------
        # Pasada 3: líneas sintéticas (sub-tanda 2D).
        # -----------------------------------------------------------------
        for idx, line in sinteticas:
            parent_id = line.parent_merge_line_id
            partida_heredada: str | None = None
            parent_record: LineValuationRecord | None = None
            if parent_id is None:
                logger.warning(
                    "[builder] sintética sin parent_merge_line_id; "
                    "descripcion=%r.",
                    line.descripcion_linea,
                )
            else:
                parent_record = records_by_merge_id.get(parent_id)
                if parent_record is None:
                    logger.warning(
                        "[builder] sintética parent_merge_line_id=%s "
                        "no resuelta.",
                        parent_id,
                    )
                else:
                    partida_heredada = parent_record.codigo_partida_final

            record = self._build_synthetic_line(
                line=line,
                parent_merge_line_id=parent_id,
                partida_heredada=partida_heredada,
                parent_record=parent_record,
                albaran_by_id=albaran_by_id,
            )
            records_by_index[idx] = record

        # Recomponemos en el orden original.
        records = [
            records_by_index[i]
            for i in range(len(envelope.data.lineas))
            if i in records_by_index
        ]

        header = self._build_header(
            envelope=envelope,
            records=records,
        )
        return header, records

    # ------------------------------------------------------------------ #
    # Auxiliares
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_albaran_ctx(
        line: LineValuationDto,
        albaran_by_id: Dict[int, AlbaranLineContextDto],
    ):
        """Devuelve el ContextoLinea de la línea, o None.

        Solo tiene sentido para líneas from_albaran (con merge_line_id).
        Las sintéticas no tienen contexto propio — heredan el de la base.
        """
        if line.merge_line_id is None:
            return None
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
    ) -> LineValuationRecord:
        """Construye el record de una línea from_albaran (base o complementaria)."""
        albaran_line = albaran_by_id.get(line.merge_line_id)  # type: ignore[arg-type]
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

        # 1. Unit guard
        categoria, category_match, guard_reasons = self._guard.resolve(
            line=line,
            unidad_albaran=unidad_albaran,
            unidad_contrato=unidad_contrato,
        )

        # 2. Price reconciliation
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

        # 3. Partida matching
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

        effective_matched_id = partida_result.matched_contrato_line_id
        if effective_matched_id is None and partida_result.derived_line is None:
            effective_matched_id = line.matched_contrato_line_id

        # 4. Unit conversion
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

        # 5. Importe
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

        reasons: list[str] = []
        reasons.extend(guard_reasons)
        reasons.extend(reconciliation.reasons)
        reasons.extend(partida_result.reasons)
        reasons.extend(converted.reasons)
        reasons.extend(importe_result.reasons)
        if line.match_method == "no_match":
            reasons.append("ia_no_match")

        if ctx is None:
            tarifa_pdf_encontrada: bool | None = None
        else:
            tarifa_pdf_encontrada = line.precio_unitario_pdf_inferido is not None
            if not tarifa_pdf_encontrada:
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
            # Sub-tanda 2C
            rol_linea=rol_linea,
            ref_linea_base_merge_id=ref_linea_base_merge_id,
            tarifa_pdf_encontrada=tarifa_pdf_encontrada,
            modifiers_applied=None,
            # Sub-tanda 2D
            line_kind="from_albaran",
            parent_merge_line_id=None,
            modifier_source=None,
            modifier_reason=None,
            descripcion_linea=None,
        )

    def _build_synthetic_line(
        self,
        *,
        line: LineValuationDto,
        parent_merge_line_id: int | None,
        partida_heredada: str | None,
        parent_record: LineValuationRecord | None,
        albaran_by_id: Dict[int, AlbaranLineContextDto],
    ) -> LineValuationRecord:
        """Construye el record de una línea sintética (sub-tanda 2D).

        Reglas:
          - No tiene línea del albarán propia. Hereda cantidad y unidad
            del parent. La herencia preferida es desde el RECORD YA
            RESUELTO del parent (``parent_record``), no del albarán
            directamente — los albaranes de hormigón suelen no traer
            unidad como columna textual (el OCR no la captura) y por
            tanto ``parent_albaran.unidad_medida`` puede ser None. En
            ese caso la unidad real vive en ``parent_record.unidad_contrato``
            (que viene del matching con la línea del contrato) o
            ``parent_record.unidad_albaran``.
          - Hereda partida de la base (resolve_partida_for_synthetic).
          - No se crea línea derivada en contrato.
          - precio_unitario_final = precio_unitario_pdf_inferido del LLM.
          - Si precio null → review_required con motivo
            'modifier_identified_no_tariff' (el revisor decide si factura).
          - importe = cantidad * precio cuando ambos disponibles.
        """
        parent_albaran = (
            albaran_by_id.get(parent_merge_line_id)
            if parent_merge_line_id is not None
            else None
        )

        # Cantidad: del parent_record si existe (ya resuelto con factor
        # de conversión), si no del albarán directamente como fallback.
        if parent_record is not None and parent_record.cantidad_albaran is not None:
            cantidad = parent_record.cantidad_albaran
        elif parent_albaran is not None:
            cantidad = parent_albaran.cantidad
        else:
            cantidad = None

        # Unidad: prioridad al record ya resuelto del parent
        # (unidad_contrato → unidad_albaran), con fallback al albarán.
        unidad: str | None = None
        if parent_record is not None:
            unidad = parent_record.unidad_contrato or parent_record.unidad_albaran
        if not unidad and parent_albaran is not None:
            unidad = parent_albaran.unidad_medida

        # Categoría de unidad: se hereda igual.
        unidad_cat: str
        if parent_record is not None and parent_record.unidad_categoria:
            unidad_cat = parent_record.unidad_categoria
        elif parent_albaran is not None and parent_albaran.unidad_categoria:
            unidad_cat = parent_albaran.unidad_categoria
        else:
            unidad_cat = "unknown"

        precio_final = line.precio_unitario_pdf_inferido
        precio_source = "pdf_inference" if precio_final is not None else "none"
        precio_agreement = "only_1b" if precio_final is not None else "neither"

        importe_calc: float | None = None
        importe_source = "none"
        if cantidad is not None and precio_final is not None:
            importe_calc = round(float(cantidad) * float(precio_final), 2)
            importe_source = "calculated"

        partida_result = self._partida_matcher.resolve_partida_for_synthetic(
            codigo_partida_base=partida_heredada,
        )

        reasons: list[str] = list(partida_result.reasons)
        if precio_final is None:
            reasons.append("modifier_identified_no_tariff")
        if parent_merge_line_id is None:
            reasons.append("synthetic_without_parent")
        elif parent_albaran is None:
            reasons.append("synthetic_parent_not_in_context")

        review_required = (
            precio_final is None
            or parent_merge_line_id is None
            or parent_albaran is None
            or line.match_confidence_pct < 60.0
        )

        tarifa_pdf_encontrada: bool | None = precio_final is not None

        return LineValuationRecord(
            merge_line_id=None,
            matched_contrato_line_id=line.matched_contrato_line_id,
            derived_contrato_line_record=None,
            precio_unitario_contrato_db=line.precio_unitario_contrato_db,
            precio_unitario_pdf_inferido=line.precio_unitario_pdf_inferido,
            precio_unitario_final=precio_final,
            precio_unitario_source=precio_source,  # type: ignore[arg-type]
            precio_unitario_agreement=precio_agreement,  # type: ignore[arg-type]
            unidad_albaran=unidad,
            unidad_contrato=unidad,
            unidad_categoria=unidad_cat,
            unidad_category_match=True,
            cantidad_albaran=cantidad,
            cantidad_convertida=cantidad,
            factor_conversion=1.0 if cantidad is not None else None,
            importe_calculado=importe_calc,
            importe_albaran_declarado=None,
            importe_source=importe_source,  # type: ignore[arg-type]
            codigo_partida_albaran=None,
            codigo_partida_final=partida_result.codigo_partida_final,
            partida_action=partida_result.partida_action,
            match_confidence_pct=float(line.match_confidence_pct),
            match_method=line.match_method,  # type: ignore[arg-type]
            review_required=review_required,
            review_reasons=reasons,
            ia_reasoning=line.razon_corta,
            # Sub-tanda 2C
            rol_linea=line.rol_linea,
            ref_linea_base_merge_id=None,
            tarifa_pdf_encontrada=tarifa_pdf_encontrada,
            modifiers_applied=None,
            # Sub-tanda 2D — identificación de la sintética
            line_kind="synthetic_modifier",
            parent_merge_line_id=parent_merge_line_id,
            modifier_source=line.modifier_source,
            modifier_reason=line.modifier_reason,
            descripcion_linea=line.descripcion_linea,
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
            raw_ia_envelope_json=None,
        )
