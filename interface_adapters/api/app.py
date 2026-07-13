# interface_adapters/api/app.py
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Dict

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from application.pipelines.run_valuation_pipeline import (
    RunValuationPipeline,
    RunValuationRequest,
)
from application.services.importe_calculator import ImporteCalculator
from application.services.partida_matcher import PartidaMatcher
from application.services.price_reconciler import PriceReconciler
from application.services.unit_category_guard import UnitCategoryGuard
from application.services.unit_converter import UnitConverter
from application.services.valuation_builder import ValuationBuilder
from config.settings import Settings
from infrastructure.clients.http_conciliacion_client import (
    HttpConciliacionClient,
)
from infrastructure.clients.http_valuation_ia_client import (
    HttpValuationIaClient,
)
from infrastructure.database import schema_contribution
from infrastructure.database.schema_drift_check import (
    check_drift,
    log_drift_report,
)
from infrastructure.database.session_factory import SessionFactory
from infrastructure.database.sqlalchemy_valuation_repository import (
    SqlAlchemyValuationRepository,
)
from infrastructure.units.yaml_unit_registry import YamlUnitRegistry

logger = logging.getLogger(__name__)


class RunBody(BaseModel):
    document_id: str
    codigo_contrato: str | None = None
    force: bool = False
    line_already_valued: bool = False


class RerunBody(BaseModel):
    """Body del POST /v1/valuation/{document_id}/re-run (jun 2026).

    El sv7 (HttpValuatorClient.rerun) SIEMPRE envió el contrato como
    JSON body ``{"codigo_contrato": "..."}``, pero el endpoint lo
    declaraba como parámetro suelto → FastAPI lo interpretaba como
    QUERY PARAM y el body se descartaba en silencio: el re-run llegaba
    con ``codigo_contrato=None`` y dependía del fallback al contrato
    seleccionado en BBDD. Este modelo restituye el contrato del API.
    """

    codigo_contrato: str | None = None


def build_app(settings: Settings) -> FastAPI:
    session_factory = SessionFactory(database_url=settings.database_url)
    repository = SqlAlchemyValuationRepository(session_factory)

    # ----------------------------------------------------------------- #
    # Inicialización ANSIOSA del schema (con tolerancia).
    #
    # Históricamente teníamos init perezosa por miedo a arrancar antes
    # que el sv3 hubiera creado sus tablas. Ahora el orquestador (sv7)
    # garantiza el orden: aplica primero el schema del sv3 y después el
    # nuestro. Por tanto al arrancar nosotros, las tablas externas YA
    # están — y nos interesa correr el DDL inmediatamente para que los
    # ALTER ... ADD COLUMN IF NOT EXISTS se apliquen ANTES del primer
    # tráfico (y no en la primera petición, como antes).
    #
    # Si por la razón que sea (sv7 no se ha ejecutado, BBDD aún no
    # provisionada) no podemos inicializar, NO matamos el proceso:
    # logueamos warning y caemos al modo perezoso. El primer request
    # reintentará.
    # ----------------------------------------------------------------- #
    try:
        repository.initialize()
    except Exception:
        logger.warning(
            "[svc6][wiring] No se pudo inicializar el schema en arranque "
            "(¿sv7 aún no ha creado las tablas externas? ¿BBDD no "
            "disponible?). Caemos a inicialización perezosa: la primera "
            "petición reintentará.",
            exc_info=True,
        )

    # ----------------------------------------------------------------- #
    # Chequeo de drift ORM ↔ BBDD.
    #
    # Solo informativo: NO modifica nada. Si detecta columnas del ORM
    # ausentes en BBDD, avisa por log antes de que llegue tráfico, lo
    # que evita el clásico "fallo en runtime al primer INSERT". Si no
    # hemos podido inicializar arriba, el reporte saldrá ruidoso — es
    # lo que queremos.
    # ----------------------------------------------------------------- #
    if settings.schema_drift_check_enabled:
        try:
            report = check_drift(session_factory.engine, only_owned=True)
            log_drift_report(report, service_label="svc6")
        except Exception:
            logger.exception(
                "[svc6][wiring] error ejecutando schema_drift_check "
                "(no crítico, seguimos)."
            )

    ia_client = HttpValuationIaClient(
        base_url=settings.valuation_api_base_url,
        timeout_s=settings.valuation_api_timeout_s,
    )
    conciliacion_client = HttpConciliacionClient(
        base_url=settings.valuation_api_base_url,
        timeout_s=settings.valuation_api_timeout_s,
    )

    unit_registry = YamlUnitRegistry(settings.unit_registry_yaml_path)

    builder = ValuationBuilder(
        unit_category_guard=UnitCategoryGuard(unit_registry=unit_registry),
        price_reconciler=PriceReconciler(
            tolerance_pct=settings.price_tolerance_pct,
        ),
        partida_matcher=PartidaMatcher(
            alm_codigo_partida=settings.alm_codigo_partida,
        ),
        unit_converter=UnitConverter(registry=unit_registry),
        importe_calculator=ImporteCalculator(
            tolerance_pct=settings.importe_tolerance_pct,
        ),
    )
    pipeline = RunValuationPipeline(
        ia_client=ia_client,
        repository=repository,
        builder=builder,
        conciliacion_client=conciliacion_client,
    )

    app = FastAPI(
        title="Albaranes Valuation Persistence API",
        version=settings.service_version,
    )

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {
            "ok": True,
            "service": "albaranes-valuation-persistence",
            "version": settings.service_version,
            "valuation_api_base_url": settings.valuation_api_base_url,
            "unit_registry_yaml_path": settings.unit_registry_yaml_path,
            "price_tolerance_pct": settings.price_tolerance_pct,
            "importe_tolerance_pct": settings.importe_tolerance_pct,
            "alm_codigo_partida": settings.alm_codigo_partida,
            # Estado de la BBDD (útil para diagnosticar):
            #   schema_ready=False → aún no hemos podido crear tablas.
            #   schema_ready=True  → DDL aplicado y verificado.
            "db_database_url_present": bool(settings.database_url),
            "schema_ready": repository.is_initialized,
        }

    # ----------------------------------------------------------------- #
    # Endpoint público de schema (consumido por el orquestador sv7).
    # ----------------------------------------------------------------- #
    @app.get("/schema/ddl")
    def get_schema_ddl() -> Dict[str, Any]:
        """Devuelve el DDL de las tablas propias de este servicio.

        Consumido por el orquestador (sv7) para crear / migrar el
        schema en la BBDD compartida sin tener una copia local
        desactualizada del DDL.

        Cualquier servicio de orquestación que quiera aplicar este
        DDL DEBE hacerlo en autocommit (cada sentencia en su propia
        transacción) para que un fallo puntual no aborte el resto.
        """
        ddl = schema_contribution.get_ddl_statements()
        return {
            "schema_name": schema_contribution.SCHEMA_NAME,
            "schema_version": schema_contribution.SCHEMA_VERSION,
            "depends_on": list(schema_contribution.SCHEMA_DEPENDS_ON),
            "owned_tables": schema_contribution.get_owned_table_names(),
            "external_table_dependencies":
                schema_contribution.get_external_table_dependencies(),
            "ddl_statements": [
                {"label": label, "sql": sql} for label, sql in ddl
            ],
            "total_statements": len(ddl),
        }

    @app.post("/v1/valuation/run")
    def run_valuation(body: RunBody) -> Dict[str, Any]:
        """Ejecuta la valoración síncrono — bloquea hasta que termina."""
        try:
            result = pipeline.run(
                RunValuationRequest(
                    document_id=body.document_id,
                    codigo_contrato=body.codigo_contrato,
                    force=body.force,
                    line_already_valued=body.line_already_valued,
                )
            )
            return asdict(result)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception(
                "Error ejecutando valoración document_id=%s",
                body.document_id,
            )
            raise HTTPException(
                status_code=500,
                detail=f"Error ejecutando valoración: {exc}",
            ) from exc

    @app.post("/v1/valuation/run-async", status_code=202)
    def run_valuation_async(
        body: RunBody,
        background_tasks: BackgroundTasks,
    ) -> Dict[str, Any]:
        """Dispara la valoración en background y responde 202 al instante."""

        def _run_in_background(request: RunValuationRequest) -> None:
            try:
                result = pipeline.run(request)
                logger.info(
                    "[run-async] OK document_id=%s valuation_id=%s status=%s "
                    "total=%.2f review=%s",
                    result.document_id,
                    result.valuation_id,
                    result.status,
                    result.total_valorado,
                    result.review_required,
                )
            except Exception:
                logger.exception(
                    "[run-async] FALLO background document_id=%s "
                    "contrato=%s",
                    request.document_id,
                    request.codigo_contrato,
                )

        background_tasks.add_task(
            _run_in_background,
            RunValuationRequest(
                document_id=body.document_id,
                codigo_contrato=body.codigo_contrato,
                force=body.force,
                line_already_valued=body.line_already_valued,
            ),
        )
        return {
            "accepted": True,
            "document_id": body.document_id,
            "codigo_contrato": body.codigo_contrato,
            "force": body.force,
            "message": "valoración encolada en background",
        }

    @app.get("/v1/valuation/{document_id}")
    def get_valuation(document_id: str) -> Dict[str, Any]:
        try:
            return repository.read_full_valuation_json(
                document_id=document_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception(
                "Error leyendo valoración document_id=%s", document_id,
            )
            raise HTTPException(
                status_code=500,
                detail=f"Error leyendo valoración: {exc}",
            ) from exc

    @app.post("/v1/valuation/{document_id}/re-run")
    def rerun_valuation(
        document_id: str,
        body: RerunBody | None = None,
        codigo_contrato: str | None = None,
    ) -> Dict[str, Any]:
        """Atajo para el front y el sv7: re-valora con force=True.

        Acepta el contrato por JSON body (``RerunBody`` — lo que envía
        el sv7) Y por query param (compatibilidad con clientes
        antiguos). El body tiene prioridad.
        """
        codigo_efectivo = (
            body.codigo_contrato
            if body is not None and body.codigo_contrato
            else codigo_contrato
        )
        try:
            result = pipeline.run(
                RunValuationRequest(
                    document_id=document_id,
                    codigo_contrato=codigo_efectivo,
                    force=True,
                    line_already_valued=False,
                )
            )
            return asdict(result)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception(
                "Error re-ejecutando valoración document_id=%s", document_id,
            )
            raise HTTPException(
                status_code=500,
                detail=f"Error re-ejecutando valoración: {exc}",
            ) from exc

    return app
