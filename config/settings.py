# config/settings.py
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    """Configuración del servicio 6 (valuation-persistence).

    Este servicio orquesta al servicio 5 (HTTP), aplica las reglas
    deterministas de valoración (conversión unidad, reconciliación
    precios, matching partida) y persiste en 3 tablas nuevas.
    """

    # BBDD (misma que svc 3 y svc 5; escribe)
    pg_host: str = Field("localhost", alias="PG_HOST")
    pg_port: int = Field(5432, alias="PG_PORT")
    pg_db: str = Field("albaranes", alias="PG_DB")
    pg_user: str = Field(..., alias="PG_USER")
    pg_password: str = Field(..., alias="PG_PASSWORD")

    # Cliente HTTP contra el servicio 5
    valuation_api_base_url: str = Field(
        "http://127.0.0.1:8002",
        alias="VALUATION_API_BASE_URL",
    )
    valuation_api_timeout_s: float = Field(
        300.0,
        alias="VALUATION_API_TIMEOUT_S",
    )

    # Registro de unidades (YAML con factores por categoría)
    unit_registry_yaml_path: str = Field(
        "config/unit_registry.yaml",
        alias="UNIT_REGISTRY_YAML_PATH",
    )

    # Reglas numéricas (pueden ajustarse por .env sin tocar código)
    price_tolerance_pct: float = Field(
        2.0,
        alias="PRICE_TOLERANCE_PCT",
        description="Tolerancia (%) para considerar que precio 1a y 1b "
                    "coinciden.",
    )
    importe_tolerance_pct: float = Field(
        5.0,
        alias="IMPORTE_TOLERANCE_PCT",
        description="Tolerancia (%) entre importe declarado del albarán "
                    "y el calculado.",
    )

    # Código literal que identifica 'almacén/acopio' en el albarán
    alm_codigo_partida: str = Field("ALM", alias="ALM_CODIGO_PARTIDA")

    api_host: str = Field("127.0.0.1", alias="API_HOST")
    api_port: int = Field(8003, alias="API_PORT")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_dir: str = Field("logs", alias="LOG_DIR")
    service_version: str = Field("1.0.0", alias="SERVICE_VERSION")

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def database_url(self) -> str:
        user = quote_plus(self.pg_user)
        password = quote_plus(self.pg_password)
        database = quote_plus(self.pg_db)
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.pg_host}:{self.pg_port}/{database}"
        )
