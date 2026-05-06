# infrastructure/database/schema_drift_check.py
"""Detector de drift entre el ORM y la BBDD real.

Para cada tabla del ORM declarado en ``orm_valuation_models``, compara
las columnas que el ORM espera con las que existen realmente en la
tabla en PostgreSQL. Si alguna columna del ORM NO está en BBDD, lo
loguea como ERROR y devuelve la lista — para que el caller pueda
decidir si abortar el arranque o seguir y dejar que falle al primer
INSERT (el comportamiento actual antes de este fix).

Esta utilidad NO modifica nada. Es solo diagnóstico. El que arregla la
BBDD es ``schema_contribution.get_ddl_statements()``.

Pensado para correr al arrancar el servicio, justo después de
``repository.initialize()``. Si el DDL contributor está bien
sincronizado con el ORM, este detector siempre reportará "OK".

Por qué existe
--------------
El problema que motivó este módulo: la tabla existía en BBDD pero
faltaba ``descuento_albaran_aplicado``. El INSERT lo descubrió al
ejecutarse — fallo en runtime, costoso de diagnosticar. Con este
chequeo al arrancar, el log lo dice ANTES de que llegue ningún tráfico.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from infrastructure.database.orm_valuation_models import Base

logger = logging.getLogger(__name__)


@dataclass
class TableDrift:
    """Diferencias entre ORM y BBDD para una tabla concreta."""

    table_name: str
    table_exists_in_db: bool
    missing_in_db: List[str] = field(default_factory=list)
    extra_in_db: List[str] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return (
            (not self.table_exists_in_db)
            or bool(self.missing_in_db)
        )


@dataclass
class DriftReport:
    """Reporte agregado del chequeo."""

    tables: List[TableDrift] = field(default_factory=list)

    @property
    def has_any_drift(self) -> bool:
        return any(t.has_drift for t in self.tables)

    @property
    def critical_drift(self) -> List[TableDrift]:
        """Drift que rompe inserts: tabla ausente o columnas faltantes."""
        return [t for t in self.tables if t.has_drift]


def check_drift(engine: Engine, *, only_owned: bool = True) -> DriftReport:
    """Compara el ORM con la BBDD real y devuelve un reporte.

    Parameters
    ----------
    engine
        Engine SQLAlchemy ya conectado a la BBDD que queremos auditar.
    only_owned
        Si ``True`` (default), ignora las tablas-stub del ORM (las que
        solo declaran la PK para que la FK resuelva) — porque esas
        tablas son propiedad de otros servicios y este check no debe
        opinar sobre ellas. Las identificamos porque solo tienen una
        columna (``id``).

    Returns
    -------
    DriftReport
    """
    inspector = inspect(engine)
    db_tables = set(inspector.get_table_names())

    report = DriftReport()

    for table in Base.metadata.sorted_tables:
        # Filtrar stubs (tablas modeladas solo con PK para FK-resolution).
        if only_owned and len(table.columns) <= 1:
            continue

        orm_cols = {col.name for col in table.columns}

        if table.name not in db_tables:
            report.tables.append(
                TableDrift(
                    table_name=table.name,
                    table_exists_in_db=False,
                    missing_in_db=sorted(orm_cols),
                )
            )
            continue

        db_cols = {c["name"] for c in inspector.get_columns(table.name)}

        missing = sorted(orm_cols - db_cols)
        extra = sorted(db_cols - orm_cols)

        report.tables.append(
            TableDrift(
                table_name=table.name,
                table_exists_in_db=True,
                missing_in_db=missing,
                extra_in_db=extra,
            )
        )

    return report


def log_drift_report(report: DriftReport, *, service_label: str) -> None:
    """Vuelca el reporte por log en formato legible.

    - Drift crítico (tabla ausente o columna del ORM faltante en BBDD)
      → log ERROR. El servicio funcionará MAL: los INSERT que toquen
      esa columna fallarán.
    - Columnas extra en BBDD (existen en BBDD pero no en el ORM) →
      log WARNING. No rompe nada, pero indica un schema obsoleto que
      conviene limpiar en algún momento.
    - Si todo OK, log INFO.
    """
    if not report.has_any_drift:
        logger.info(
            "[%s][drift-check] OK — ORM y BBDD están sincronizados (%d tablas).",
            service_label,
            len(report.tables),
        )
        return

    for drift in report.tables:
        if not drift.table_exists_in_db:
            logger.error(
                "[%s][drift-check] tabla '%s' NO EXISTE en BBDD. "
                "Esto romperá las operaciones. Faltarían columnas: %s",
                service_label,
                drift.table_name,
                drift.missing_in_db,
            )
            continue

        if drift.missing_in_db:
            logger.error(
                "[%s][drift-check] tabla '%s' — columnas del ORM "
                "AUSENTES en BBDD: %s. Los INSERT que las usen fallarán. "
                "Probablemente el DDL no se aplicó completo. Revisa "
                "schema_contribution.py y el orquestador.",
                service_label,
                drift.table_name,
                drift.missing_in_db,
            )

        if drift.extra_in_db:
            logger.warning(
                "[%s][drift-check] tabla '%s' — columnas en BBDD que el "
                "ORM ya no declara: %s. No rompe nada, pero el schema "
                "está desfasado. Considera limpiar.",
                service_label,
                drift.table_name,
                drift.extra_in_db,
            )
