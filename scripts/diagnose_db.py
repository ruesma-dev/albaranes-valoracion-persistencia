# diagnose_db.py
from __future__ import annotations

import sys

from sqlalchemy import create_engine, text

from config.settings import Settings


def main() -> None:
    settings = Settings()
    url = settings.database_url
    print(f"URL: {url}")

    engine = create_engine(url)
    with engine.connect() as conn:
        current_db = conn.execute(text("SELECT current_database()")).scalar()
        current_schema = conn.execute(text("SELECT current_schema()")).scalar()
        search_path = conn.execute(text("SHOW search_path")).scalar()
        print(f"current_database : {current_db}")
        print(f"current_schema   : {current_schema}")
        print(f"search_path      : {search_path}")

        val_regclass = conn.execute(
            text("SELECT to_regclass('public.albaran_valuations')")
        ).scalar()
        doc_regclass = conn.execute(
            text("SELECT to_regclass('public.albaran_documents_merge')")
        ).scalar()
        print(f"public.albaran_valuations         -> {val_regclass}")
        print(f"public.albaran_documents_merge    -> {doc_regclass}")

        print("\nTablas públicas (pg_tables):")
        rows = conn.execute(
            text(
                "SELECT schemaname, tablename "
                "FROM pg_tables "
                "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') "
                "ORDER BY schemaname, tablename"
            )
        ).all()
        for schema, name in rows:
            print(f"  {schema}.{name}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc!r}", file=sys.stderr)
        raise