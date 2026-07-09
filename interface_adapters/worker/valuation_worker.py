# interface_adapters/worker/valuation_worker.py
"""Handler del worker de valoración (consumidor de q-valoracion).

Recibe ``MensajeValoracion`` (document_id, codigo_contrato, force) y ejecuta
el ``RunValuationPipeline`` real de sv6 — que llama a sv5 (IA) por HTTP,
aplica las reglas deterministas y persiste la valoración en BBDD. No publica
en ninguna cola siguiente: el documento queda valorado y a la espera de
revisión/aprobación en sv4 (estado de reposo 'awaiting_approval').

Idempotencia: con entrega *at-least-once* un mismo document_id puede llegar
más de una vez. ``RunValuationPipeline`` ya es idempotente con ``force=False``
(si existe valoración previa la devuelve sin re-ejecutar la IA). Por eso el
worker IGNORA ``force`` salvo que el propio mensaje lo pida explícitamente
(p. ej. un re-lanzamiento desde sv4): así un redelivery accidental no vuelve
a gastar una llamada al LLM.
"""
from __future__ import annotations

import logging

from application.pipelines.run_valuation_pipeline import (
    RunValuationPipeline,
    RunValuationRequest,
)
from ruesma_comun.colas import MensajeValoracion
from ruesma_comun.colas.consumidor import ManejadorMensaje

logger = logging.getLogger(__name__)


def construir_handler_valoracion(
    *,
    pipeline: RunValuationPipeline,
) -> ManejadorMensaje:
    """Devuelve el handler que el consumidor invocará por cada mensaje."""

    def handler(mensaje: MensajeValoracion) -> None:
        document_id = mensaje.document_id
        codigo_contrato = mensaje.codigo_contrato
        force = bool(mensaje.force)
        logger.info(
            "[ca-valorador] document_id=%s START contrato=%s force=%s",
            document_id, codigo_contrato, force,
        )

        result = pipeline.run(
            RunValuationRequest(
                document_id=document_id,
                codigo_contrato=codigo_contrato,
                force=force,
            )
        )

        if result.duplicate:
            logger.info(
                "[ca-valorador] document_id=%s YA VALORADO (duplicate); "
                "valuation_id=%s status=%s — no se re-ejecuta la IA.",
                document_id, result.valuation_id, result.status,
            )
            return

        logger.info(
            "[ca-valorador] document_id=%s OK -> valorado "
            "(valuation_id=%s status=%s lineas=%s total=%.2f review=%s)",
            document_id,
            result.valuation_id,
            result.status,
            result.total_lines,
            result.total_valorado,
            result.review_required,
        )
        if result.review_required and result.review_reasons:
            logger.info(
                "[ca-valorador] document_id=%s review_reasons=%s",
                document_id, result.review_reasons,
            )

    return handler
