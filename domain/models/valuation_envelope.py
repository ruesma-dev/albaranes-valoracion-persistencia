# domain/models/valuation_envelope.py
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from domain.models.contexto_linea import ContextoLinea


class _StrictModel(BaseModel):
    # Aceptamos extra en el envelope del svc 5 para no romper si
    # añaden campos nuevos (ej. un nuevo proveedor como bloque top).
    model_config = ConfigDict(extra="ignore")


class ValuationEnvelopeMeta(_StrictModel):
    document_id: str
    codigo_contrato: Optional[str] = None
    pdf_relative_path: Optional[str] = None
    pdf_filename: Optional[str] = None
    pdf_sha256: Optional[str] = None
    prompt_key: Optional[str] = None
    schema_name: Optional[str] = Field(default=None, alias="schema")
    primary_provider: Optional[str] = None
    model: Optional[str] = None
    processed_at_utc: Optional[str] = None
    service: Optional[str] = None
    service_version: Optional[str] = None
    providers_used: List[str] = Field(default_factory=list)


class LineValuationDto(_StrictModel):
    """Réplica del schema de salida de la IA (ver svc 5)."""

    merge_line_id: int
    match_method: Literal["exact_concept", "semantic", "price_only", "no_match"]
    matched_contrato_line_id: Optional[int] = None
    match_confidence_pct: float = 0.0
    unidad_categoria_albaran: str = "unknown"
    unidad_category_match: bool = False
    precio_unitario_contrato_db: Optional[float] = None
    precio_unitario_pdf_inferido: Optional[float] = None
    pdf_inference_reasoning: Optional[str] = None
    razon_corta: str = ""


class DocumentoValoracionDto(_StrictModel):
    lineas: List[LineValuationDto]


class AlbaranLineContextDto(_StrictModel):
    """Una línea del contexto que el svc 5 nos devuelve bajo ``context``.

    Permite que el svc 6 conozca los datos originales del albarán
    (unidad, partida, cantidad) sin necesidad de re-leer la BBDD.
    """

    merge_line_id: int
    line_index: int
    codigo: Optional[str] = None
    descripcion: Optional[str] = None
    unidad_medida: Optional[str] = None
    unidad_categoria: str = "unknown"
    cantidad: Optional[float] = None
    precio_unitario_albaran: Optional[float] = None
    importe_albaran: Optional[float] = None
    codigo_partida_albaran: Optional[str] = None
    # -----------------------------------------------------------------
    # Contexto estructural de la línea (familia hormigón / combustible /
    # alquiler_maquinaria / otro). Llega del svc5 ya deserializado como
    # dict (convertido desde ContextoLinea con model_dump). Pydantic
    # lo vuelve a construir como ContextoLinea al deserializar el
    # envelope.
    #
    # Null si la línea no es de familia compleja o si el albarán es
    # anterior a la sub-tanda 2A.
    # -----------------------------------------------------------------
    contexto_linea: Optional[ContextoLinea] = None


class ContratoLineContextDto(_StrictModel):
    contrato_line_id: int
    codigo_contrato: str
    codigo_producto: Optional[str] = None
    descripcion: Optional[str] = None
    unidad_medida: Optional[str] = None
    unidad_categoria: str = "unknown"
    precio_unitario: Optional[float] = None
    codigo_partida: Optional[str] = None


class ValuationContextDto(_StrictModel):
    lineas_albaran: List[AlbaranLineContextDto] = Field(default_factory=list)
    lineas_contrato: List[ContratoLineContextDto] = Field(default_factory=list)


class ValuationEnvelope(_StrictModel):
    status: Literal["ok", "no_contract"]
    meta: ValuationEnvelopeMeta
    data: DocumentoValoracionDto
    context: ValuationContextDto
    debug: Optional[dict] = None
