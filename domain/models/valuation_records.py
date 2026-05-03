# domain/models/valuation_records.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

PrecioUnitarioSource = Literal[
    "contract_line_match",
    "pdf_inference",
    "both_agreed",
    "albaran_declared",
    "albaran_calculated",
    "none",
]

PrecioUnitarioAgreement = Literal[
    "match", "mismatch", "only_1a", "only_1b", "neither",
]

MatchMethod = Literal[
    "exact_concept", "semantic", "price_only", "no_match",
]

ImporteSource = Literal["declared_albaran", "calculated", "none"]

PartidaAction = Literal[
    "existing_matched",
    "new_line_created",
    "alm_new_line_created",
    "no_action",
    "inherited_from_base_line",
]

ValuationStatus = Literal[
    "pending", "running", "ok", "failed", "no_contract", "partial",
]


@dataclass
class DerivedContratoLineRecord:
    """Línea derivada creada por el proceso de valoración.

    Se persiste en ``contrato_lines_derived`` y se referencia desde
    ``albaran_line_valuations.derived_contrato_line_id``.
    """

    codigo_contrato: str
    codigo_producto: str | None
    descripcion_linea: str | None
    unidad_medida: str | None
    precio_unitario: float | None
    codigo_partida: str | None
    origen: Literal["missing_partida", "alm_acopio"]


@dataclass
class LineValuationRecord:
    """Resultado final a persistir en albaran_line_valuations.

    V3 (sub-tanda 2D): soporte para LÍNEAS SINTÉTICAS.
    ``merge_line_id`` pasa a ser Optional. Cuando es None, la línea es
    un modificador sintético que cuelga de la línea base apuntada por
    ``parent_merge_line_id``.
    """

    # Para from_albaran: el id de la línea del albarán. Para sintéticas: None.
    merge_line_id: int | None
    matched_contrato_line_id: int | None
    derived_contrato_line_record: DerivedContratoLineRecord | None

    precio_unitario_contrato_db: float | None
    precio_unitario_pdf_inferido: float | None
    precio_unitario_final: float | None
    precio_unitario_source: PrecioUnitarioSource
    precio_unitario_agreement: PrecioUnitarioAgreement

    unidad_albaran: str | None
    unidad_contrato: str | None
    unidad_categoria: str
    unidad_category_match: bool

    cantidad_albaran: float | None
    cantidad_convertida: float | None
    factor_conversion: float | None

    importe_calculado: float | None
    importe_albaran_declarado: float | None
    importe_source: ImporteSource

    codigo_partida_albaran: str | None
    codigo_partida_final: str | None
    partida_action: PartidaAction

    match_confidence_pct: float
    match_method: MatchMethod
    review_required: bool
    review_reasons: list[str] = field(default_factory=list)
    ia_reasoning: str | None = None

    # ------------------------------------------------------------------
    # Campos nuevos de la sub-tanda 2C. Derivados del ``contexto_linea``
    # que llega del svc5 dentro del envelope.
    #
    # rol_linea:
    #   Copia del ``contexto_linea.rol_linea`` de la línea. None si la
    #   línea no tiene contexto_linea (producto simple).
    #   Valores esperados: 'base' | 'extra_tiempo' | 'transporte' |
    #   'recargo_horario' | 'desplazamiento' | 'operario' | 'otro'.
    #
    # ref_linea_base_merge_id:
    #   Solo para líneas complementarias (rol_linea distinto de 'base').
    #   Traducción de ``contexto_linea.ref_linea_base`` (que es un
    #   line_index del OCR) al merge_line_id real de la línea base en
    #   ``albaran_lines_merge``. Usado por el partida_matcher para
    #   heredar la partida.
    #   None si es base o si no encontramos la base referenciada.
    #
    # tarifa_pdf_encontrada:
    #   True si el LLM encontró tarifa completa en el PDF del contrato
    #   (regla Forma A o B). False si la línea tiene contexto_linea y
    #   no se encontró (Forma C: modifier_not_in_contract o
    #   complementario sin tarifa). None si la línea no tiene
    #   contexto_linea (no aplica).
    #   Derivado en el builder a partir de precio_unitario_pdf_inferido.
    #
    # modifiers_applied:
    #   Lista de dicts con los modificadores tarifados en el PDF.
    #   RESERVADO para uso futuro — ahora mismo el LLM no devuelve
    #   esta lista estructurada, así que queda siempre None. Si más
    #   adelante se enriquece el schema del svc5 para que el LLM
    #   devuelva los modificadores aplicados, este campo se rellenará.
    # ------------------------------------------------------------------
    rol_linea: str | None = None
    ref_linea_base_merge_id: int | None = None
    tarifa_pdf_encontrada: bool | None = None
    modifiers_applied: list[dict] | None = None

    # ------------------------------------------------------------------
    # Campos nuevos de la sub-tanda 2D. Soporte para LÍNEAS SINTÉTICAS
    # (modificadores implícitos de hormigón identificados por el
    # valorador).
    #
    # line_kind:
    #   'from_albaran' (default): línea normal, procede del albarán.
    #   'synthetic_modifier': línea sintética generada por el valorador
    #   para representar un modificador que no aparece como línea en el
    #   albarán pero sí tarifado en el contrato (año, consistencia,
    #   árido, aditivo, residuos, tiempo).
    #
    # parent_merge_line_id:
    #   Solo para sintéticas: el merge_line_id de la línea base de la
    #   que cuelga este modificador. Permite agrupar en la UI.
    #   Null para 'from_albaran'.
    #
    # modifier_source / modifier_reason:
    #   Solo para sintéticas. ``modifier_source`` es una etiqueta
    #   canónica ('codigo_producto', 'observaciones', 'year_contract',
    #   'year_albaran', 'tiempo_exceso', 'gestion_residuos', 'otro').
    #   ``modifier_reason`` es texto libre para auditoría humana.
    #
    # descripcion_linea:
    #   Para sintéticas: texto que verá el revisor ("INCREMENTO POR
    #   CONSISTENCIA FLUIDA"). Para from_albaran es null (la descripción
    #   ya viene de la línea del albarán).
    # ------------------------------------------------------------------
    line_kind: str = "from_albaran"
    parent_merge_line_id: int | None = None
    modifier_source: str | None = None
    modifier_reason: str | None = None
    descripcion_linea: str | None = None

    # ------------------------------------------------------------------
    # Tanda descuento — abr 2026
    #
    # descuento_albaran_aplicado:
    #   Porcentaje de descuento (0-100) que se aplicó al precio del
    #   contrato para calcular ``importe_calculado``. Persistido para
    #   auditoría: si en el futuro alguien pregunta "¿por qué este
    #   importe vale X y no Y?", se puede inspeccionar este campo y
    #   reconstruir el cálculo.
    #
    #   Política:
    #     - Líneas 'from_albaran' (rol_linea base o complementaria):
    #       copia el ``descuento_albaran`` de la línea del albarán.
    #       None si la línea del albarán no tiene descuento.
    #     - Líneas 'synthetic_modifier' (M1-M7): hereda el descuento
    #       de la línea base padre (decisión de negocio Ruesma:
    #       las sintéticas heredan).
    #     - 0 o None significan "no aplicar descuento" (resultado
    #       idéntico en la fórmula).
    # ------------------------------------------------------------------
    descuento_albaran_aplicado: float | None = None


@dataclass
class ValuationHeaderRecord:
    """Resultado final a persistir en albaran_valuations."""

    document_id: str
    contrato_codigo: str | None
    status: ValuationStatus
    provider_ia: str | None
    model_name: str | None
    prompt_key: str | None
    total_valorado: float
    total_lines: int
    lines_matched_exact: int
    lines_matched_semantic: int
    lines_matched_price_only: int
    lines_unmatched: int
    review_required: bool
    review_reasons: list[str] = field(default_factory=list)
    raw_ia_envelope_json: str | None = None
