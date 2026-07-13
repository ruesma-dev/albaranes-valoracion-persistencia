# application/services/conciliacion_orchestrator.py
"""Orquestacion de IA4 (conciliacion semantica condicional).

Se ejecuta DESPUES de la valoracion de sv5 (IA3) y ANTES del build
determinista. Recolecta las lineas del envelope que quedaron SIN casar
(o casadas sin precio), llama a IA4 EN LOTE y APLICA sobre el envelope
(matched_contrato_line_id + precio_unitario_contrato_db). Como se modifica
el envelope antes de build(), el importe (unitario * (1 - dto/100) *
cantidad) lo recalcula build() de forma determinista; aqui NO se calculan
importes.

Es best-effort: si IA4 falla o devuelve algo raro, no rompe la valoracion
(se registra y se sigue con lo que dio el determinista).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _necesita_conciliacion(dto: Any) -> bool:
    """Linea sin casar o casada sin precio unitario."""
    return (
        dto.matched_contrato_line_id is None
        or dto.precio_unitario_contrato_db is None
        or dto.match_method == "no_match"
    )


def aplicar_conciliacion_ia4(*, envelope: Any, client: Any) -> int:
    """Concilia IN PLACE las lineas no casadas del envelope. Devuelve nº aplicadas."""
    lineas = envelope.data.lineas
    alb_by_id = {
        l.merge_line_id: l
        for l in envelope.context.lineas_albaran
        if l.merge_line_id is not None
    }

    # 1) Lote de pendientes (referenciadas por indice en envelope.data.lineas,
    #    para que valga tambien para sinteticas con merge_line_id=None).
    pendientes = []
    for i, dto in enumerate(lineas):
        if not _necesita_conciliacion(dto):
            continue
        alb = (
            alb_by_id.get(dto.merge_line_id)
            if dto.merge_line_id is not None
            else None
        )
        descripcion = (
            getattr(dto, "descripcion_linea", None)
            or (getattr(alb, "descripcion", None) if alb else None)
            or getattr(dto, "modifier_reason", None)
            or ""
        )
        cantidad = (
            dto.cantidad_override
            if getattr(dto, "cantidad_override", None) is not None
            else (getattr(alb, "cantidad", None) if alb else None)
        )
        pendientes.append(
            {
                "line_ref": i,
                "descripcion": descripcion,
                "unidad_medida": (
                    getattr(alb, "unidad_medida", None) if alb else None
                ),
                "cantidad": cantidad,
                "codigo_partida": (
                    getattr(alb, "codigo_partida_albaran", None) if alb else None
                ),
                "precio_albaran": (
                    getattr(alb, "precio_unitario_albaran", None) if alb else None
                ),
            }
        )

    if not pendientes:
        return 0

    # 2) Lineas de contrato disponibles.
    lineas_contrato = [
        {
            "id": c.contrato_line_id,
            "descripcion": c.descripcion,
            "unidad_medida": c.unidad_medida,
            "precio_unitario": c.precio_unitario,
            "codigo_partida": c.codigo_partida,
        }
        for c in envelope.context.lineas_contrato
    ]
    if not lineas_contrato:
        logger.info("[conciliacion] IA4 omitida: sin lineas de contrato")
        return 0

    # 3) Llamada a IA4 (best-effort).
    try:
        data = client.conciliar(
            document_id=envelope.meta.document_id,
            lineas_no_casadas=pendientes,
            lineas_contrato=lineas_contrato,
        )
    except Exception as exc:  # pragma: no cover - red/LLM
        logger.warning(
            "[conciliacion] IA4 fallo (best-effort, se ignora): %s", exc
        )
        return 0

    # 4) Aplicar SOLO matches validos (id existente, no inventado, no null).
    ids_validos = {
        c.contrato_line_id for c in envelope.context.lineas_contrato
    }
    n = 0
    for conc in (data or {}).get("conciliaciones") or []:
        ref = conc.get("line_ref")
        mid = conc.get("matched_contrato_line_id")
        precio = conc.get("precio_unitario_contrato_db")
        if not isinstance(ref, int) or ref < 0 or ref >= len(lineas):
            continue
        if mid is None or mid not in ids_validos:
            continue  # no aplicamos no_match ni ids inventados
        dto = lineas[ref]
        dto.matched_contrato_line_id = mid
        if precio is not None:
            try:
                dto.precio_unitario_contrato_db = float(precio)
            except (TypeError, ValueError):
                pass
        dto.match_method = "semantic"
        razon = conc.get("razon_corta")
        if razon:
            dto.razon_corta = ((dto.razon_corta or "") + " | IA4: " + str(razon))[
                :500
            ]
        n += 1

    logger.info(
        "[conciliacion] IA4 aplicada: %s/%s lineas conciliadas", n, len(pendientes)
    )
    return n
