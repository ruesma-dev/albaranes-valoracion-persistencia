# domain/ports/unit_registry_port.py
from __future__ import annotations

from abc import ABC, abstractmethod

from domain.models.unit_models import ConversionResult, UnitInfo


class UnitRegistry(ABC):
    """Registro determinista de unidades y conversiones."""

    @abstractmethod
    def classify(self, unit: str | None) -> UnitInfo:
        """Clasifica una unidad. Si no la conoce, devuelve category=unknown."""
        raise NotImplementedError

    @abstractmethod
    def convert(
        self,
        *,
        quantity: float | None,
        from_unit: str | None,
        to_unit: str | None,
    ) -> ConversionResult:
        """Convierte ``quantity`` de ``from_unit`` a ``to_unit``.

        - Si alguna unidad es ``unknown`` o las categorías no coinciden,
          devuelve ``category_match=false`` y no convierte.
        - Si alguna es "ambiguous" (caja, saco, palé…) marca
          ``ambiguous=True`` pero devuelve la conversión (factor 1).
        """
        raise NotImplementedError
