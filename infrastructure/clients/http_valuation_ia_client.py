# infrastructure/clients/http_valuation_ia_client.py
from __future__ import annotations

import logging

import httpx

from domain.models.valuation_envelope import ValuationEnvelope
from domain.ports.valuation_ia_client import ValuationIaClient

logger = logging.getLogger(__name__)


class HttpValuationIaClient(ValuationIaClient):
    """Cliente HTTP contra el servicio 5 (albaranes-valuation-api).

    Timeout alto por diseño: una llamada a Claude con PDF de contrato
    puede tardar 30-120s. Si falla por red/5xx se propaga para que el
    servicio 6 decida (un reintento con backoff a nivel de endpoint es
    opcional; aquí preferimos que el front lo reintente manualmente
    para no bloquear workers).
    """

    def __init__(self, *, base_url: str, timeout_s: float) -> None:
        if not base_url:
            raise ValueError("HttpValuationIaClient requiere base_url")
        self._base_url = base_url.rstrip("/")
        self._timeout_s = float(timeout_s)

    def value(
        self,
        *,
        document_id: str,
        codigo_contrato: str | None = None,
    ) -> ValuationEnvelope:
        url = f"{self._base_url}/v1/albaranes/value"
        payload: dict = {"document_id": document_id}
        if codigo_contrato:
            payload["codigo_contrato"] = codigo_contrato

        logger.info(
            "[valuation-ia-client] POST %s document_id=%s contrato=%s",
            url, document_id, codigo_contrato,
        )

        with httpx.Client(timeout=self._timeout_s) as client:
            response = client.post(url, json=payload)

        if response.status_code >= 400:
            body_preview = (response.text or "")[:500]
            raise RuntimeError(
                f"valuation-api devolvió {response.status_code}: {body_preview}"
            )

        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(
                f"valuation-api respuesta no JSON: {(response.text or '')[:500]}"
            ) from exc

        return ValuationEnvelope.model_validate(data)
