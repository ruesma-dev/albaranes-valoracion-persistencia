# domain/ports/valuation_ia_client.py
from __future__ import annotations

from abc import ABC, abstractmethod

from domain.models.valuation_envelope import ValuationEnvelope


class ValuationIaClient(ABC):
    """Puerto para llamar al servicio 5 (valuation-api)."""

    @abstractmethod
    def value(
        self,
        *,
        document_id: str,
        codigo_contrato: str | None = None,
    ) -> ValuationEnvelope:
        raise NotImplementedError
