# domain/ports/valuation_ia_client.py
from __future__ import annotations

from abc import ABC, abstractmethod

from domain.models.valuation_envelope import ValuationEnvelope


class ValuationPeticionInvalida(RuntimeError):
    """(jul 2026) Peticion INVALIDA hacia la valuation-api (4xx): jamas
    va a tener exito reintentando (p. ej. documento sin lineas de
    albaran en el merge). El pipeline debe cerrar la valoracion como
    'failed' y el consumidor completar el mensaje, NO devolverlo a la
    cola (bucle envenenado: reintento eterno cada visibility-timeout y
    'Valorando...' infinito en el front).
    """


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
