# application/services/partida_matcher.py
from __future__ import annotations

import logging
from dataclasses import dataclass

from domain.models.valuation_envelope import (
    ContratoLineContextDto,
    LineValuationDto,
)
from domain.models.valuation_records import (
    DerivedContratoLineRecord,
    PartidaAction,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PartidaMatchResult:
    partida_action: PartidaAction
    matched_contrato_line_id: int | None
    derived_line: DerivedContratoLineRecord | None
    codigo_partida_final: str | None
    reasons: list[str]


class PartidaMatcher:
    """Dado un producto ya identificado por la IA, busca entre las
    líneas del contrato la que tenga la MISMA partida que el albarán.

    Reglas:
      - Si ``codigo_partida_albaran`` == ``ALM`` (acopio/almacén):
        codigo_partida_final = None y se CREA una línea derivada.
      - Si existe una línea de contrato con mismo ``codigo_producto`` y
        misma ``codigo_partida``: ``existing_matched`` (se prefiere esa
        frente a la que devolvió la IA, que podía ser de otra partida).
      - Si no existe: ``new_line_created`` y se CREA una línea derivada
        con el ``codigo_partida_albaran`` asignado.
    """

    def __init__(self, *, alm_codigo_partida: str) -> None:
        self._alm_literal = (alm_codigo_partida or "ALM").strip().upper()

    def match(
        self,
        *,
        line: LineValuationDto,
        codigo_partida_albaran: str | None,
        precio_unitario_final: float | None,
        unidad_albaran: str | None,
        contrato_lines: list[ContratoLineContextDto],
        albaran_descripcion: str | None,
        albaran_codigo_producto: str | None,
    ) -> PartidaMatchResult:
        partida_norm = self._normalize(codigo_partida_albaran)

        # Caso ALM: siempre se crea línea derivada con partida None.
        if partida_norm == self._alm_literal:
            return PartidaMatchResult(
                partida_action="alm_new_line_created",
                matched_contrato_line_id=None,
                derived_line=self._build_derived(
                    line=line,
                    contrato_lines=contrato_lines,
                    codigo_partida_final=None,
                    origen="alm_acopio",
                    precio_unitario_final=precio_unitario_final,
                    unidad_albaran=unidad_albaran,
                    albaran_descripcion=albaran_descripcion,
                    albaran_codigo_producto=albaran_codigo_producto,
                ),
                codigo_partida_final=None,
                reasons=["alm_acopio_generates_derived_line"],
            )

        # Si IA no casó ninguna línea de contrato y no hay partida del
        # albarán, no podemos hacer matching: no_action.
        if line.matched_contrato_line_id is None and partida_norm is None:
            return PartidaMatchResult(
                partida_action="no_action",
                matched_contrato_line_id=None,
                derived_line=None,
                codigo_partida_final=None,
                reasons=["no_ia_match_and_no_partida"],
            )

        ia_line = self._find_by_id(
            contrato_lines, line.matched_contrato_line_id,
        )

        # ------------------------------------------------------------- #
        # CASO PRINCIPAL — RESPETAR EL MATCH SEMÁNTICO DE LA IA.
        #
        # La IA (sv5) ya elige la línea de contrato más parecida mirando
        # descripción, partida, precio y unidad. NO debemos re-apuntar por
        # ``codigo_producto`` + ``codigo_partida``: en Sigrid el
        # ``codigo_producto`` suele ser genérico (p.ej. "MA9999" para
        # TODAS las líneas de la obra), de modo que (producto, partida) no
        # distingue el hormigón de una línea de "CANCELACIÓN" o de un
        # recargo, y acababa cogiendo la PRIMERA de la partida.
        # ------------------------------------------------------------- #
        if ia_line is not None:
            ia_partida = self._normalize(ia_line.codigo_partida)

            # (a) La IA casó una línea YA en la partida del albarán (o el
            #     albarán no trae partida). Confiamos en la IA tal cual.
            if partida_norm is None or ia_partida == partida_norm:
                return PartidaMatchResult(
                    partida_action="existing_matched",
                    matched_contrato_line_id=ia_line.contrato_line_id,
                    derived_line=None,
                    codigo_partida_final=ia_partida or partida_norm,
                    reasons=["ia_match_trusted"],
                )

            # (b) La IA casó una línea en OTRA partida. Re-apuntamos a la
            #     línea con la MISMA descripción en la partida del albarán
            #     (el contrato replica el mismo producto por partida con
            #     idéntica descripción). NO por codigo_producto.
            repointed = self._find_same_description_in_partida(
                contrato_lines=contrato_lines,
                descripcion=ia_line.descripcion,
                codigo_partida=partida_norm,
            )
            if repointed is not None:
                return PartidaMatchResult(
                    partida_action="existing_matched",
                    matched_contrato_line_id=repointed.contrato_line_id,
                    derived_line=None,
                    codigo_partida_final=partida_norm,
                    reasons=["ia_match_repointed_to_partida_by_description"],
                )

            # (c) No existe esa descripción en la partida del albarán →
            #     línea derivada (con la descripción/precio de la línea IA)
            #     en la partida del albarán.
            return PartidaMatchResult(
                partida_action="new_line_created",
                matched_contrato_line_id=None,
                derived_line=self._build_derived(
                    line=line,
                    contrato_lines=contrato_lines,
                    codigo_partida_final=partida_norm,
                    origen="missing_partida",
                    precio_unitario_final=precio_unitario_final,
                    unidad_albaran=unidad_albaran,
                    albaran_descripcion=albaran_descripcion,
                    albaran_codigo_producto=albaran_codigo_producto,
                ),
                codigo_partida_final=partida_norm,
                reasons=["ia_match_partida_missing_derived"],
            )

        # ------------------------------------------------------------- #
        # Sin match de la IA pero con partida del albarán. NO adivinamos
        # por codigo_producto genérico (eso era lo que casaba la base con
        # la línea de cancelación). Creamos una línea derivada en la
        # partida del albarán y se marca para revisión aguas arriba.
        # ------------------------------------------------------------- #
        return PartidaMatchResult(
            partida_action="new_line_created",
            matched_contrato_line_id=None,
            derived_line=self._build_derived(
                line=line,
                contrato_lines=contrato_lines,
                codigo_partida_final=partida_norm,
                origen="no_ia_match",
                precio_unitario_final=precio_unitario_final,
                unidad_albaran=unidad_albaran,
                albaran_descripcion=albaran_descripcion,
                albaran_codigo_producto=albaran_codigo_producto,
            ),
            codigo_partida_final=partida_norm,
            reasons=["no_ia_match_derived"],
        )

    def resolve_partida_for_complementaria(
        self,
        *,
        codigo_partida_base: str | None,
    ) -> PartidaMatchResult:
        """Partida heredada para una línea complementaria (sub-tanda 2C).

        Una línea con ``rol_linea != 'base'`` que apunta a otra base
        (``ref_linea_base_merge_id``) hereda su partida SIN hacer
        matching en el contrato ni crear línea derivada. La base ya se
        resolvió en la primera pasada del builder y se trae aquí.

        Nota: no se intenta casar contra ``albaran_contrato_lines_merge``
        aunque exista una línea con (producto, partida) coincidente.
        El motivo: una línea "exceso de tiempo" no es un producto en el
        contrato — es un modificador de la línea base. Si existe tarifa
        en el PDF la captura el LLM (precio_unitario_pdf_inferido). Si
        no existe, se marca review_required por "modifier_not_in_contract"
        (responsabilidad del builder, no del matcher).

        Devuelve siempre ``partida_action='inherited_from_base_line'``
        con ``matched_contrato_line_id=None`` y sin línea derivada.
        """
        if codigo_partida_base is None:
            # La base no tenía partida (p. ej. base fue ALM/acopio, o
            # base sin resolver). La complementaria tampoco tiene.
            return PartidaMatchResult(
                partida_action="inherited_from_base_line",
                matched_contrato_line_id=None,
                derived_line=None,
                codigo_partida_final=None,
                reasons=["inherited_from_base_no_partida"],
            )
        return PartidaMatchResult(
            partida_action="inherited_from_base_line",
            matched_contrato_line_id=None,
            derived_line=None,
            codigo_partida_final=self._normalize(codigo_partida_base),
            reasons=["inherited_from_base_line"],
        )

    def resolve_partida_for_synthetic(
        self,
        *,
        codigo_partida_base: str | None,
    ) -> PartidaMatchResult:
        """Partida heredada para una línea sintética (sub-tanda 2D).

        Una línea sintética (``line_kind='synthetic_modifier'``) es un
        modificador implícito generado por el valorador, no aparece como
        línea en el albarán. Hereda la partida de la línea base (a la
        que apunta por ``parent_merge_line_id``) SIN hacer matching en
        contrato ni crear línea derivada.

        El ``partida_action`` devuelto es ``'inherited_from_base_line'``
        (mismo valor que para complementarias de 2C; semánticamente es
        el mismo concepto: hereda, no busca).

        Esta función es funcionalmente idéntica a
        ``resolve_partida_for_complementaria`` pero se deja como método
        separado para dejar claro en el código qué tipo de línea se
        está procesando y dejar margen a bifurcar en el futuro si la
        regla cambia para una u otra.
        """
        return self.resolve_partida_for_complementaria(
            codigo_partida_base=codigo_partida_base,
        )

    def _build_derived(
        self,
        *,
        line: LineValuationDto,
        contrato_lines: list[ContratoLineContextDto],
        codigo_partida_final: str | None,
        origen: str,
        precio_unitario_final: float | None,
        unidad_albaran: str | None,
        albaran_descripcion: str | None,
        albaran_codigo_producto: str | None,
    ) -> DerivedContratoLineRecord:
        ia_line = self._find_by_id(contrato_lines, line.matched_contrato_line_id)
        codigo_contrato = ""
        descripcion: str | None = albaran_descripcion
        unidad: str | None = unidad_albaran

        if ia_line is not None:
            codigo_contrato = ia_line.codigo_contrato
            descripcion = ia_line.descripcion or descripcion
            unidad = ia_line.unidad_medida or unidad
        elif contrato_lines:
            # Si no hay IA match, cogemos el código de contrato de la
            # primera línea (todas pertenecen al mismo contrato por
            # construcción).
            codigo_contrato = contrato_lines[0].codigo_contrato

        return DerivedContratoLineRecord(
            codigo_contrato=codigo_contrato,
            codigo_producto=albaran_codigo_producto,
            descripcion_linea=descripcion,
            unidad_medida=unidad,
            precio_unitario=precio_unitario_final,
            codigo_partida=codigo_partida_final,
            origen=origen,  # type: ignore[arg-type]
        )

    @staticmethod
    def _find_by_id(
        contrato_lines: list[ContratoLineContextDto],
        line_id: int | None,
    ) -> ContratoLineContextDto | None:
        if line_id is None:
            return None
        for cl in contrato_lines:
            if cl.contrato_line_id == line_id:
                return cl
        return None

    @staticmethod
    def _find_by_producto_partida(
        *,
        contrato_lines: list[ContratoLineContextDto],
        codigo_producto: str | None,
        codigo_partida: str | None,
    ) -> ContratoLineContextDto | None:
        if codigo_producto is None or codigo_partida is None:
            return None
        prod_norm = codigo_producto.strip().upper()
        part_norm = codigo_partida.strip().upper()
        for cl in contrato_lines:
            cp = (cl.codigo_producto or "").strip().upper()
            pp = (cl.codigo_partida or "").strip().upper()
            if cp == prod_norm and pp == part_norm:
                return cl
        return None

    @staticmethod
    def _find_same_description_in_partida(
        *,
        contrato_lines: list[ContratoLineContextDto],
        descripcion: str | None,
        codigo_partida: str | None,
    ) -> ContratoLineContextDto | None:
        """Busca, dentro de ``codigo_partida``, la línea cuya descripción
        coincide (normalizada) con ``descripcion``. Sirve para re-apuntar
        el match de la IA a la partida del albarán cuando el contrato
        replica el mismo producto por partidas con idéntica descripción,
        sin depender del ``codigo_producto`` (que suele ser genérico).
        """
        if not descripcion or codigo_partida is None:
            return None
        desc_norm = PartidaMatcher._normalize_desc(descripcion)
        part_norm = codigo_partida.strip().upper()
        if not desc_norm:
            return None
        for cl in contrato_lines:
            pp = (cl.codigo_partida or "").strip().upper()
            if pp != part_norm:
                continue
            if PartidaMatcher._normalize_desc(cl.descripcion) == desc_norm:
                return cl
        return None

    @staticmethod
    def _normalize_desc(value: str | None) -> str:
        """Normaliza descripción para comparar: mayúsculas, sin acentos,
        espacios colapsados.
        """
        if not value:
            return ""
        import unicodedata
        import re
        nfkd = unicodedata.normalize("NFKD", value)
        ascii_ = "".join(c for c in nfkd if not unicodedata.combining(c))
        return re.sub(r"\s+", " ", ascii_.upper()).strip()

    @staticmethod
    def _normalize(value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        return stripped.upper()
