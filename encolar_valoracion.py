# encolar_valoracion.py
"""Utilidad de prueba: encola un mensaje en q-valoracion a mano.

Normalmente q-valoracion la alimenta sv3 (auto-selección de contrato único) o
sv4 (el revisor). Este script sirve para re-lanzar la valoración de un
``document_id`` concreto sin repetir toda la cadena.

Uso (PowerShell):
    python encolar_valoracion.py <document_id> [codigo_contrato] [--force]

Ejemplo (el doc del e2e):
    python encolar_valoracion.py 4bdae59c-d173-4a6b-a82a-601ef2dfc0ce CTSU24/0476 --force

Requiere COLAS_CONNECTION_STRING (Azurite) en el .env o el entorno.
"""
from __future__ import annotations

import sys

from ruesma_comun.colas import COLA_VALORACION, MensajeValoracion
from ruesma_comun.colas.arranque import construir_publicador


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    args = [a for a in sys.argv[1:]]
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    if not args:
        print(__doc__)
        return 2

    document_id = args[0]
    codigo_contrato = args[1] if len(args) > 1 else None

    publicador = construir_publicador(emitido_por="encolar_valoracion")
    mensaje = MensajeValoracion(
        document_id=document_id,
        codigo_contrato=codigo_contrato,
        force=force,
    )
    publicador.publicar(COLA_VALORACION, mensaje)
    print(
        f"[encolado] q-valoracion <- document_id={document_id} "
        f"contrato={codigo_contrato} force={force}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
