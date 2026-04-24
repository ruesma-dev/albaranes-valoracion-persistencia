# infrastructure/database/sqlalchemy_valuation_repository.py
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text

from domain.models.valuation_records import (
    DerivedContratoLineRecord,
    LineValuationRecord,
    ValuationHeaderRecord,
)
from domain.ports.valuation_repository import (
    ExistingValuationSummary,
    PersistedValuation,
    ValuationRepository,
)
from infrastructure.database.orm_valuation_models import (
    AlbaranLineValuationOrm,
    AlbaranValuationOrm,
    ContratoLineDerivedOrm,
)
from infrastructure.database.session_factory import SessionFactory

logger = logging.getLogger(__name__)


_DDL_STATEMENTS: tuple[tuple[str, str], ...] = (
    # (descripción_corta, sql)
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
     "ON contrato_lines_derived(codigo_contrato, codigo_producto, codigo_partida)"),

    ("CREATE albaran_line_valuations", """
        CREATE TABLE IF NOT EXISTS albaran_line_valuations (
            id                             SERIAL PRIMARY KEY,
            valuation_id                   VARCHAR(36) NOT NULL
                REFERENCES albaran_valuations(id) ON DELETE CASCADE,
            merge_line_id                  INTEGER NOT NULL
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

    # -----------------------------------------------------------------
    # Sub-tanda 2C: columnas nuevas en albaran_line_valuations para
    # persistir el contexto estructural que llega en el envelope del
    # svc5. Idempotentes (IF NOT EXISTS). El svc3 también las crea
    # (_VALUATION_DDL) como doble red de seguridad.
    #
    # Ver domain/models/valuation_records.py (LineValuationRecord)
    # para la semántica de cada campo.
    # -----------------------------------------------------------------
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
)


class SqlAlchemyValuationRepository(ValuationRepository):
    """Repositorio con inicialización PEREZOSA y DDL por sentencia.

    Cada sentencia DDL se ejecuta en su propia transacción para que un
    fallo puntual no invalide el resto. Al final, comprueba que las
    tres tablas existen de verdad en la BBDD; si no, levanta un error
    claro.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._initialized = False

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    # ------------------------------------------------------------------ #
    # Init — perezosa, por sentencia, con verificación final
    # ------------------------------------------------------------------ #
    def initialize(self) -> None:
        if self._initialized:
            return

        # Prerequisito: tablas del svc 3 presentes.
        missing_svc3 = self._missing_service3_tables()
        if missing_svc3:
            raise RuntimeError(
                "No se pueden crear las tablas del servicio 6 porque "
                f"faltan tablas del servicio 3: {missing_svc3}. "
                "Arranca el servicio 3 al menos una vez contra la misma "
                "base de datos (envíale un albarán) y reintenta."
            )

        engine = self._session_factory.engine
        logger.info(
            "[valuation-repo] Ejecutando %s sentencias DDL (autocommit) ...",
            len(_DDL_STATEMENTS),
        )
        # AUTOCOMMIT: cada sentencia DDL se confirma por separado. Así un
        # fallo puntual no invalida la transacción entera, y además
        # hacemos imposible "la transacción silenciosa abortada".
        autocommit_engine = engine.execution_options(
            isolation_level="AUTOCOMMIT",
        )
        for idx, (label, ddl) in enumerate(_DDL_STATEMENTS, start=1):
            try:
                with autocommit_engine.connect() as conn:
                    conn.execute(text(ddl))
                logger.info(
                    "[valuation-repo]   [%2s/%2s] OK %s",
                    idx, len(_DDL_STATEMENTS), label,
                )
            except Exception as exc:
                logger.error(
                    "[valuation-repo]   [%2s/%2s] FALLO %s -> %s",
                    idx, len(_DDL_STATEMENTS), label, exc,
                )
                raise

        # Verificación: las 3 tablas tienen que existir.
        still_missing = self._missing_own_tables()
        if still_missing:
            raise RuntimeError(
                "Tras ejecutar la DDL, siguen faltando tablas: "
                f"{still_missing}. Esto no debería pasar. Revisa los "
                "permisos del usuario de BBDD y los logs anteriores."
            )

        self._initialized = True
        logger.info(
            "[valuation-repo] Inicialización completa y VERIFICADA. "
            "Tablas: albaran_valuations, albaran_line_valuations, "
            "contrato_lines_derived"
        )

    def _missing_service3_tables(self) -> list[str]:
        required = [
            "albaran_documents_merge",
            "albaran_lines_merge",
            "albaran_contrato_lines_merge",
        ]
        return self._missing_tables(required)

    def _missing_own_tables(self) -> list[str]:
        required = [
            "albaran_valuations",
            "albaran_line_valuations",
            "contrato_lines_derived",
        ]
        return self._missing_tables(required)

    def _missing_tables(self, required: list[str]) -> list[str]:
        missing: list[str] = []
        try:
            with self._session_factory.create_session() as session:
                for table_name in required:
                    row = session.execute(
                        text("SELECT to_regclass(:qualified)"),
                        {"qualified": f"public.{table_name}"},
                    ).first()
                    if row is None or row[0] is None:
                        missing.append(table_name)
        except Exception:
            logger.exception(
                "[valuation-repo] No se pudo comprobar existencia de tablas."
            )
            return required
        return missing

    # ------------------------------------------------------------------ #
    # Lectura
    # ------------------------------------------------------------------ #
    def get_by_document_id(
        self,
        *,
        document_id: str,
    ) -> ExistingValuationSummary | None:
        self.initialize()
        with self._session_factory.create_session() as session:
            row = session.scalar(
                select(AlbaranValuationOrm).where(
                    AlbaranValuationOrm.document_id == document_id,
                )
            )
            if row is None:
                return None
            return ExistingValuationSummary(
                valuation_id=row.id,
                document_id=row.document_id,
                contrato_codigo=row.contrato_codigo,
                status=row.status,  # type: ignore[arg-type]
                total_valorado=float(row.total_valorado or 0.0),
                total_lines=int(row.total_lines or 0),
                review_required=bool(row.review_required),
                created_at_utc=row.created_at_utc,
                updated_at_utc=row.updated_at_utc,
            )

    def read_full_valuation_json(
        self,
        *,
        document_id: str,
    ) -> dict:
        self.initialize()
        with self._session_factory.create_session() as session:
            header = session.scalar(
                select(AlbaranValuationOrm).where(
                    AlbaranValuationOrm.document_id == document_id,
                )
            )
            if header is None:
                raise KeyError(
                    f"No hay valoración para document_id={document_id}"
                )
            lines = session.scalars(
                select(AlbaranLineValuationOrm)
                .where(AlbaranLineValuationOrm.valuation_id == header.id)
                .order_by(AlbaranLineValuationOrm.id)
            ).all()
            derived = session.scalars(
                select(ContratoLineDerivedOrm)
                .where(
                    ContratoLineDerivedOrm.created_by_valuation_id == header.id
                )
                .order_by(ContratoLineDerivedOrm.id)
            ).all()

            return {
                "header": self._header_to_dict(header),
                "lines": [self._line_to_dict(line) for line in lines],
                "derived_contract_lines": [
                    self._derived_to_dict(d) for d in derived
                ],
            }

    # ------------------------------------------------------------------ #
    # Escritura
    # ------------------------------------------------------------------ #
    def replace_valuation(
        self,
        *,
        header: ValuationHeaderRecord,
        lines: list[LineValuationRecord],
    ) -> PersistedValuation:
        self.initialize()
        now = datetime.now(timezone.utc).isoformat()

        with self._session_factory.create_session() as session:
            existing = session.scalar(
                select(AlbaranValuationOrm).where(
                    AlbaranValuationOrm.document_id == header.document_id,
                )
            )
            if existing is not None:
                session.delete(existing)
                session.flush()

            valuation_id = str(uuid.uuid4())
            valuation_orm = AlbaranValuationOrm(
                id=valuation_id,
                document_id=header.document_id,
                contrato_codigo=header.contrato_codigo,
                status=header.status,
                provider_ia=header.provider_ia,
                model_name=header.model_name,
                prompt_key=header.prompt_key,
                total_valorado=float(header.total_valorado),
                total_lines=int(header.total_lines),
                lines_matched_exact=int(header.lines_matched_exact),
                lines_matched_semantic=int(header.lines_matched_semantic),
                lines_matched_price_only=int(header.lines_matched_price_only),
                lines_unmatched=int(header.lines_unmatched),
                review_required=bool(header.review_required),
                review_reasons_json=json.dumps(
                    header.review_reasons, ensure_ascii=False,
                ),
                raw_ia_envelope_json=header.raw_ia_envelope_json,
                created_at_utc=now,
                updated_at_utc=now,
            )
            session.add(valuation_orm)
            session.flush()

            derived_by_record: dict[int, int] = {}
            for idx, line in enumerate(lines):
                if line.derived_contrato_line_record is None:
                    continue
                derived_orm = self._build_derived_orm(
                    valuation_id=valuation_id,
                    document_id=header.document_id,
                    record=line.derived_contrato_line_record,
                    created_at_utc=now,
                )
                session.add(derived_orm)
                session.flush()
                derived_by_record[idx] = derived_orm.id

            for idx, line in enumerate(lines):
                derived_id = derived_by_record.get(idx)
                line_orm = self._build_line_orm(
                    valuation_id=valuation_id,
                    line=line,
                    derived_contrato_line_id=derived_id,
                    created_at_utc=now,
                )
                session.add(line_orm)

            session.commit()

            logger.info(
                "[valuation-repo] persistida document_id=%s valuation_id=%s "
                "lines=%s derived=%s total=%.2f",
                header.document_id,
                valuation_id,
                len(lines),
                len(derived_by_record),
                header.total_valorado,
            )
            return PersistedValuation(
                valuation_id=valuation_id,
                document_id=header.document_id,
                status=header.status,
                total_valorado=float(header.total_valorado),
                total_lines=int(header.total_lines),
                review_required=bool(header.review_required),
            )

    # ------------------------------------------------------------------ #
    # Builders ORM
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_derived_orm(
        *,
        valuation_id: str,
        document_id: str,
        record: DerivedContratoLineRecord,
        created_at_utc: str,
    ) -> ContratoLineDerivedOrm:
        return ContratoLineDerivedOrm(
            created_by_valuation_id=valuation_id,
            source_document_id=document_id,
            codigo_contrato=record.codigo_contrato,
            codigo_producto=record.codigo_producto,
            descripcion_linea=record.descripcion_linea,
            unidad_medida=record.unidad_medida,
            precio_unitario=record.precio_unitario,
            codigo_partida=record.codigo_partida,
            origen=record.origen,
            created_at_utc=created_at_utc,
        )

    @staticmethod
    def _build_line_orm(
        *,
        valuation_id: str,
        line: LineValuationRecord,
        derived_contrato_line_id: int | None,
        created_at_utc: str,
    ) -> AlbaranLineValuationOrm:
        return AlbaranLineValuationOrm(
            valuation_id=valuation_id,
            merge_line_id=line.merge_line_id,
            matched_contrato_line_id=line.matched_contrato_line_id,
            derived_contrato_line_id=derived_contrato_line_id,
            precio_unitario_contrato_db=line.precio_unitario_contrato_db,
            precio_unitario_pdf_inferido=line.precio_unitario_pdf_inferido,
            precio_unitario_final=line.precio_unitario_final,
            precio_unitario_source=line.precio_unitario_source,
            precio_unitario_agreement=line.precio_unitario_agreement,
            unidad_albaran=line.unidad_albaran,
            unidad_contrato=line.unidad_contrato,
            unidad_categoria=line.unidad_categoria,
            unidad_category_match=line.unidad_category_match,
            cantidad_albaran=line.cantidad_albaran,
            cantidad_convertida=line.cantidad_convertida,
            factor_conversion=line.factor_conversion,
            importe_calculado=line.importe_calculado,
            importe_albaran_declarado=line.importe_albaran_declarado,
            importe_source=line.importe_source,
            codigo_partida_albaran=line.codigo_partida_albaran,
            codigo_partida_final=line.codigo_partida_final,
            partida_action=line.partida_action,
            match_confidence_pct=float(line.match_confidence_pct),
            match_method=line.match_method,
            review_required=bool(line.review_required),
            review_reasons_json=json.dumps(
                line.review_reasons, ensure_ascii=False,
            ),
            ia_reasoning=line.ia_reasoning,
            created_at_utc=created_at_utc,
            # ---------------------------------------------------------
            # Campos nuevos sub-tanda 2C. modifiers_applied se
            # serializa como JSON string (null si es None, que es el
            # caso normal mientras el LLM no devuelva la lista).
            # ---------------------------------------------------------
            rol_linea=line.rol_linea,
            ref_linea_base_merge_id=line.ref_linea_base_merge_id,
            tarifa_pdf_encontrada=line.tarifa_pdf_encontrada,
            modifiers_applied_json=(
                json.dumps(line.modifiers_applied, ensure_ascii=False)
                if line.modifiers_applied is not None
                else None
            ),
        )

    # ------------------------------------------------------------------ #
    # To dict
    # ------------------------------------------------------------------ #
    @staticmethod
    def _header_to_dict(header: AlbaranValuationOrm) -> dict[str, Any]:
        return {
            "valuation_id": header.id,
            "document_id": header.document_id,
            "contrato_codigo": header.contrato_codigo,
            "status": header.status,
            "provider_ia": header.provider_ia,
            "model_name": header.model_name,
            "prompt_key": header.prompt_key,
            "total_valorado": float(header.total_valorado or 0.0),
            "total_lines": int(header.total_lines or 0),
            "lines_matched_exact": int(header.lines_matched_exact or 0),
            "lines_matched_semantic": int(header.lines_matched_semantic or 0),
            "lines_matched_price_only": int(
                header.lines_matched_price_only or 0
            ),
            "lines_unmatched": int(header.lines_unmatched or 0),
            "review_required": bool(header.review_required),
            "review_reasons": json.loads(header.review_reasons_json or "[]"),
            "created_at_utc": header.created_at_utc,
            "updated_at_utc": header.updated_at_utc,
        }

    @staticmethod
    def _line_to_dict(line: AlbaranLineValuationOrm) -> dict[str, Any]:
        return {
            "id": line.id,
            "valuation_id": line.valuation_id,
            "merge_line_id": line.merge_line_id,
            "matched_contrato_line_id": line.matched_contrato_line_id,
            "derived_contrato_line_id": line.derived_contrato_line_id,
            "precio_unitario_contrato_db": line.precio_unitario_contrato_db,
            "precio_unitario_pdf_inferido": line.precio_unitario_pdf_inferido,
            "precio_unitario_final": line.precio_unitario_final,
            "precio_unitario_source": line.precio_unitario_source,
            "precio_unitario_agreement": line.precio_unitario_agreement,
            "unidad_albaran": line.unidad_albaran,
            "unidad_contrato": line.unidad_contrato,
            "unidad_categoria": line.unidad_categoria,
            "unidad_category_match": bool(line.unidad_category_match),
            "cantidad_albaran": line.cantidad_albaran,
            "cantidad_convertida": line.cantidad_convertida,
            "factor_conversion": line.factor_conversion,
            "importe_calculado": line.importe_calculado,
            "importe_albaran_declarado": line.importe_albaran_declarado,
            "importe_source": line.importe_source,
            "codigo_partida_albaran": line.codigo_partida_albaran,
            "codigo_partida_final": line.codigo_partida_final,
            "partida_action": line.partida_action,
            "match_confidence_pct": float(line.match_confidence_pct or 0.0),
            "match_method": line.match_method,
            "review_required": bool(line.review_required),
            "review_reasons": json.loads(line.review_reasons_json or "[]"),
            "ia_reasoning": line.ia_reasoning,
            "created_at_utc": line.created_at_utc,
            # Sub-tanda 2C: exponer los campos nuevos en la respuesta
            # HTTP GET /v1/albaranes/{id}/valuation. Incluye el parse
            # del JSON de modifiers_applied (o null).
            "rol_linea": line.rol_linea,
            "ref_linea_base_merge_id": line.ref_linea_base_merge_id,
            "tarifa_pdf_encontrada": (
                bool(line.tarifa_pdf_encontrada)
                if line.tarifa_pdf_encontrada is not None
                else None
            ),
            "modifiers_applied": (
                json.loads(line.modifiers_applied_json)
                if line.modifiers_applied_json
                else None
            ),
        }

    @staticmethod
    def _derived_to_dict(d: ContratoLineDerivedOrm) -> dict[str, Any]:
        return {
            "id": d.id,
            "created_by_valuation_id": d.created_by_valuation_id,
            "source_document_id": d.source_document_id,
            "codigo_contrato": d.codigo_contrato,
            "codigo_producto": d.codigo_producto,
            "descripcion_linea": d.descripcion_linea,
            "unidad_medida": d.unidad_medida,
            "precio_unitario": d.precio_unitario,
            "codigo_partida": d.codigo_partida,
            "origen": d.origen,
            "created_at_utc": d.created_at_utc,
        }
