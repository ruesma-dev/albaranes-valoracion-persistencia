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
from infrastructure.database.schema_contribution import (
    get_ddl_statements,
    get_external_table_dependencies,
    get_owned_table_names,
)
from infrastructure.database.session_factory import SessionFactory

logger = logging.getLogger(__name__)


class SqlAlchemyValuationRepository(ValuationRepository):
    """Repositorio con inicialización PEREZOSA y DDL por sentencia.

    El DDL ya NO vive en este archivo. Se carga del módulo
    ``infrastructure/database/schema_contribution.py``, que es la
    fuente única de verdad y el contrato público que el orquestador
    (servicio 7) consume para crear las tablas. Así, cuando añadimos
    una columna, basta con tocar ``schema_contribution.py``: el repo
    de este servicio y el sv7 leen del mismo sitio.

    Cada sentencia DDL se ejecuta en su propia transacción (autocommit)
    para que un fallo puntual no invalide el resto. Al final, se
    comprueba que todas las tablas declaradas como propias existen en
    la BBDD; si no, se levanta un error claro.
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

        # Prerequisito: tablas externas (servicio 3) presentes — porque
        # nuestras FKs apuntan a ellas.
        missing_external = self._missing_tables(
            get_external_table_dependencies()
        )
        if missing_external:
            raise RuntimeError(
                "No se pueden crear las tablas del servicio 6 porque "
                f"faltan tablas externas (servicio 3): {missing_external}. "
                "Arranca el servicio 3 (o el orquestador sv7) contra la "
                "misma base de datos y reintenta."
            )

        ddl_statements = get_ddl_statements()
        engine = self._session_factory.engine
        logger.info(
            "[valuation-repo] Ejecutando %s sentencias DDL "
            "(autocommit) desde schema_contribution ...",
            len(ddl_statements),
        )
        # AUTOCOMMIT: cada sentencia DDL se confirma por separado. Así un
        # fallo puntual no invalida la transacción entera, y además
        # hacemos imposible "la transacción silenciosa abortada".
        autocommit_engine = engine.execution_options(
            isolation_level="AUTOCOMMIT",
        )
        for idx, (label, ddl) in enumerate(ddl_statements, start=1):
            try:
                with autocommit_engine.connect() as conn:
                    conn.execute(text(ddl))
                logger.info(
                    "[valuation-repo]   [%2s/%2s] OK %s",
                    idx, len(ddl_statements), label,
                )
            except Exception as exc:
                logger.error(
                    "[valuation-repo]   [%2s/%2s] FALLO %s -> %s",
                    idx, len(ddl_statements), label, exc,
                )
                raise

        # Verificación: todas las tablas propias tienen que existir.
        still_missing = self._missing_tables(get_owned_table_names())
        if still_missing:
            raise RuntimeError(
                "Tras ejecutar la DDL, siguen faltando tablas: "
                f"{still_missing}. Esto no debería pasar. Revisa los "
                "permisos del usuario de BBDD y los logs anteriores."
            )

        self._initialized = True
        logger.info(
            "[valuation-repo] Inicialización completa y VERIFICADA. "
            "Tablas propias: %s",
            ", ".join(get_owned_table_names()),
        )

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
            # Sub-tanda 2C: contexto de línea.
            rol_linea=line.rol_linea,
            ref_linea_base_merge_id=line.ref_linea_base_merge_id,
            tarifa_pdf_encontrada=line.tarifa_pdf_encontrada,
            modifiers_applied_json=(
                json.dumps(line.modifiers_applied, ensure_ascii=False)
                if line.modifiers_applied is not None
                else None
            ),
            # Sub-tanda 2D: líneas sintéticas.
            line_kind=line.line_kind,
            parent_merge_line_id=line.parent_merge_line_id,
            modifier_source=line.modifier_source,
            modifier_reason=line.modifier_reason,
            descripcion_linea=line.descripcion_linea,
            # Tanda descuento — abr 2026
            descuento_albaran_aplicado=line.descuento_albaran_aplicado,
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
            # Sub-tanda 2C: contexto estructural.
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
            # Sub-tanda 2D: líneas sintéticas.
            "line_kind": line.line_kind,
            "parent_merge_line_id": line.parent_merge_line_id,
            "modifier_source": line.modifier_source,
            "modifier_reason": line.modifier_reason,
            "descripcion_linea": line.descripcion_linea,
            # Tanda descuento — abr 2026
            "descuento_albaran_aplicado": line.descuento_albaran_aplicado,
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
