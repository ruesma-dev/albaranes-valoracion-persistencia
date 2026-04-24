# domain/ports/valuation_repository.py
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from domain.models.valuation_records import (
    LineValuationRecord,
    ValuationHeaderRecord,
    ValuationStatus,
)


@dataclass(frozen=True)
class PersistedValuation:
    valuation_id: str
    document_id: str
    status: ValuationStatus
    total_valorado: float
    total_lines: int
    review_required: bool


@dataclass(frozen=True)
class ExistingValuationSummary:
    valuation_id: str
    document_id: str
    contrato_codigo: str | None
    status: ValuationStatus
    total_valorado: float
    total_lines: int
    review_required: bool
    created_at_utc: str
    updated_at_utc: str | None


class ValuationRepository(ABC):
    """Puerto de persistencia de la valoración."""

    @abstractmethod
    def initialize(self) -> None:
        """Crea/alinea las tres tablas nuevas (idempotente)."""
        raise NotImplementedError

    @abstractmethod
    def get_by_document_id(
        self,
        *,
        document_id: str,
    ) -> ExistingValuationSummary | None:
        raise NotImplementedError

    @abstractmethod
    def replace_valuation(
        self,
        *,
        header: ValuationHeaderRecord,
        lines: list[LineValuationRecord],
    ) -> PersistedValuation:
        """Borra e inserta: la valoración es 1:1 con document_id."""
        raise NotImplementedError

    @abstractmethod
    def read_full_valuation_json(
        self,
        *,
        document_id: str,
    ) -> dict:
        """Devuelve todas las filas de valoración de un documento como
        dict JSON-serializable, para que el front las muestre."""
        raise NotImplementedError
