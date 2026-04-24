# infrastructure/database/session_factory.py
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


class SessionFactory:
    """Factoría simple. La BBDD ya la creó el servicio 3."""

    def __init__(self, database_url: str) -> None:
        self._engine: Engine = create_engine(
            database_url,
            future=True,
            pool_pre_ping=True,
        )
        self._sessionmaker = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            future=True,
        )

    @property
    def engine(self) -> Engine:
        return self._engine

    def create_session(self) -> Session:
        return self._sessionmaker()
