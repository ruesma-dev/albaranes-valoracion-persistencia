# infrastructure/database/orm_valuation_models.py
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# =============================================================================
# STUBS de tablas del SERVICIO 3.
#
# Los registramos en este ``Base.metadata`` para que SQLAlchemy pueda
# resolver las FK de nuestras tablas nuevas (``albaran_valuations.document_id``
# → ``albaran_documents_merge.id``, etc.) SIN tener que importar el ORM
# del servicio 3 (son microservicios independientes y queremos
# desacoplarlos).
#
# Solo declaramos la primary key — es lo único que necesita el FK para
# resolverse. NO se crean en ``create_all`` porque en ``initialize()``
# pasamos explícitamente la lista ``tables=[...]`` con solo nuestras
# tablas nuevas. Si el servicio 3 evoluciona su schema (columnas
# nuevas), este archivo NO tiene que cambiar — los stubs solo modelan
# lo que nos interesa: la PK.
#
# Tipos coherentes con el ORM real del servicio 3:
#   - albaran_documents_merge.id      : String(36) UUID
#   - albaran_lines_merge.id          : Integer autoincrement
#   - albaran_contrato_lines_merge.id : Integer autoincrement
# =============================================================================

albaran_documents_merge_stub = Table(
    "albaran_documents_merge",
    Base.metadata,
    Column("id", String(36), primary_key=True),
)

albaran_lines_merge_stub = Table(
    "albaran_lines_merge",
    Base.metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
)

albaran_contrato_lines_merge_stub = Table(
    "albaran_contrato_lines_merge",
    Base.metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
)


# =============================================================================
# Tablas NUEVAS propias del servicio 6.
# =============================================================================

class AlbaranValuationOrm(Base):
    """Cabecera de valoración. 1:1 con albaran_documents_merge.id."""

    __tablename__ = "albaran_valuations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("albaran_documents_merge.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    contrato_codigo: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_ia: Mapped[str | None] = mapped_column(String(32))
    model_name: Mapped[str | None] = mapped_column(String(100))
    prompt_key: Mapped[str | None] = mapped_column(String(100))
    total_valorado: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0,
    )
    total_lines: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lines_matched_exact: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    lines_matched_semantic: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    lines_matched_price_only: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    lines_unmatched: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    review_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    review_reasons_json: Mapped[str | None] = mapped_column(Text)
    raw_ia_envelope_json: Mapped[str | None] = mapped_column(Text)
    created_at_utc: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at_utc: Mapped[str | None] = mapped_column(String(64))

    lines: Mapped[list["AlbaranLineValuationOrm"]] = relationship(
        back_populates="valuation",
        cascade="all, delete-orphan",
    )
    derived_contract_lines: Mapped[list["ContratoLineDerivedOrm"]] = relationship(
        back_populates="valuation",
        cascade="all, delete-orphan",
    )


class ContratoLineDerivedOrm(Base):
    """Línea de contrato creada por el proceso de valoración.

    Se crea cuando:
      - El albarán imputa a una partida que el contrato NO tiene para
        ese producto (``origen='missing_partida'``).
      - El albarán imputa a almacén/acopio (``origen='alm_acopio'``),
        en cuyo caso ``codigo_partida`` = NULL.
    """

    __tablename__ = "contrato_lines_derived"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    created_by_valuation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("albaran_valuations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_document_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True,
    )
    codigo_contrato: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
    )
    codigo_producto: Mapped[str | None] = mapped_column(String(64))
    descripcion_linea: Mapped[str | None] = mapped_column(Text)
    unidad_medida: Mapped[str | None] = mapped_column(String(32))
    precio_unitario: Mapped[float | None] = mapped_column(Float)
    codigo_partida: Mapped[str | None] = mapped_column(String(64))
    origen: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at_utc: Mapped[str] = mapped_column(String(64), nullable=False)

    valuation: Mapped[AlbaranValuationOrm] = relationship(
        back_populates="derived_contract_lines",
    )
    line_valuations: Mapped[list["AlbaranLineValuationOrm"]] = relationship(
        back_populates="derived_contrato_line",
    )

    __table_args__ = (
        Index(
            "ix_contrato_lines_derived_producto_partida",
            "codigo_contrato",
            "codigo_producto",
            "codigo_partida",
        ),
    )


class AlbaranLineValuationOrm(Base):
    """Resultado por línea de albarán. 1 fila por línea del merge."""

    __tablename__ = "albaran_line_valuations"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    valuation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("albaran_valuations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # -----------------------------------------------------------------
    # V3 / sub-tanda 2D: merge_line_id es ahora nullable.
    # Para líneas sintéticas (modificadores implícitos) el valorador no
    # tiene una línea del albarán concreta — por eso merge_line_id es
    # null y parent_merge_line_id apunta a la línea base.
    # -----------------------------------------------------------------
    merge_line_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("albaran_lines_merge.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    matched_contrato_line_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("albaran_contrato_lines_merge.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    derived_contrato_line_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("contrato_lines_derived.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    precio_unitario_contrato_db: Mapped[float | None] = mapped_column(Float)
    precio_unitario_pdf_inferido: Mapped[float | None] = mapped_column(Float)
    precio_unitario_final: Mapped[float | None] = mapped_column(Float)
    precio_unitario_source: Mapped[str] = mapped_column(
        String(32), nullable=False,
    )
    precio_unitario_agreement: Mapped[str] = mapped_column(
        String(32), nullable=False,
    )

    unidad_albaran: Mapped[str | None] = mapped_column(String(32))
    unidad_contrato: Mapped[str | None] = mapped_column(String(32))
    unidad_categoria: Mapped[str] = mapped_column(String(32), nullable=False)
    unidad_category_match: Mapped[bool] = mapped_column(Boolean, nullable=False)

    cantidad_albaran: Mapped[float | None] = mapped_column(Float)
    cantidad_convertida: Mapped[float | None] = mapped_column(Float)
    factor_conversion: Mapped[float | None] = mapped_column(Float)

    importe_calculado: Mapped[float | None] = mapped_column(Float)
    importe_albaran_declarado: Mapped[float | None] = mapped_column(Float)
    importe_source: Mapped[str] = mapped_column(String(32), nullable=False)

    codigo_partida_albaran: Mapped[str | None] = mapped_column(String(64))
    codigo_partida_final: Mapped[str | None] = mapped_column(String(64))
    partida_action: Mapped[str] = mapped_column(String(32), nullable=False)

    match_confidence_pct: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0,
    )
    match_method: Mapped[str] = mapped_column(String(32), nullable=False)
    review_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    review_reasons_json: Mapped[str | None] = mapped_column(Text)
    ia_reasoning: Mapped[str | None] = mapped_column(Text)
    created_at_utc: Mapped[str] = mapped_column(String(64), nullable=False)

    # -----------------------------------------------------------------
    # Campos nuevos de la sub-tanda 2C. Ver
    # domain/models/valuation_records.py para semántica detallada.
    #
    # - rol_linea: VARCHAR(32). 'base' | 'extra_tiempo' | 'transporte'
    #   | 'recargo_horario' | 'desplazamiento' | 'operario' | 'otro'.
    #   Null si la línea no es de familia compleja.
    # - ref_linea_base_merge_id: INTEGER. merge_line_id de la línea
    #   base asociada si esta es complementaria. Null si es base.
    # - tarifa_pdf_encontrada: BOOLEAN. True si el LLM encontró tarifa
    #   completa en el PDF. False si no la encontró (Forma C). Null si
    #   la línea no tiene contexto (no aplica).
    # - modifiers_applied_json: TEXT. JSON con lista de modificadores
    #   tarifados. Reservado para uso futuro (ahora siempre null).
    # -----------------------------------------------------------------
    rol_linea: Mapped[str | None] = mapped_column(String(32))
    ref_linea_base_merge_id: Mapped[int | None] = mapped_column(Integer)
    tarifa_pdf_encontrada: Mapped[bool | None] = mapped_column(Boolean)
    modifiers_applied_json: Mapped[str | None] = mapped_column(Text)

    # -----------------------------------------------------------------
    # Sub-tanda 2D: soporte para líneas sintéticas.
    # Ver domain/models/valuation_records.py para semántica.
    #
    # line_kind NOT NULL con default 'from_albaran' para compatibilidad
    # retroactiva: los records previos quedan marcados como normales.
    # -----------------------------------------------------------------
    line_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="from_albaran",
    )
    parent_merge_line_id: Mapped[int | None] = mapped_column(Integer, index=True)
    modifier_source: Mapped[str | None] = mapped_column(String(32))
    modifier_reason: Mapped[str | None] = mapped_column(Text)
    descripcion_linea: Mapped[str | None] = mapped_column(Text)

    valuation: Mapped[AlbaranValuationOrm] = relationship(
        back_populates="lines",
    )
    derived_contrato_line: Mapped[ContratoLineDerivedOrm | None] = relationship(
        back_populates="line_valuations",
    )

    __table_args__ = (
        UniqueConstraint(
            "valuation_id", "merge_line_id",
            name="uq_albaran_line_valuations_val_line",
        ),
    )
