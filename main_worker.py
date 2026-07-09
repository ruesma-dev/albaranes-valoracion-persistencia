# main_worker.py
"""Entrypoint del WORKER de valoración (ca-valorador = sv5+sv6).

Consume q-valoracion con ``{document_id, codigo_contrato, force}``, ejecuta el
``RunValuationPipeline`` real (que llama a sv5 por HTTP, aplica reglas y
persiste) y deja el documento valorado a la espera de aprobación en sv4. No
encadena ninguna cola posterior.

Variables de entorno:
  - COLAS_CONNECTION_STRING (local/Azurite) o COLAS_ACCOUNT_URL (nube).
  - VALUATION_API_BASE_URL : URL del servicio 5 (def http://127.0.0.1:8002).
    En el piloto local, arranca sv5 como su FastAPI normal (python main.py).
  + todas las de sv6 (PG_*, UNIT_REGISTRY_YAML_PATH, *_TOLERANCE_PCT, …).

Requisito del piloto: sv5 debe estar levantado y accesible en
VALUATION_API_BASE_URL, y la BBDD debe tener el documento ya persistido por
sv3 (contexto de valoración).
"""
from __future__ import annotations

import logging
from pathlib import Path

from config.logging_config import configure_logging
from config.settings import Settings
from interface_adapters.composition import build_run_valuation_pipeline
from interface_adapters.worker.valuation_worker import (
    construir_handler_valoracion,
)
from ruesma_comun.colas import COLA_VALORACION
from ruesma_comun.colas.arranque import ejecutar_worker


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    settings = Settings()
    configure_logging(Path(settings.log_dir), settings.log_level)
    logging.getLogger("azure").setLevel(logging.WARNING)

    pipeline = build_run_valuation_pipeline(settings)
    handler = construir_handler_valoracion(pipeline=pipeline)

    return ejecutar_worker(
        nombre_cola=COLA_VALORACION,
        tipo_mensaje="valoracion",
        handler=handler,
        emitido_por="ca-valorador",
    )


if __name__ == "__main__":
    raise SystemExit(main())
