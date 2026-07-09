# application/services/residuos_container_calc.py
"""Cálculo determinista de la valoración por CONTENEDORES (residuos).

En residuos la valoración va por contenedor: el contrato tarifa un
contenedor de X m³, y la cantidad valorada es el NÚMERO de contenedores,
no los m³ del albarán. Los m³ (y el peso en Tn) se conservan como
metadato de la línea para trabajos posteriores.

Regla de negocio (a confirmar/afinar con Ruesma):

    num_contenedores = ceil(volumen_m3_albaran / m3_por_contenedor_contrato)

donde ``m3_por_contenedor_contrato`` se lee de la descripción de la línea
de contrato casada (p.ej. "CONTENEDOR RCD 6 M3" → 6). Si no hay m³ en el
albarán o no se detecta el tamaño de contenedor, se devuelve
``num_contenedores=None`` y una razón para que la línea vaya a revisión
(NUNCA se inventa un número).

Esta función es PURA (sin I/O), fácil de testear.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# Captura la cifra de m³ en textos tipo "6 m3", "6m³", "7 M3", "6,5 mc".
# Deliberadamente permisiva; es un punto a afinar con descripciones reales.
_M3_REGEX = re.compile(r"(\d+(?:[.,]\d+)?)\s*m\s*[3³c]", re.IGNORECASE)


@dataclass(frozen=True)
class ResultadoContenedores:
    num_contenedores: Optional[int]
    volumen_m3: Optional[float]
    contenedor_m3: Optional[float]
    reasons: list[str] = field(default_factory=list)


def _parse_m3(texto: Optional[str]) -> Optional[float]:
    if not texto:
        return None
    m = _M3_REGEX.search(str(texto))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _tamano_contenedor(contrato_line: Any) -> Optional[float]:
    """m³ por contenedor, leídos de la línea de contrato casada."""
    if contrato_line is None:
        return None
    # Prioridad: descripción (p.ej. "CONTENEDOR RCD 6 M3"), luego unidad.
    return (
        _parse_m3(getattr(contrato_line, "descripcion", None))
        or _parse_m3(getattr(contrato_line, "unidad_medida", None))
    )


def _elegir_contenedor(contrato_lines: Any, m3: float) -> Optional[float]:
    """m³ por contenedor eligiendo de las líneas de contrato (determinista).

    Regla de negocio: si hay un contenedor cuyo tamaño COINCIDE con los m³
    del albarán, usa ese; si no, y solo hay un tamaño de contenedor, usa
    ese (por defecto). Con varios tamaños y ninguno exacto → None (se deja
    a la IA / a la línea casada).
    """
    if not contrato_lines:
        return None
    contenedores: list[float] = []
    for l in contrato_lines:
        desc = getattr(l, "descripcion", None) or getattr(
            l, "descripcion_linea", None
        )
        if not desc or "contenedor" not in str(desc).lower():
            continue
        size = _parse_m3(desc)
        if size and size > 0:
            contenedores.append(size)
    if not contenedores:
        return None
    for size in contenedores:
        if abs(size - m3) < 0.01:      # tamaño exacto -> ese
            return size
    unicos = set(contenedores)
    if len(unicos) == 1:               # un solo tipo -> por defecto
        return next(iter(unicos))
    return None                        # varios tamaños, ninguno exacto


def calcular_contenedores_residuos(
    *,
    contexto_linea: Any,
    contrato_line: Any = None,
    contrato_lines: Any = None,
    contenedor_m3_ia: Optional[float] = None,
) -> ResultadoContenedores:
    """Devuelve el nº de contenedores a valorar para una línea de residuos."""
    m3 = getattr(contexto_linea, "volumen_m3", None)
    try:
        m3 = float(m3) if m3 is not None else None
    except (TypeError, ValueError):
        m3 = None

    if m3 is None or m3 <= 0:
        return ResultadoContenedores(
            num_contenedores=None,
            volumen_m3=m3,
            contenedor_m3=None,
            reasons=["residuos_sin_volumen_m3"],
        )

    # Prioridad: el tamaño que fijó la IA leyendo el contrato (regla
    # exacto/por-defecto). Fallback: parseo de la descripción de la línea
    # de contrato casada (por si la IA no lo emite).
    # Prioridad del tamaño de contenedor:
    #   1) escaneo DETERMINISTA de las líneas de contrato (regla exacto/
    #      por-defecto) -> lo más fiable si están estructuradas;
    #   2) el que fijó la IA leyendo el PDF (contenedor_m3_ia);
    #   3) regex sobre la descripción de la línea de contrato casada.
    tam = _elegir_contenedor(contrato_lines, m3)
    if (tam is None or tam <= 0) and contenedor_m3_ia is not None:
        try:
            tam = float(contenedor_m3_ia)
        except (TypeError, ValueError):
            tam = None
    if tam is None or tam <= 0:
        tam = _tamano_contenedor(contrato_line)
    if tam is None or tam <= 0:
        return ResultadoContenedores(
            num_contenedores=None,
            volumen_m3=m3,
            contenedor_m3=None,
            reasons=["residuos_tamano_contenedor_no_detectado"],
        )

    num = int(math.ceil(m3 / tam))
    return ResultadoContenedores(
        num_contenedores=num,
        volumen_m3=m3,
        contenedor_m3=tam,
        reasons=[
            f"residuos_contenedores={num} "
            f"(m3={m3:g} / contenedor={tam:g} m3)"
        ],
    )
