# interface_adapters/composition.py
"""Composition root del WORKER de valoración (ca-valorador, sv5+sv6).

Replica el wiring de ``build_app`` (interface_adapters/api/app.py) pero SOLO
en lo que necesita el pipeline de valoración, omitiendo los endpoints FastAPI.
No se toca ``app.py``: el modo HTTP sigue intacto; este módulo es la entrada
para el modo worker (consumidor de q-valoracion).

Igual que en HTTP, ``RunValuationPipeline`` llama al servicio 5 por HTTP
(``HttpValuationIaClient`` → ``VALUATION_API_BASE_URL``). En el piloto local,
sv5 sigue corriendo como su propio FastAPI; en despliegue ambos viven en el
mismo contenedor 'ca-valorador' y se hablan por localhost.
"""
from __future__ import annotations

import logging

from application.pipelines.run_valuation_pipeline import RunValuationPipeline
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


def build_run_valuation_pipeline(settings: Settings) -> RunValuationPipeline:
    """Construye el ``RunValuationPipeline`` con el mismo cableado que HTTP.

    Pasos (idénticos a ``build_app``, sin la capa FastAPI):
      1. SessionFactory + repository, e init ANSIOSO del schema (tolerante:
         si falla, cae a init perezosa — la primera valoración reintenta).
      2. Chequeo de drift ORM↔BBDD (solo informativo), si está habilitado.
      3. HttpValuationIaClient contra el servicio 5.
      4. ValuationBuilder con sus reglas deterministas.
      5. RunValuationPipeline(ia_client, repository, builder).
    """
    session_factory = SessionFactory(database_url=settings.database_url)
    repository = SqlAlchemyValuationRepository(session_factory)

    try:
        repository.initialize()
    except Exception:
        logger.warning(
            "[ca-valorador][worker-wiring] No se pudo inicializar el schema "
            "en arranque (¿BBDD no disponible? ¿tablas externas de sv3 aún "
            "no creadas?). Caemos a inicialización perezosa: la primera "
            "valoración reintentará.",
            exc_info=True,
        )

    if settings.schema_drift_check_enabled:
        try:
            report = check_drift(session_factory.engine, only_owned=True)
            log_drift_report(report, service_label="ca-valorador")
        except Exception:
            logger.exception(
                "[ca-valorador][worker-wiring] error ejecutando "
                "schema_drift_check (no crítico, seguimos)."
            )

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
    logger.info(
        "[ca-valorador][worker-wiring] pipeline construido; "
        "sv5_base_url=%s schema_ready=%s",
        settings.valuation_api_base_url,
        repository.is_initialized,
    )
    return pipeline
