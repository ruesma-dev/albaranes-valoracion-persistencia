# infrastructure/database/schema_contribution.py
"""Contrato público de schema de este microservicio.

Este módulo expone, en una API estable y versionada, el conjunto de
sentencias DDL idempotentes (CREATE TABLE / ALTER TABLE / CREATE INDEX
con cláusulas ``IF NOT EXISTS``) que describen TODAS las tablas e
índices propios del servicio 6 (``albaran-valoracion-persist``).

Objetivo del patrón
-------------------
El orquestador (servicio 7) NO debe duplicar este DDL en su propio
código. En su lugar, el sv7 debe importar dinámicamente este módulo
desde la ruta del proyecto del sv6 (en local) o desde el package
publicado (en Azure) y llamar a ``get_ddl_statements()``. Así, cuando
añadimos una columna a este servicio, el sv7 NO necesita actualizarse:
basta con redeployar y el orquestador leerá el DDL nuevo.

Contrato esperado por el ``schema_loader`` del sv7:

* ``SCHEMA_NAME``        : nombre lógico único del schema.
* ``SCHEMA_VERSION``     : entero monotónico para diagnóstico (no
                           obliga a migraciones — es informativo).
* ``SCHEMA_DEPENDS_ON``  : lista de ``SCHEMA_NAME`` de otros servicios
                           que deben aplicarse ANTES (FKs entre svcs).
* ``get_ddl_statements()``: devuelve ``list[tuple[label, sql]]``
                           ordenado e idempotente.

Reglas para añadir DDL a este módulo
------------------------------------
1. TODO debe ser idempotente: ``CREATE TABLE IF NOT EXISTS``,
   ``CREATE INDEX IF NOT EXISTS``, ``ADD COLUMN IF NOT EXISTS``, etc.
2. Si una columna nueva es nullable y se añade a una tabla preexistente,
   usa ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``.
3. Si necesitas un cambio NO idempotente (DROP COLUMN, RENAME,
   alteraciones de tipo) — habla con arquitectura. Eso ya es Alembic.
4. Mantén el DDL alineado con
   ``infrastructure/database/orm_valuation_models.py``. El módulo
   ``schema_drift_check.py`` te avisa por log si hay drift.
"""
from __future__ import annotations

from typing import List, Tuple

# --------------------------------------------------------------------------- #
# Identidad del schema (consumido por el sv7).
# --------------------------------------------------------------------------- #

SCHEMA_NAME: str = "valuation"
"""Nombre lógico único del schema. No cambiar a la ligera — el sv7 lo
usa como clave para resolver dependencias y como label en los logs."""

SCHEMA_VERSION: int = 4
"""Versión informativa. Súbela al añadir/eliminar columnas o tablas.
NO se usa para migraciones (no llevamos histórico aquí); solo para que
el sv7 pueda loguear "aplicando valuation v4" y detectar fácilmente
deploys desincronizados."""

SCHEMA_DEPENDS_ON: List[str] = ["albaran_persist"]
"""Schemas que deben estar aplicados ANTES de este. Las FKs de nuestras
tablas (albaran_valuations.document_id → albaran_documents_merge.id,
etc.) apuntan a tablas del servicio 3 — su ``SCHEMA_NAME`` es
``albaran_persist``. El sv7 ordena topológicamente y aplica primero las
dependencias."""


# --------------------------------------------------------------------------- #
# DDL idempotente.
# Consumido tanto por el repo de este servicio (``initialize()``) como
# por el ``schema_loader`` del sv7. Una única fuente de verdad.
# --------------------------------------------------------------------------- #

_DDL_STATEMENTS: Tuple[Tuple[str, str], ...] = (
    # ===================================================================== #
    # 1. albaran_valuations — cabecera de valoración (1:1 con documento).
    # ===================================================================== #
    ("CREATE albaran_valuations", """
        CREATE TABLE IF NOT EXISTS albaran_valuations (
            id                        VARCHAR(36) PRIMARY KEY,
            document_id               VARCHAR(36) NOT NULL UNIQUE
                REFERENCES albaran_documents_merge(id) ON DELETE CASCADE,
            contrato_codigo           VARCHAR(64),
            status                    VARCHAR(32) NOT NULL,
            provider_ia               VARCHAR(32),
            model_name                VARCHAR(100),
            prompt_key                VARCHAR(100),
            total_valorado            DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            total_lines               INTEGER NOT NULL DEFAULT 0,
            lines_matched_exact       INTEGER NOT NULL DEFAULT 0,
            lines_matched_semantic    INTEGER NOT NULL DEFAULT 0,
            lines_matched_price_only  INTEGER NOT NULL DEFAULT 0,
            lines_unmatched           INTEGER NOT NULL DEFAULT 0,
            review_required           BOOLEAN NOT NULL DEFAULT FALSE,
            review_reasons_json       TEXT,
            raw_ia_envelope_json      TEXT,
            created_at_utc            VARCHAR(64) NOT NULL,
            updated_at_utc            VARCHAR(64)
        )
    """),
    ("INDEX albaran_valuations.document_id",
     "CREATE INDEX IF NOT EXISTS ix_albaran_valuations_document_id "
     "ON albaran_valuations(document_id)"),
    ("INDEX albaran_valuations.status",
     "CREATE INDEX IF NOT EXISTS ix_albaran_valuations_status "
     "ON albaran_valuations(status)"),
    ("INDEX albaran_valuations.contrato_codigo",
     "CREATE INDEX IF NOT EXISTS ix_albaran_valuations_contrato_codigo "
     "ON albaran_valuations(contrato_codigo)"),

    # ===================================================================== #
    # 2. contrato_lines_derived — partidas creadas por la valoración.
    # ===================================================================== #
    ("CREATE contrato_lines_derived", """
        CREATE TABLE IF NOT EXISTS contrato_lines_derived (
            id                          SERIAL PRIMARY KEY,
            created_by_valuation_id     VARCHAR(36) NOT NULL
                REFERENCES albaran_valuations(id) ON DELETE CASCADE,
            source_document_id          VARCHAR(36) NOT NULL,
            codigo_contrato             VARCHAR(64) NOT NULL,
            codigo_producto             VARCHAR(64),
            descripcion_linea           TEXT,
            unidad_medida               VARCHAR(32),
            precio_unitario             DOUBLE PRECISION,
            codigo_partida              VARCHAR(64),
            origen                      VARCHAR(32) NOT NULL,
            created_at_utc              VARCHAR(64) NOT NULL
        )
    """),
    ("INDEX contrato_lines_derived.created_by",
     "CREATE INDEX IF NOT EXISTS ix_contrato_lines_derived_created_by "
     "ON contrato_lines_derived(created_by_valuation_id)"),
    ("INDEX contrato_lines_derived.source_doc",
     "CREATE INDEX IF NOT EXISTS ix_contrato_lines_derived_source_doc "
     "ON contrato_lines_derived(source_document_id)"),
    ("INDEX contrato_lines_derived.producto_partida",
     "CREATE INDEX IF NOT EXISTS ix_contrato_lines_derived_producto_partida "
     "ON contrato_lines_derived(codigo_contrato, codigo_producto, "
     "codigo_partida)"),

    # ===================================================================== #
    # 3. albaran_line_valuations — resultado por línea.
    #
    # OJO: el CREATE describe el schema INICIAL (versión 1).
    # Cualquier columna añadida después va como ALTER ... ADD COLUMN
    # IF NOT EXISTS más abajo. Así, una BBDD vieja con la tabla ya
    # creada (sin las columnas nuevas) se autorrepara en cuanto este
    # módulo se aplica.
    # ===================================================================== #
    ("CREATE albaran_line_valuations", """
        CREATE TABLE IF NOT EXISTS albaran_line_valuations (
            id                             SERIAL PRIMARY KEY,
            valuation_id                   VARCHAR(36) NOT NULL
                REFERENCES albaran_valuations(id) ON DELETE CASCADE,
            merge_line_id                  INTEGER
                REFERENCES albaran_lines_merge(id) ON DELETE CASCADE,
            matched_contrato_line_id       INTEGER
                REFERENCES albaran_contrato_lines_merge(id) ON DELETE SET NULL,
            derived_contrato_line_id       INTEGER
                REFERENCES contrato_lines_derived(id) ON DELETE SET NULL,
            precio_unitario_contrato_db    DOUBLE PRECISION,
            precio_unitario_pdf_inferido   DOUBLE PRECISION,
            precio_unitario_final          DOUBLE PRECISION,
            precio_unitario_source         VARCHAR(32) NOT NULL,
            precio_unitario_agreement      VARCHAR(32) NOT NULL,
            unidad_albaran                 VARCHAR(32),
            unidad_contrato                VARCHAR(32),
            unidad_categoria               VARCHAR(32) NOT NULL,
            unidad_category_match          BOOLEAN NOT NULL,
            cantidad_albaran               DOUBLE PRECISION,
            cantidad_convertida            DOUBLE PRECISION,
            factor_conversion              DOUBLE PRECISION,
            importe_calculado              DOUBLE PRECISION,
            importe_albaran_declarado      DOUBLE PRECISION,
            importe_source                 VARCHAR(32) NOT NULL,
            codigo_partida_albaran         VARCHAR(64),
            codigo_partida_final           VARCHAR(64),
            partida_action                 VARCHAR(32) NOT NULL,
            match_confidence_pct           DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            match_method                   VARCHAR(32) NOT NULL,
            review_required                BOOLEAN NOT NULL DEFAULT FALSE,
            review_reasons_json            TEXT,
            ia_reasoning                   TEXT,
            created_at_utc                 VARCHAR(64) NOT NULL,
            CONSTRAINT uq_albaran_line_valuations_val_line
                UNIQUE (valuation_id, merge_line_id)
        )
    """),
    ("INDEX albaran_line_valuations.valuation_id",
     "CREATE INDEX IF NOT EXISTS ix_albaran_line_valuations_valuation_id "
     "ON albaran_line_valuations(valuation_id)"),
    ("INDEX albaran_line_valuations.merge_line_id",
     "CREATE INDEX IF NOT EXISTS ix_albaran_line_valuations_merge_line_id "
     "ON albaran_line_valuations(merge_line_id)"),
    ("INDEX albaran_line_valuations.matched_contrato",
     "CREATE INDEX IF NOT EXISTS ix_albaran_line_valuations_matched_contrato "
     "ON albaran_line_valuations(matched_contrato_line_id)"),
    ("INDEX albaran_line_valuations.derived_contrato",
     "CREATE INDEX IF NOT EXISTS ix_albaran_line_valuations_derived_contrato "
     "ON albaran_line_valuations(derived_contrato_line_id)"),
    ("INDEX albaran_line_valuations.match_method",
     "CREATE INDEX IF NOT EXISTS ix_albaran_line_valuations_match_method "
     "ON albaran_line_valuations(match_method)"),

    # --------------------------------------------------------------------- #
    # ALTER de evolución — orden cronológico.
    #
    # Idempotente. Si la tabla se creó con un schema antiguo (por ejemplo,
    # porque el sv7 la creó con su DDL viejo cacheado), estas sentencias
    # la actualizan a la versión actual sin perder datos.
    # --------------------------------------------------------------------- #

    # Sub-tanda 2C — contexto estructural por línea.
    ("ALTER albaran_line_valuations.rol_linea",
     "ALTER TABLE albaran_line_valuations "
     "ADD COLUMN IF NOT EXISTS rol_linea VARCHAR(32)"),
    ("ALTER albaran_line_valuations.ref_linea_base_merge_id",
     "ALTER TABLE albaran_line_valuations "
     "ADD COLUMN IF NOT EXISTS ref_linea_base_merge_id INTEGER"),
    ("ALTER albaran_line_valuations.tarifa_pdf_encontrada",
     "ALTER TABLE albaran_line_valuations "
     "ADD COLUMN IF NOT EXISTS tarifa_pdf_encontrada BOOLEAN"),
    ("ALTER albaran_line_valuations.modifiers_applied_json",
     "ALTER TABLE albaran_line_valuations "
     "ADD COLUMN IF NOT EXISTS modifiers_applied_json TEXT"),

    # Sub-tanda 2D — soporte de líneas sintéticas.
    ("ALTER merge_line_id DROP NOT NULL",
     "ALTER TABLE albaran_line_valuations "
     "ALTER COLUMN merge_line_id DROP NOT NULL"),
    ("ALTER albaran_line_valuations.line_kind",
     "ALTER TABLE albaran_line_valuations "
     "ADD COLUMN IF NOT EXISTS line_kind VARCHAR(32) "
     "NOT NULL DEFAULT 'from_albaran'"),
    ("ALTER albaran_line_valuations.parent_merge_line_id",
     "ALTER TABLE albaran_line_valuations "
     "ADD COLUMN IF NOT EXISTS parent_merge_line_id INTEGER"),
    ("ALTER albaran_line_valuations.modifier_source",
     "ALTER TABLE albaran_line_valuations "
     "ADD COLUMN IF NOT EXISTS modifier_source VARCHAR(32)"),
    ("ALTER albaran_line_valuations.modifier_reason",
     "ALTER TABLE albaran_line_valuations "
     "ADD COLUMN IF NOT EXISTS modifier_reason TEXT"),
    ("ALTER albaran_line_valuations.descripcion_linea",
     "ALTER TABLE albaran_line_valuations "
     "ADD COLUMN IF NOT EXISTS descripcion_linea TEXT"),
    ("INDEX albaran_line_valuations.parent_merge_line_id",
     "CREATE INDEX IF NOT EXISTS ix_albaran_line_valuations_parent "
     "ON albaran_line_valuations(parent_merge_line_id)"),
    ("INDEX albaran_line_valuations.line_kind",
     "CREATE INDEX IF NOT EXISTS ix_albaran_line_valuations_line_kind "
     "ON albaran_line_valuations(line_kind)"),

    # Tanda descuento — abr 2026.
    # Esta es la columna que faltaba en la BBDD productiva y rompía el
    # INSERT. Idempotente: si ya existe no hace nada.
    ("ALTER albaran_line_valuations.descuento_albaran_aplicado",
     "ALTER TABLE albaran_line_valuations "
     "ADD COLUMN IF NOT EXISTS descuento_albaran_aplicado DOUBLE PRECISION"),
)


# --------------------------------------------------------------------------- #
# API pública (consumida por sv7 schema_loader y por el repo de sv6).
# --------------------------------------------------------------------------- #

def get_ddl_statements() -> List[Tuple[str, str]]:
    """Devuelve la lista ordenada de sentencias DDL idempotentes.

    El orden importa: primero CREATE TABLE de cabecera, luego sus FKs
    descendentes, luego ALTER de evolución, luego índices.

    Returns
    -------
    list[tuple[str, str]]
        Pares ``(label, sql)``. ``label`` es un identificador humano
        para logs; ``sql`` es la sentencia idempotente.
    """
    return list(_DDL_STATEMENTS)


def get_owned_table_names() -> List[str]:
    """Lista las tablas que este schema CREA (no las que solo referencia).

    Útil para verificación tras aplicar el DDL — el caller puede
    comprobar que las tablas existen en la BBDD. NO incluye tablas de
    otros servicios (las FK ``albaran_documents_merge``, etc., son
    propiedad del servicio 3 / schema ``albaran_persist``).
    """
    return [
        "albaran_valuations",
        "contrato_lines_derived",
        "albaran_line_valuations",
    ]


def get_external_table_dependencies() -> List[str]:
    """Tablas de OTROS schemas que este schema referencia por FK.

    Informativo. El sv7 puede usar esto para verificar el orden
    topológico real (más allá de ``SCHEMA_DEPENDS_ON``).
    """
    return [
        "albaran_documents_merge",        # propiedad de sv3
        "albaran_lines_merge",            # propiedad de sv3
        "albaran_contrato_lines_merge",   # propiedad de sv3
    ]
