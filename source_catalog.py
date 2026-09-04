"""Discover referenced ASA objects and persist source metadata before migration."""

from __future__ import annotations

import json
import re
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from database import DATABASE_PATH, connect, now


IDENT = r'(?:(?:"[^"]+")|(?:[A-Za-z_][A-Za-z0-9_$]*))'


def _unquote(value: str) -> str:
    return value[1:-1].replace('""', '"') if value.startswith('"') else value


def referenced_relations(sql: str, default_schema: str = 'dba') -> list[tuple[str, str]]:
    """Return unique persistent relations referenced by DML in source order."""
    temporary = {
        _unquote(match.group('name')).lower()
        for match in re.finditer(
            rf'\b(?:DECLARE\s+LOCAL\s+TEMPORARY|CREATE\s+TEMPORARY)\s+TABLE\s+(?:pg_temp\.)?(?P<name>{IDENT})',
            sql, re.I,
        )
    }
    found = []
    seen = set()
    pattern = re.compile(
        rf'\b(?:FROM|JOIN|UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+'
        rf'(?:(?P<schema>{IDENT})\s*\.\s*)?(?P<name>{IDENT})',
        re.I,
    )
    for match in pattern.finditer(sql):
        name = _unquote(match.group('name'))
        schema = _unquote(match.group('schema')) if match.group('schema') else default_schema
        key = (schema.lower(), name.lower())
        if name.lower() in temporary or key in seen:
            continue
        seen.add(key)
        found.append((schema, name))
    return found


@dataclass
class SourceCatalog:
    snapshot_id: int
    columns: dict[tuple[str, str, str], str]

    def column_type(self, schema: str, table: str, column: str) -> str | None:
        return self.columns.get((schema.lower(), table.lower(), column.lower()))


def capture_source_catalog(connection, project_id: str, sql: str, database_path: Path = DATABASE_PATH,
                           default_schema: str = 'dba') -> SourceCatalog:
    """Capture definitions for referenced ASA relations into the SQLite registry."""
    relations = referenced_relations(sql, default_schema)
    with closing(connect(database_path)) as registry:
        snapshot_id = registry.execute(
            "INSERT INTO source_catalog_snapshots(project_id,source_dialect,captured_at,source_sql) VALUES (?,?,?,?)",
            (project_id, 'sybase_asa', now(), sql),
        ).lastrowid
        collected = {}
        for schema, name in relations:
            rows = _fetch_columns(connection, schema, name)
            object_type, view_definition = _fetch_object_definition(connection, schema, name)
            normalized_definition = view_definition or json.dumps([
                {'name': row[0], 'ordinal': row[1], 'type': row[2], 'length': row[3],
                 'precision': row[4], 'scale': row[5], 'nullable': str(row[6]).upper() == 'YES'}
                for row in rows
            ], separators=(',', ':'))
            object_id = registry.execute(
                """INSERT INTO source_catalog_objects
                   (snapshot_id,schema_name,object_name,object_type,definition_text) VALUES (?,?,?,?,?)""",
                (snapshot_id, schema, name, object_type, normalized_definition),
            ).lastrowid
            registry.execute(
                "INSERT INTO source_catalog_dependencies(snapshot_id,schema_name,object_name) VALUES (?,?,?)",
                (snapshot_id, schema, name),
            )
            for column_name, ordinal, data_type, length, precision, scale, nullable in rows:
                registry.execute(
                    """INSERT INTO source_catalog_columns
                       (object_id,column_name,ordinal_position,data_type,character_maximum_length,
                        numeric_precision,numeric_scale,is_nullable) VALUES (?,?,?,?,?,?,?,?)""",
                    (object_id, column_name, ordinal, data_type, length, precision, scale,
                     1 if str(nullable).upper() == 'YES' else 0),
                )
                collected[(schema.lower(), name.lower(), str(column_name).lower())] = str(data_type)
        registry.commit()
    return SourceCatalog(snapshot_id, collected)


def _fetch_columns(connection, schema: str, table: str) -> list[tuple]:
    cursor = connection.cursor()
    try:
        cursor.execute(
            """SELECT c.column_name, c.column_id, c.base_type_str,
                      c.width, c.width, c.scale,
                      CASE WHEN c.nulls = 'Y' THEN 'YES' ELSE 'NO' END
               FROM SYS.SYSTAB t
               JOIN SYS.SYSUSER u ON u.user_id = t.creator
               JOIN SYS.SYSTABCOL c ON c.table_id = t.table_id
               WHERE LOWER(u.user_name)=LOWER(?) AND LOWER(t.table_name)=LOWER(?)
               ORDER BY c.column_id""",
            (schema, table),
        )
        return list(cursor.fetchall())
    finally:
        close = getattr(cursor, 'close', None)
        if close:
            close()


def _fetch_object_definition(connection, schema: str, table: str) -> tuple[str, str]:
    cursor = connection.cursor()
    try:
        cursor.execute(
            """SELECT t.table_type_str, v.view_def
               FROM SYS.SYSTAB t
               JOIN SYS.SYSUSER u ON u.user_id = t.creator
               LEFT JOIN SYS.SYSVIEW v ON v.view_object_id = t.object_id
               WHERE LOWER(u.user_name)=LOWER(?) AND LOWER(t.table_name)=LOWER(?)""",
            (schema, table),
        )
        row = cursor.fetchone()
        object_type = str(row[0]).lower().replace('base', 'table') if row else 'missing'
        definition = str(row[1]) if row and len(row) > 1 and row[1] is not None else ''
        return object_type, definition
    finally:
        close = getattr(cursor, 'close', None)
        if close:
            close()
