"""One-off: copy Open WebUI's SQLite database into its Postgres schema.

Open WebUI moved from SQLite (in its Docker volume) to our Postgres (schema `open_webui`, see
docker-compose.yml) so the agent API can follow chat deletions. Open WebUI has no migration of
its own, so this copies every table that exists on both sides, converting SQLite's text JSON
and integer booleans to Postgres types.

Steps:
1. Back up the SQLite file out of the old container, as a consistent copy (SQLite backup API):
   docker exec <open-webui> python -c "import sqlite3; d=sqlite3.connect('/tmp/w.db');
       sqlite3.connect('/app/backend/data/webui.db').backup(d); d.close()"
   docker cp <open-webui>:/tmp/w.db ./webui-backup.db
2. docker compose up -d open-webui   (starts on Postgres and creates its empty tables)
3. docker compose stop open-webui
4. uv run python scripts/migrate_owui_to_postgres.py ./webui-backup.db
5. docker compose start open-webui

Usage: uv run python scripts/migrate_owui_to_postgres.py WEBUI_DB [--schema open_webui] [--force]
"""

import argparse
import json
import sqlite3
import sys

import psycopg
from psycopg.types.json import Jsonb

from travel_agent.config import get_settings

SKIP = {"alembic_version", "migratehistory"}


def pg_columns(cur, schema: str, table: str) -> dict[str, str]:
    cur.execute(
        "select column_name, data_type from information_schema.columns "
        "where table_schema = %s and table_name = %s",
        (schema, table),
    )
    return dict(cur.fetchall())


def convert(value, pg_type: str):
    if value is None:
        return None
    if pg_type == "boolean":
        return bool(value)
    if pg_type in ("json", "jsonb"):
        return Jsonb(json.loads(value) if isinstance(value, str) else value)
    return value


def main(args) -> int:
    source = sqlite3.connect(args.sqlite)
    tables = [
        name
        for (name,) in source.execute("select name from sqlite_master where type = 'table'")
        if name not in SKIP and not name.startswith("sqlite_")
    ]
    with psycopg.connect(get_settings().database_url) as pg, pg.cursor() as cur:
        cur.execute(f'select count(*) from "{args.schema}"."chat"')
        if cur.fetchone()[0] and not args.force:
            print(f"{args.schema}.chat already has rows; pass --force to replace them")
            return 1
        for table in tables:
            columns = pg_columns(cur, args.schema, table)
            if not columns:
                print(f"skip {table}: not in Postgres")
                continue
            names = [r[1] for r in source.execute(f'pragma table_info("{table}")')]
            shared = [n for n in names if n in columns]
            quoted = ", ".join(f'"{n}"' for n in shared)
            rows = source.execute(f'select {quoted} from "{table}"').fetchall()
            cur.execute(f'truncate "{args.schema}"."{table}" cascade')
            if rows:
                placeholders = ", ".join(["%s"] * len(shared))
                cur.executemany(
                    f'insert into "{args.schema}"."{table}" ({quoted}) values ({placeholders})',
                    [
                        [convert(v, columns[n]) for v, n in zip(row, shared, strict=True)]
                        for row in rows
                    ],
                )
            dropped = sorted(set(names) - set(shared))
            print(
                f"{table}: {len(rows)} rows"
                + (f" (columns not copied: {dropped})" if dropped else "")
            )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sqlite", help="path to a copy of Open WebUI's webui.db")
    parser.add_argument("--schema", default="open_webui")
    parser.add_argument("--force", action="store_true", help="replace existing Postgres rows")
    sys.exit(main(parser.parse_args()))
