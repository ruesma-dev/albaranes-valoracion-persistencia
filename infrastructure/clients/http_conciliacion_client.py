# infrastructure/clients/http_conciliacion_client.py
"""Cliente HTTP contra /v1/albaranes/conciliar del servicio 5 (IA4).

Mismo servicio 5 que la valoracion, pero el endpoint de conciliacion
semantica en lote. Best-effort: si falla, el orquestador lo ignora y la
valoracion sigue con el matching determinista.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)


class HttpConciliacionClient:
    def __init__(self, *, base_url: str, timeout_s: float) -> None:
        if not base_url:
            raise ValueError("HttpConciliacionClient requiere base_url")
        self._base_url = base_url.rstrip("/")
        self._timeout_s = float(timeout_s)

    def conciliar(
        self,
        *,
        document_id: str,
        lineas_no_casadas: List[Dict[str, Any]],
        lineas_contrato: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        url = f"{self._base_url}/v1/albaranes/conciliar"
        payload = {
            "document_id": document_id,
            "lineas_no_casadas": lineas_no_casadas,
            "lineas_contrato": lineas_contrato,
        }
        logger.info(
            "[conciliacion-client] POST %s document_id=%s lote=%s contrato=%s",
            url, document_id, len(lineas_no_casadas), len(lineas_contrato),
        )
        with httpx.Client(timeout=self._timeout_s) as client:
            response = client.post(url, json=payload)
        if response.status_code >= 400:
            body = (response.text or "")[:500]
            raise RuntimeError(
                f"conciliar devolvio {response.status_code}: {body}"
            )
        return response.json()
