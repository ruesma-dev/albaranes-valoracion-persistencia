# application/pipelines/run_valuation_pipeline.py
from __future__ import annotations

import json
import logging
from typing import Any

from application.services.conciliacion_orchestrator import (
    aplicar_conciliacion_ia4,
)
from dataclasses import dataclass

from application.services.valuation_builder import ValuationBuilder
from domain.models.valuation_records import ValuationHeaderRecord
from domain.ports.valuation_ia_client import ValuationIaClient
from domain.ports.valuation_repository import (
    ExistingValuationSummary,
    PersistedValuation,
    ValuationRepository,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunValuationRequest:
    document_id: str
    codigo_contrato: str | None = None
    force: bool = False
    line_already_valued: bool = False


@dataclass(frozen=True)
class RunValuationResult:
    valuation_id: str
    document_id: str
    status: str
    total_valorado: float
    total_lines: int
    review_required: bool
    review_reasons: list[str]
    duplicate: bool


class RunValuationPipeline:
    """Pipeline principal del servicio 6.

    Pasos:
      1. Si existe valoración y no se fuerza: devolver la existente.
      2. Llamar al servicio 5 via HTTP.
      3. Si status == no_contract: persistir cabecera vacía con
         status='no_contract' y terminar.
      4. Pasarle el envelope al ValuationBuilder.
      5. Persistir en las 3 tablas (replace atómico).
    """

    def __init__(
        self,
        *,
        ia_client: ValuationIaClient,
        repository: ValuationRepository,
        builder: ValuationBuilder,
        conciliacion_client: Any = None,
    ) -> None:
        self._ia_client = ia_client
        self._repository = repository
        self._builder = builder
        self._conciliacion_client = conciliacion_client

    def run(self, request: RunValuationRequest) -> RunValuationResult:
        existing: ExistingValuationSummary | None = (
            self._repository.get_by_document_id(document_id=request.document_id)
        )

        if existing is not None and not request.force:
            logger.info(
                "[run-valuation] existe valoración previa id=%s status=%s; "
                "se devuelve sin re-ejecutar.",
                existing.valuation_id, existing.status,
            )
            return RunValuationResult(
                valuation_id=existing.valuation_id,
                document_id=existing.document_id,
                status=existing.status,
                total_valorado=existing.total_valorado,
                total_lines=existing.total_lines,
                review_required=existing.review_required,
                review_reasons=[],
                duplicate=True,
            )

        # Llamada al servicio 5.
        logger.info(
            "[run-valuation] llamando a IA document_id=%s contrato=%s force=%s",
            request.document_id,
            request.codigo_contrato,
            request.force,
        )
        envelope = self._ia_client.value(
            document_id=request.document_id,
            codigo_contrato=request.codigo_contrato,
        )

        # Caso "no contrato seleccionado".
        if envelope.status == "no_contract":
            logger.info(
                "[run-valuation] document_id=%s sin contrato; "
                "persistiendo cabecera vacía.",
                request.document_id,
            )
            empty_header = ValuationHeaderRecord(
                document_id=request.document_id,
                contrato_codigo=None,
                status="no_contract",
                provider_ia=None,
                model_name=None,
                prompt_key=None,
                total_valorado=0.0,
                total_lines=0,
                lines_matched_exact=0,
                lines_matched_semantic=0,
                lines_matched_price_only=0,
                lines_unmatched=0,
                review_required=True,
                review_reasons=["no_contract_selected"],
                raw_ia_envelope_json=envelope.model_dump_json(),
            )
            persisted = self._repository.replace_valuation(
                header=empty_header, lines=[]
            )
            return RunValuationResult(
                valuation_id=persisted.valuation_id,
                document_id=persisted.document_id,
                status=persisted.status,
                total_valorado=persisted.total_valorado,
                total_lines=persisted.total_lines,
                review_required=persisted.review_required,
                review_reasons=empty_header.review_reasons,
                duplicate=False,
            )

        # IA4 (condicional): conciliar semanticamente las lineas que el
        # determinista no casara (o casadas sin precio), modificando el
        # envelope ANTES del build. build() recalcula el importe
        # determinista (unitario * (1 - dto/100) * cantidad). Best-effort.
        if self._conciliacion_client is not None:
            try:
                aplicar_conciliacion_ia4(
                    envelope=envelope, client=self._conciliacion_client
                )
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.warning("[run-valuation] IA4 fallo: %s", exc)

        # Caso normal: aplicar reglas deterministas y persistir.
        header, lines = self._builder.build(
            envelope=envelope,
            existing_document_already_valued=request.line_already_valued,
        )

        # Adjuntamos el envelope bruto al header para trazabilidad.
        header_with_envelope = ValuationHeaderRecord(
            document_id=header.document_id,
            contrato_codigo=header.contrato_codigo,
            status=header.status,
            provider_ia=header.provider_ia,
            model_name=header.model_name,
            prompt_key=header.prompt_key,
            total_valorado=header.total_valorado,
            total_lines=header.total_lines,
            lines_matched_exact=header.lines_matched_exact,
            lines_matched_semantic=header.lines_matched_semantic,
            lines_matched_price_only=header.lines_matched_price_only,
            lines_unmatched=header.lines_unmatched,
            review_required=header.review_required,
            review_reasons=header.review_reasons,
            raw_ia_envelope_json=envelope.model_dump_json(),
        )

        persisted: PersistedValuation = self._repository.replace_valuation(
            header=header_with_envelope, lines=lines,
        )
        logger.info(
            "[run-valuation] persistida valoración id=%s document_id=%s "
            "lineas=%s total=%.2f review=%s",
            persisted.valuation_id,
            persisted.document_id,
            persisted.total_lines,
            persisted.total_valorado,
            persisted.review_required,
        )
        return RunValuationResult(
            valuation_id=persisted.valuation_id,
            document_id=persisted.document_id,
            status=persisted.status,
            total_valorado=persisted.total_valorado,
            total_lines=persisted.total_lines,
            review_required=persisted.review_required,
            review_reasons=header_with_envelope.review_reasons,
            duplicate=False,
        )
