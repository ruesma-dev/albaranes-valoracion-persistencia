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
from infrastructure.clients.http_valuation_ia_client import (
    HttpValuationIaClient,
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


def build_app(settings: Settings) -> FastAPI:
    session_factory = SessionFactory(database_url=settings.database_url)
    repository = SqlAlchemyValuationRepository(session_factory)
    # NO llamamos a repository.initialize() aquí a propósito.
    #
    # El servicio 6 puede arrancar ANTES de que exista la BBDD o ANTES
    # de que el servicio 3 haya creado sus tablas (albaran_documents_merge,
    # albaran_lines_merge, albaran_contrato_lines_merge), de las cuales
    # dependen nuestras FK. Forzar la creación en el arranque provocaría
    # un error inmediato en ese escenario.
    #
    # Inicialización perezosa: cada método público del repositorio
    # llama internamente a self.initialize(), que es idempotente y solo
    # ejecuta la DDL una vez por proceso. Así, cuando llegue la primera
    # petición de valoración (/v1/valuation/run-async desde el svc 3, o
    # /v1/valuation/{doc}/re-run desde el front), el svc 3 ya habrá
    # creado sus tablas y nosotros crearemos las nuestras sobre ellas
    # sin problema.

    ia_client = HttpValuationIaClient(
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
            # Estado de la BBDD (útil para diagnosticar:
            #   schema_ready=False → aún no hemos podido crear tablas.
            #   schema_ready=True  → ya corrimos la DDL al menos una vez.
            # No forzamos creación aquí; solo informamos.
            "db_database_url_present": bool(settings.database_url),
            "schema_ready": repository.is_initialized,
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
        codigo_contrato: str | None = None,
    ) -> Dict[str, Any]:
        """Atajo para el front: re-valora con force=True."""
        try:
            result = pipeline.run(
                RunValuationRequest(
                    document_id=document_id,
                    codigo_contrato=codigo_contrato,
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
