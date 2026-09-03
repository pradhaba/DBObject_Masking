"""Embedded SQLite persistence for projects, uploads, objects, and processing runs."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path


DATABASE_DIR = Path(__file__).resolve().parent / "data"
DATABASE_PATH = DATABASE_DIR / "ddl_masker.sqlite3"

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_database TEXT NOT NULL,
    target_database TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    database_name TEXT NOT NULL,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    archive_path TEXT NOT NULL DEFAULT '',
    workspace TEXT NOT NULL DEFAULT '',
    default_operation TEXT NOT NULL DEFAULT 'mask',
    object_scope TEXT NOT NULL DEFAULT 'all',
    input_type TEXT NOT NULL DEFAULT 'archive'
);
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    archive_path TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    file_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS project_objects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    absolute_path TEXT NOT NULL,
    selected INTEGER NOT NULL DEFAULT 1,
    discovered_at TEXT NOT NULL,
    UNIQUE(project_id, relative_path)
);
CREATE TABLE IF NOT EXISTS processing_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    object_path TEXT NOT NULL,
    operation TEXT NOT NULL,
    dialect TEXT NOT NULL,
    input_ddl TEXT NOT NULL,
    output_ddl TEXT NOT NULL,
    mapping_json TEXT,
    status TEXT NOT NULL,
    error_message TEXT,
    processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_catalog_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    source_dialect TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    source_sql TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS source_catalog_objects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES source_catalog_snapshots(id) ON DELETE CASCADE,
    schema_name TEXT NOT NULL,
    object_name TEXT NOT NULL,
    object_type TEXT NOT NULL,
    definition_text TEXT NOT NULL DEFAULT '',
    UNIQUE(snapshot_id, schema_name, object_name)
);
CREATE TABLE IF NOT EXISTS source_catalog_columns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id INTEGER NOT NULL REFERENCES source_catalog_objects(id) ON DELETE CASCADE,
    column_name TEXT NOT NULL,
    ordinal_position INTEGER NOT NULL,
    data_type TEXT NOT NULL,
    character_maximum_length INTEGER,
    numeric_precision INTEGER,
    numeric_scale INTEGER,
    is_nullable INTEGER NOT NULL DEFAULT 1,
    UNIQUE(object_id, column_name)
);
CREATE TABLE IF NOT EXISTS source_catalog_dependencies (
    snapshot_id INTEGER NOT NULL REFERENCES source_catalog_snapshots(id) ON DELETE CASCADE,
    schema_name TEXT NOT NULL,
    object_name TEXT NOT NULL,
    reference_kind TEXT NOT NULL DEFAULT 'relation',
    PRIMARY KEY(snapshot_id, schema_name, object_name)
);
CREATE TABLE IF NOT EXISTS masking_rules (
    object_type TEXT PRIMARY KEY,
    token_prefix TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS unmasking_rules (
    dialect TEXT PRIMARY KEY,
    parameter_prefix TEXT NOT NULL DEFAULT '',
    variable_prefix TEXT NOT NULL DEFAULT '',
    preserve_at_sigil INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS migration_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_dialect TEXT NOT NULL,
    target_dialect TEXT NOT NULL,
    instructions TEXT NOT NULL,
    transformations_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source_dialect, target_dialect)
);
CREATE TABLE IF NOT EXISTS skill_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id INTEGER NOT NULL REFERENCES migration_skills(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','testing','awaiting_approval','active','superseded','rejected')),
    instructions TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'system',
    approved_by TEXT,
    approved_at TEXT,
    UNIQUE(skill_id, version)
);
CREATE TABLE IF NOT EXISTS skill_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_version_id INTEGER NOT NULL REFERENCES skill_versions(id) ON DELETE CASCADE,
    rule_code TEXT NOT NULL,
    priority INTEGER NOT NULL,
    pattern TEXT NOT NULL,
    replacement TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    UNIQUE(skill_version_id, rule_code)
);
CREATE TABLE IF NOT EXISTS deployment_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    processing_run_id INTEGER REFERENCES processing_runs(id) ON DELETE SET NULL,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    skill_version_id INTEGER REFERENCES skill_versions(id) ON DELETE SET NULL,
    target_dialect TEXT NOT NULL,
    deployed_sql TEXT NOT NULL,
    status TEXT NOT NULL,
    sqlstate TEXT,
    error_message TEXT,
    error_position INTEGER,
    attempted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_change_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_attempt_id INTEGER NOT NULL REFERENCES deployment_attempts(id) ON DELETE CASCADE,
    base_skill_version_id INTEGER NOT NULL REFERENCES skill_versions(id),
    title TEXT NOT NULL,
    rationale TEXT NOT NULL,
    pattern TEXT NOT NULL DEFAULT '',
    replacement TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    test_status TEXT NOT NULL DEFAULT 'pending',
    proposed_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    resulting_skill_version_id INTEGER REFERENCES skill_versions(id)
);
CREATE TABLE IF NOT EXISTS skill_regression_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL REFERENCES skill_change_proposals(id) ON DELETE CASCADE,
    test_name TEXT NOT NULL,
    passed INTEGER NOT NULL,
    details TEXT NOT NULL,
    tested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_objects_project ON project_objects(project_id);
CREATE INDEX IF NOT EXISTS idx_runs_project ON processing_runs(project_id, processed_at);
CREATE INDEX IF NOT EXISTS idx_source_catalog_project ON source_catalog_snapshots(project_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_source_columns_name ON source_catalog_columns(column_name);
"""

PROJECT_COLUMNS = {
    "default_operation": "TEXT NOT NULL DEFAULT 'mask'",
    "object_scope": "TEXT NOT NULL DEFAULT 'all'",
    "input_type": "TEXT NOT NULL DEFAULT 'archive'",
    "target_host": "TEXT NOT NULL DEFAULT 'localhost'",
    "target_port": "INTEGER NOT NULL DEFAULT 5432",
    "target_database_name": "TEXT NOT NULL DEFAULT ''",
    "target_username": "TEXT NOT NULL DEFAULT ''",
    "formatter_indent": "TEXT NOT NULL DEFAULT '4 spaces'",
}
RUN_COLUMNS = {
    "source_dialect": "TEXT NOT NULL DEFAULT 'generic'",
    "target_dialect": "TEXT NOT NULL DEFAULT 'generic'",
    "migration_skill_id": "INTEGER",
    "skill_version_id": "INTEGER",
    "target_object_type": "TEXT",
    "classification_reason": "TEXT",
    "skill_trace_json": "TEXT",
    "classification_rule": "TEXT",
    "human_override": "TEXT",
    "routine_analysis_json": "TEXT",
    "routine_language": "TEXT",
    "technical_status": "TEXT NOT NULL DEFAULT 'success'",
    "review_status": "TEXT NOT NULL DEFAULT 'pending_review'",
    "diagnostics_json": "TEXT NOT NULL DEFAULT '[]'",
    "reviewed_by": "TEXT",
    "reviewed_at": "TEXT",
    "review_notes": "TEXT NOT NULL DEFAULT ''",
}
RULE_COLUMNS = {
    "description": "TEXT NOT NULL DEFAULT ''",
    "source_example": "TEXT NOT NULL DEFAULT ''",
    "target_example": "TEXT NOT NULL DEFAULT ''",
    "risk_level": "TEXT NOT NULL DEFAULT 'low'",
    "category": "TEXT NOT NULL DEFAULT 'general'",
    "review_status": "TEXT NOT NULL DEFAULT 'awaiting_approval'",
    "review_notes": "TEXT NOT NULL DEFAULT ''",
    "is_custom": "INTEGER NOT NULL DEFAULT 0",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path = DATABASE_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    _upgrade_columns(connection, "projects", PROJECT_COLUMNS)
    _upgrade_columns(connection, "processing_runs", RUN_COLUMNS)
    _upgrade_columns(connection, "skill_rules", RULE_COLUMNS)
    _seed_rules(connection)
    _seed_skill_versions(connection)
    _retire_auto_approved_versions(connection)
    _seed_asa_procedure_candidate(connection)
    _seed_additional_asa_rules(connection)
    _inherit_unchanged_rule_reviews(connection)
    _seed_postgresql_type_revision(connection)
    _seed_global_variable_rule(connection)
    _seed_schema_qualification_rule(connection)
    _seed_builtin_function_rules(connection)
    _seed_table_alias_rule(connection)
    return connection


def _upgrade_columns(connection, table: str, definitions: dict[str, str]) -> None:
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    for name, definition in definitions.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    connection.commit()


def _seed_rules(connection) -> None:
    prefixes = [
        ("table","TBL",1),("view","VW",2),("procedure","PROC",3),("function","FUNC",4),
        ("trigger","TRG",5),("index","IDX",6),("sequence","SEQ",7),("type","TYPE",8),
        ("column","COL",9),("parameter","PARAM",10),("variable","VAR",11),
    ]
    connection.executemany(
        "INSERT OR IGNORE INTO masking_rules(object_type,token_prefix,sort_order) VALUES (?,?,?)", prefixes
    )
    connection.executemany(
        """INSERT OR IGNORE INTO unmasking_rules
        (dialect,parameter_prefix,variable_prefix,preserve_at_sigil) VALUES (?,?,?,?)""",
        [("generic","","",0),("postgresql","p_","_",0),("sybase_ase","","",1),("sybase_asa","","",1)],
    )
    common = [
        ("SAP ASE to PostgreSQL","sybase_ase","postgresql","Convert SAP ASE DDL to PostgreSQL while preserving masked identifiers.",
         json.dumps([{"pattern":r"(?im)^\s*GO\s*$","replacement":""},{"pattern":r"\bGETDATE\s*\(\s*\)","replacement":"CURRENT_TIMESTAMP"},{"pattern":r"\bDATETIME\b","replacement":"TIMESTAMP"},{"pattern":r"\bTINYINT\b","replacement":"SMALLINT"}])),
        ("SAP ASA to PostgreSQL","sybase_asa","postgresql","Convert SAP SQL Anywhere DDL to PostgreSQL while preserving masked identifiers.",
         json.dumps([{"pattern":r"(?im)^\s*GO\s*$","replacement":""},{"pattern":r"\bGETDATE\s*\(\s*\)","replacement":"CURRENT_TIMESTAMP"},{"pattern":r"\bDATETIME\b","replacement":"TIMESTAMP"}])),
        ("PostgreSQL to SAP ASE","postgresql","sybase_ase","Convert PostgreSQL DDL to SAP ASE while preserving masked identifiers.",
         json.dumps([{"pattern":r"\bCURRENT_TIMESTAMP\b","replacement":"GETDATE()"},{"pattern":r"\bTIMESTAMP\b","replacement":"DATETIME"},{"pattern":r"\bBOOLEAN\b","replacement":"BIT"}])),
        ("PostgreSQL to SAP ASA","postgresql","sybase_asa","Convert PostgreSQL DDL to SAP SQL Anywhere while preserving masked identifiers.",
         json.dumps([{"pattern":r"\bCURRENT_TIMESTAMP\b","replacement":"CURRENT TIMESTAMP"},{"pattern":r"\bBOOLEAN\b","replacement":"BIT"}])),
    ]
    connection.executemany(
        """INSERT OR IGNORE INTO migration_skills
        (name,source_dialect,target_dialect,instructions,transformations_json) VALUES (?,?,?,?,?)""", common
    )
    dialects = ("generic", "oracle", "sqlserver", "postgresql", "sybase_ase", "sybase_asa")
    defaults = [
        (f"{source} to {target}", source, target,
         f"Convert {source} DDL to {target}. Preserve masked identifiers and review unsupported vendor syntax.", "[]")
        for source in dialects for target in dialects if source != target
    ]
    connection.executemany(
        """INSERT OR IGNORE INTO migration_skills
        (name,source_dialect,target_dialect,instructions,transformations_json) VALUES (?,?,?,?,?)""", defaults
    )
    connection.commit()


def _seed_skill_versions(connection) -> None:
    skills = connection.execute("SELECT * FROM migration_skills WHERE enabled=1").fetchall()
    for skill in skills:
        existing = connection.execute("SELECT id FROM skill_versions WHERE skill_id=? LIMIT 1", (skill["id"],)).fetchone()
        if existing:
            continue
        cursor = connection.execute(
            """INSERT INTO skill_versions
            (skill_id,version,status,instructions,created_at,created_by)
            VALUES (?,1,'draft',?,?,'system')""",
            (skill["id"], skill["instructions"], now()),
        )
        for priority, rule in enumerate(json.loads(skill["transformations_json"]), start=1):
            connection.execute(
                """INSERT INTO skill_rules
                (skill_version_id,rule_code,priority,pattern,replacement) VALUES (?,?,?,?,?)""",
                (cursor.lastrowid, f"seed-{priority}", priority, rule["pattern"], rule.get("replacement", "")),
            )
    connection.commit()


def _retire_auto_approved_versions(connection) -> None:
    connection.execute(
        """UPDATE skill_versions SET status='superseded'
        WHERE status='active' AND created_by='system' AND approved_by='system'"""
    )
    connection.commit()


def _seed_asa_procedure_candidate(connection) -> None:
    """Install known ASA rules as a review candidate, never as auto-approved."""
    skill = connection.execute(
        "SELECT * FROM migration_skills WHERE source_dialect='sybase_asa' AND target_dialect='postgresql'"
    ).fetchone()
    if skill is None:
        return
    existing = connection.execute(
        """SELECT sv.id FROM skill_versions sv JOIN skill_rules sr ON sr.skill_version_id=sv.id
        WHERE sv.skill_id=? AND ((sv.status='awaiting_approval' AND sr.rule_code='asa-pg-remainder')
        OR (sv.status='active' AND sr.rule_code='asa-pg-varchar-star' AND EXISTS (
            SELECT 1 FROM skill_rules approved WHERE approved.skill_version_id=sv.id
            AND approved.review_status='approved'))) LIMIT 1""", (skill["id"],)
    ).fetchone()
    if existing:
        return
    previous = connection.execute(
        "SELECT * FROM skill_versions WHERE skill_id=? ORDER BY version DESC LIMIT 1", (skill["id"],)
    ).fetchone()
    connection.execute("UPDATE skill_versions SET status='superseded' WHERE skill_id=? AND status='active'", (skill["id"],))
    version = connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM skill_versions WHERE skill_id=?", (skill["id"],)).fetchone()[0]
    instructions = (
        "Migrate SAP SQL Anywhere ASA procedures to PostgreSQL routines using only reviewed deterministic rules. "
        "Preserve masked identifiers and flag unsupported transactional, dynamic SQL, cursor, and exception constructs."
    )
    cursor = connection.execute(
        """INSERT INTO skill_versions(skill_id,version,status,instructions,created_at,created_by)
        VALUES (?,?,'awaiting_approval',?,?,'system-candidate')""",
        (skill["id"], version, instructions, now()),
    )
    if previous is not None:
        connection.execute(
            """INSERT INTO skill_rules(skill_version_id,rule_code,priority,pattern,replacement,enabled,
            description,source_example,target_example,risk_level,category,review_status,review_notes,is_custom)
            SELECT ?,rule_code,priority,pattern,replacement,enabled,description,source_example,target_example,
            risk_level,category,review_status,review_notes,is_custom FROM skill_rules WHERE skill_version_id=?""",
            (cursor.lastrowid, previous["id"]),
        )
    rules = [
        ("asa-pg-go", 10, r"(?im)^\s*GO\s*$", ""),
        ("asa-pg-getdate", 20, r"\bGETDATE\s*\(\s*\)", "CURRENT_TIMESTAMP"),
        ("asa-pg-now", 30, r"\bNOW\s*\(\s*\)", "CURRENT_TIMESTAMP"),
        ("asa-pg-current-date", 40, r"\bCURRENT\s+DATE\b", "CURRENT_DATE"),
        ("asa-pg-isnull", 50, r"\bISNULL\s*\(", "COALESCE("),
        ("asa-pg-long-varchar", 60, r"\bLONG\s+VARCHAR\b", "TEXT"),
        ("asa-pg-datetime", 70, r"\bDATETIME\b", "TIMESTAMP"),
        ("asa-pg-tinyint", 80, r"\bTINYINT\b", "SMALLINT"),
        ("asa-pg-money", 90, r"\bMONEY\b", "NUMERIC(19,4)"),
        ("asa-pg-bit", 100, r"\bBIT\b", "BOOLEAN"),
    ]
    connection.executemany(
        """INSERT OR IGNORE INTO skill_rules(skill_version_id,rule_code,priority,pattern,replacement)
        VALUES (?,?,?,?,?)""", [(cursor.lastrowid, *rule) for rule in rules]
    )
    connection.commit()


def _seed_additional_asa_rules(connection) -> None:
    candidate = connection.execute(
        """SELECT sv.id FROM skill_versions sv JOIN migration_skills ms ON ms.id=sv.skill_id
        WHERE ms.source_dialect='sybase_asa' AND ms.target_dialect='postgresql'
        AND sv.status='awaiting_approval' ORDER BY sv.version DESC LIMIT 1"""
    ).fetchone()
    if candidate is None:
        return
    base_metadata = {
        "asa-pg-go": ("Remove the ASA batch separator; it is not PostgreSQL DDL.", "GO", "", "low"),
        "asa-pg-getdate": ("Convert the ASA current timestamp function.", "GETDATE()", "CURRENT_TIMESTAMP", "low"),
        "asa-pg-now": ("Convert the ASA NOW function.", "NOW()", "CURRENT_TIMESTAMP", "low"),
        "asa-pg-current-date": ("Normalize the ASA current-date expression.", "CURRENT DATE", "CURRENT_DATE", "low"),
        "asa-pg-isnull": ("Convert the two-argument null fallback function.", "ISNULL(value,0)", "COALESCE(value,0)", "medium"),
        "asa-pg-long-varchar": ("Convert the unbounded character datatype.", "LONG VARCHAR", "TEXT", "low"),
        "asa-pg-datetime": ("Convert the ASA date-and-time datatype.", "DATETIME", "TIMESTAMP", "medium"),
        "asa-pg-tinyint": ("Widen ASA unsigned tiny integer storage.", "TINYINT", "SMALLINT", "low"),
        "asa-pg-money": ("Replace vendor MONEY with an explicit fixed precision.", "MONEY", "NUMERIC(19,4)", "medium"),
        "asa-pg-bit": ("Convert the ASA bit datatype to PostgreSQL boolean.", "BIT", "BOOLEAN", "medium"),
    }
    connection.executemany(
        """UPDATE skill_rules SET description=?,source_example=?,target_example=?,risk_level=?
        WHERE skill_version_id=? AND rule_code=? AND description=''""",
        [(description, source, target, risk, candidate["id"], code)
         for code, (description, source, target, risk) in base_metadata.items()],
    )
    rules = [
        ("asa-pg-today",110,r"\bTODAY\s*\(\s*\)","CURRENT_DATE","Convert ASA TODAY() to the PostgreSQL current-date expression.","SELECT TODAY();","SELECT CURRENT_DATE;","low"),
        ("asa-pg-current-time",120,r"\bCURRENT\s+TIME\b","CURRENT_TIME","Normalize ASA CURRENT TIME syntax.","SELECT CURRENT TIME;","SELECT CURRENT_TIME;","low"),
        ("asa-pg-current-timestamp",130,r"\bCURRENT\s+TIMESTAMP\b","CURRENT_TIMESTAMP","Normalize ASA CURRENT TIMESTAMP syntax.","SELECT CURRENT TIMESTAMP;","SELECT CURRENT_TIMESTAMP;","low"),
        ("asa-pg-lcase",140,r"\bLCASE\s*\(","LOWER(","Convert the ASA lowercase function.","LCASE(name)","LOWER(name)","low"),
        ("asa-pg-ucase",150,r"\bUCASE\s*\(","UPPER(","Convert the ASA uppercase function.","UCASE(name)","UPPER(name)","low"),
        ("asa-pg-datalength",160,r"\bDATALENGTH\s*\(","OCTET_LENGTH(","Convert byte-length calculation.","DATALENGTH(value)","OCTET_LENGTH(value)","medium"),
        ("asa-pg-elseif",170,r"\bELSEIF\b","ELSIF","Convert ASA procedural ELSEIF keyword to PL/pgSQL.","ELSEIF condition THEN","ELSIF condition THEN","low"),
        ("asa-pg-remainder",180,r"\bREMAINDER\s*\(","MOD(","Convert numeric remainder function.","REMAINDER(a,b)","MOD(a,b)","medium"),
        ("asa-pg-parameter-sigil",190,r"@(?=PARAM_\d+\b)","","Remove the ASA @ sigil from masked procedure parameters before PostgreSQL unmasking.","@customer_id INTEGER","p_customer_id INTEGER","low"),
        ("asa-pg-variable-sigil",200,r"@(?=VAR_\d+\b)","","Remove the ASA @ sigil from masked local variables before PostgreSQL unmasking.","DECLARE @total INTEGER","DECLARE _total INTEGER","low"),
        ("asa-pg-begin-transaction",210,r"\bBEGIN\s+TRANSACTION\b","START TRANSACTION","Convert explicit transaction start syntax. PostgreSQL routine restrictions must be tested.","BEGIN TRANSACTION","START TRANSACTION","high"),
        ("asa-pg-commit-work",220,r"\bCOMMIT\s+WORK\b","COMMIT","Normalize transaction commit syntax for PostgreSQL procedures.","COMMIT WORK","COMMIT","high"),
        ("asa-pg-rollback-work",230,r"\bROLLBACK\s+WORK\b","ROLLBACK","Normalize transaction rollback syntax for PostgreSQL procedures.","ROLLBACK WORK","ROLLBACK","high"),
        ("asa-pg-raiserror",240,r"\bRAISERROR\s+\d+\s+'([^']*)'","RAISE EXCEPTION '\\1'","Convert a simple constant ASA RAISERROR statement; complex arguments require a custom rule.","RAISERROR 17000 'Invalid value'","RAISE EXCEPTION 'Invalid value'","high"),
        ("asa-pg-cursor-read-only",250,r"\s+FOR\s+READ\s+ONLY\b","","Remove the ASA read-only cursor qualifier when PostgreSQL does not accept it.","DECLARE c CURSOR FOR SELECT 1 FOR READ ONLY","DECLARE c CURSOR FOR SELECT 1","high"),
        ("asa-pg-execute-immediate",260,r"\bEXECUTE\s+IMMEDIATE\b","EXECUTE","Convert ASA dynamic SQL execution keyword to PL/pgSQL EXECUTE.","EXECUTE IMMEDIATE sql_text","EXECUTE sql_text","high"),
        ("asa-pg-varchar-star",270,r"\bVARCHAR\s*\(\s*\*\s*\)","TEXT","Convert unbounded ASA VARCHAR(*) to PostgreSQL TEXT.","VARCHAR(*)","TEXT","low"),
        ("asa-pg-char-star",280,r"\bCHAR\s*\(\s*\*\s*\)","TEXT","Convert unbounded ASA CHAR(*) to PostgreSQL TEXT.","CHAR(*)","TEXT","medium"),
        ("asa-pg-custom-template",1000,r"CUSTOM_SOURCE_PATTERN","CUSTOM_TARGET_TEXT","Template for a reviewer-defined custom conversion. Edit every field before approval.","CUSTOM_SOURCE_PATTERN","CUSTOM_TARGET_TEXT","high"),
    ]
    connection.executemany(
        """INSERT OR IGNORE INTO skill_rules
        (skill_version_id,rule_code,priority,pattern,replacement,description,source_example,target_example,risk_level)
        VALUES (?,?,?,?,?,?,?,?,?)""", [(candidate["id"], *rule) for rule in rules]
    )
    categories = {
        "asa-pg-go":"procedure_structure", "asa-pg-getdate":"date_time", "asa-pg-now":"date_time",
        "asa-pg-current-date":"date_time", "asa-pg-isnull":"null_handling",
        "asa-pg-long-varchar":"datatypes", "asa-pg-datetime":"datatypes",
        "asa-pg-tinyint":"datatypes", "asa-pg-money":"datatypes", "asa-pg-bit":"datatypes",
        "asa-pg-today":"date_time", "asa-pg-current-time":"date_time",
        "asa-pg-current-timestamp":"date_time", "asa-pg-lcase":"string_functions",
        "asa-pg-ucase":"string_functions", "asa-pg-datalength":"string_functions",
        "asa-pg-elseif":"conditional_control_flow", "asa-pg-remainder":"numeric_functions",
        "asa-pg-parameter-sigil":"parameters_variables", "asa-pg-variable-sigil":"parameters_variables",
        "asa-pg-begin-transaction":"transactions", "asa-pg-commit-work":"transactions",
        "asa-pg-rollback-work":"transactions", "asa-pg-raiserror":"error_handling",
        "asa-pg-cursor-read-only":"cursors", "asa-pg-execute-immediate":"dynamic_sql",
        "asa-pg-varchar-star":"datatypes", "asa-pg-char-star":"datatypes",
        "asa-pg-custom-template":"custom",
    }
    connection.executemany(
        "UPDATE skill_rules SET category=? WHERE skill_version_id=? AND rule_code=?",
        [(category,candidate["id"],code) for code,category in categories.items()],
    )
    connection.commit()


def _inherit_unchanged_rule_reviews(connection) -> None:
    """Carry review decisions forward only when rule behavior is unchanged."""
    candidates = connection.execute(
        "SELECT * FROM skill_versions WHERE status='awaiting_approval'"
    ).fetchall()
    for candidate in candidates:
        previous = connection.execute(
            """SELECT id FROM skill_versions WHERE skill_id=? AND version<?
            ORDER BY version DESC LIMIT 1""", (candidate["skill_id"], candidate["version"])
        ).fetchone()
        if previous is None:
            continue
        connection.execute(
            """UPDATE skill_rules AS current SET
            review_status=(SELECT old.review_status FROM skill_rules old WHERE old.skill_version_id=?
                AND old.rule_code=current.rule_code AND old.pattern=current.pattern AND old.replacement=current.replacement),
            review_notes=(SELECT old.review_notes FROM skill_rules old WHERE old.skill_version_id=?
                AND old.rule_code=current.rule_code AND old.pattern=current.pattern AND old.replacement=current.replacement),
            enabled=(SELECT old.enabled FROM skill_rules old WHERE old.skill_version_id=?
                AND old.rule_code=current.rule_code AND old.pattern=current.pattern AND old.replacement=current.replacement)
            WHERE current.skill_version_id=? AND current.review_status='awaiting_approval'
            AND EXISTS (SELECT 1 FROM skill_rules old WHERE old.skill_version_id=?
                AND old.rule_code=current.rule_code AND old.pattern=current.pattern AND old.replacement=current.replacement
                AND old.review_status IN ('approved','rejected'))""",
            (previous["id"], previous["id"], previous["id"], candidate["id"], previous["id"]),
        )
    connection.commit()


def _seed_postgresql_type_revision(connection) -> None:
    """Create a review candidate for generalized character and integer types."""
    skill = connection.execute(
        "SELECT id FROM migration_skills WHERE source_dialect='sybase_asa' AND target_dialect='postgresql'"
    ).fetchone()
    if skill is None:
        return
    existing = connection.execute(
        """SELECT sv.id FROM skill_versions sv JOIN skill_rules sr ON sr.skill_version_id=sv.id
        WHERE sv.skill_id=? AND sr.rule_code='asa-pg-int' AND sv.status IN ('awaiting_approval','active')
        LIMIT 1""", (skill["id"],)
    ).fetchone()
    if existing:
        return
    base = connection.execute(
        """SELECT * FROM skill_versions WHERE skill_id=? AND status='active'
        ORDER BY version DESC LIMIT 1""", (skill["id"],)
    ).fetchone()
    if base is None:
        return
    next_version = connection.execute(
        "SELECT COALESCE(MAX(version),0)+1 FROM skill_versions WHERE skill_id=?", (skill["id"],)
    ).fetchone()[0]
    cursor = connection.execute(
        """INSERT INTO skill_versions(skill_id,version,status,instructions,created_at,created_by)
        VALUES (?,?,'awaiting_approval',?,?,'system-type-revision')""",
        (skill["id"], next_version, base["instructions"], now()),
    )
    new_id = cursor.lastrowid
    connection.execute(
        """INSERT INTO skill_rules(skill_version_id,rule_code,priority,pattern,replacement,enabled,
        description,source_example,target_example,risk_level,category,review_status,review_notes,is_custom)
        SELECT ?,rule_code,priority,pattern,replacement,enabled,description,source_example,target_example,
        risk_level,category,review_status,review_notes,is_custom FROM skill_rules WHERE skill_version_id=?""",
        (new_id, base["id"]),
    )
    revisions = [
        (r"\bVARCHAR\s*\(\s*(?:\*|\d+)\s*\)", "Convert ASA VARCHAR with any explicit length or * to PostgreSQL TEXT.", "VARCHAR(50)", "asa-pg-varchar-star"),
        (r"\bCHAR\s*\(\s*(?:\*|\d+)\s*\)", "Convert ASA CHAR with any explicit length or * to PostgreSQL TEXT.", "CHAR(20)", "asa-pg-char-star"),
    ]
    connection.executemany(
        """UPDATE skill_rules SET pattern=?,description=?,source_example=?,review_status='awaiting_approval',
        review_notes='Pattern expanded to cover numeric lengths; reapproval required.'
        WHERE skill_version_id=? AND rule_code=?""",
        [(pattern, description, example, new_id, code) for pattern,description,example,code in revisions],
    )
    connection.execute(
        """INSERT INTO skill_rules(skill_version_id,rule_code,priority,pattern,replacement,description,
        source_example,target_example,risk_level,category,review_status)
        VALUES (?,'asa-pg-int',285,?, 'INTEGER', ?, 'INT', 'INTEGER', 'low', 'datatypes', 'awaiting_approval')""",
        (new_id, r"\bINT\b", "Convert the ASA INT datatype keyword to PostgreSQL INTEGER."),
    )
    connection.commit()


def _seed_global_variable_rule(connection) -> None:
    """Add the gi_* accessor rule to the current candidate or a cloned revision."""
    skill = connection.execute(
        "SELECT id FROM migration_skills WHERE source_dialect='sybase_asa' AND target_dialect='postgresql'"
    ).fetchone()
    if skill is None:
        return
    existing = connection.execute(
        """SELECT 1 FROM skill_versions sv JOIN skill_rules sr ON sr.skill_version_id=sv.id
        WHERE sv.skill_id=? AND sr.rule_code='asa-pg-global-int-variable'
        AND sv.status IN ('awaiting_approval','active') LIMIT 1""", (skill["id"],)
    ).fetchone()
    if existing:
        return
    candidate = connection.execute(
        "SELECT * FROM skill_versions WHERE skill_id=? AND status='awaiting_approval' ORDER BY version DESC LIMIT 1",
        (skill["id"],),
    ).fetchone()
    if candidate is None:
        base = connection.execute(
            "SELECT * FROM skill_versions WHERE skill_id=? AND status='active' ORDER BY version DESC LIMIT 1",
            (skill["id"],),
        ).fetchone()
        if base is None:
            return
        next_version = connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM skill_versions WHERE skill_id=?", (skill["id"],)
        ).fetchone()[0]
        cursor = connection.execute(
            """INSERT INTO skill_versions(skill_id,version,status,instructions,created_at,created_by)
            VALUES (?,?,'awaiting_approval',?,?,'system-global-variable-revision')""",
            (skill["id"], next_version, base["instructions"], now()),
        )
        candidate_id = cursor.lastrowid
        connection.execute(
            """INSERT INTO skill_rules(skill_version_id,rule_code,priority,pattern,replacement,enabled,
            description,source_example,target_example,risk_level,category,review_status,review_notes,is_custom)
            SELECT ?,rule_code,priority,pattern,replacement,enabled,description,source_example,target_example,
            risk_level,category,review_status,review_notes,is_custom FROM skill_rules WHERE skill_version_id=?""",
            (candidate_id, base["id"]),
        )
    else:
        candidate_id = candidate["id"]
    connection.execute(
        """INSERT INTO skill_rules(skill_version_id,rule_code,priority,pattern,replacement,description,
        source_example,target_example,risk_level,category,review_status)
        VALUES (?,'asa-pg-global-int-variable',290,?,?,?,? ,?,'medium','parameters_variables','awaiting_approval')""",
        (candidate_id, r"\b(gi_[A-Za-z_][A-Za-z0-9_]*)\b", r"dba.get_int_var('\1')",
         "Resolve ASA gi_* global integer variables through the PostgreSQL accessor function.",
         "gi_language", "dba.get_int_var('gi_language')"),
    )
    connection.commit()


def _seed_schema_qualification_rule(connection) -> None:
    """Add a reviewable structural rule for the project's dba schema."""
    skill = connection.execute(
        "SELECT id FROM migration_skills WHERE source_dialect='sybase_asa' AND target_dialect='postgresql'"
    ).fetchone()
    if skill is None:
        return
    existing = connection.execute(
        """SELECT 1 FROM skill_versions sv JOIN skill_rules sr ON sr.skill_version_id=sv.id
        WHERE sv.skill_id=? AND sr.rule_code='asa-pg-schema-qualification'
        AND sv.status IN ('awaiting_approval','active') LIMIT 1""", (skill["id"],)
    ).fetchone()
    if existing:
        return
    candidate = connection.execute(
        "SELECT * FROM skill_versions WHERE skill_id=? AND status='awaiting_approval' ORDER BY version DESC LIMIT 1",
        (skill["id"],),
    ).fetchone()
    if candidate is None:
        base = connection.execute(
            "SELECT * FROM skill_versions WHERE skill_id=? AND status='active' ORDER BY version DESC LIMIT 1",
            (skill["id"],),
        ).fetchone()
        if base is None:
            return
        next_version = connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM skill_versions WHERE skill_id=?", (skill["id"],)).fetchone()[0]
        cursor = connection.execute(
            """INSERT INTO skill_versions(skill_id,version,status,instructions,created_at,created_by)
            VALUES (?,?,'awaiting_approval',?,?,'system-schema-revision')""",
            (skill["id"], next_version, base["instructions"], now()),
        )
        candidate_id = cursor.lastrowid
        connection.execute(
            """INSERT INTO skill_rules(skill_version_id,rule_code,priority,pattern,replacement,enabled,
            description,source_example,target_example,risk_level,category,review_status,review_notes,is_custom)
            SELECT ?,rule_code,priority,pattern,replacement,enabled,description,source_example,target_example,
            risk_level,category,review_status,review_notes,is_custom FROM skill_rules WHERE skill_version_id=?""",
            (candidate_id, base["id"]),
        )
    else:
        candidate_id = candidate["id"]
    connection.execute(
        """INSERT INTO skill_rules(skill_version_id,rule_code,priority,pattern,replacement,description,
        source_example,target_example,risk_level,category,review_status)
        VALUES (?,'asa-pg-schema-qualification',900,?,?,?, ?,?,'medium','schema_qualification','awaiting_approval')""",
        (candidate_id, r"\b(?:FROM|JOIN|UPDATE|INTO|DELETE\s+FROM)\b|\b[A-Za-z_]\w*\s*\(", "dba",
         "Qualify unqualified table names and internal routine calls with the approved PostgreSQL schema.",
         "SELECT wa_someone_get_it(report_code) FROM tablename1 a JOIN tablename2 b",
         "SELECT dba.wa_someone_get_it(report_code) FROM dba.tablename1 a JOIN dba.tablename2 b"),
    )
    connection.commit()


def _seed_builtin_function_rules(connection) -> None:
    """Add STRING and LEN conversions without treating them as schema routines."""
    skill = connection.execute(
        "SELECT id FROM migration_skills WHERE source_dialect='sybase_asa' AND target_dialect='postgresql'"
    ).fetchone()
    if skill is None:
        return
    existing = connection.execute(
        """SELECT 1 FROM skill_versions sv JOIN skill_rules sr ON sr.skill_version_id=sv.id
        WHERE sv.skill_id=? AND sr.rule_code='asa-pg-string-concat'
        AND sv.status IN ('awaiting_approval','active') LIMIT 1""", (skill["id"],)
    ).fetchone()
    if existing:
        return
    candidate = connection.execute(
        "SELECT * FROM skill_versions WHERE skill_id=? AND status='awaiting_approval' ORDER BY version DESC LIMIT 1",
        (skill["id"],),
    ).fetchone()
    if candidate is None:
        base = connection.execute(
            "SELECT * FROM skill_versions WHERE skill_id=? AND status='active' ORDER BY version DESC LIMIT 1",
            (skill["id"],),
        ).fetchone()
        if base is None:
            return
        next_version = connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM skill_versions WHERE skill_id=?", (skill["id"],)).fetchone()[0]
        cursor = connection.execute(
            """INSERT INTO skill_versions(skill_id,version,status,instructions,created_at,created_by)
            VALUES (?,?,'awaiting_approval',?,?,'system-builtin-revision')""",
            (skill["id"], next_version, base["instructions"], now()),
        )
        candidate_id = cursor.lastrowid
        connection.execute(
            """INSERT INTO skill_rules(skill_version_id,rule_code,priority,pattern,replacement,enabled,
            description,source_example,target_example,risk_level,category,review_status,review_notes,is_custom)
            SELECT ?,rule_code,priority,pattern,replacement,enabled,description,source_example,target_example,
            risk_level,category,review_status,review_notes,is_custom FROM skill_rules WHERE skill_version_id=?""",
            (candidate_id, base["id"]),
        )
        candidate = {"id": candidate_id}
    rules = [
        ("asa-pg-string-concat", 175, r"\bSTRING\s*\(", "CONCAT(",
         "Convert the ASA STRING concatenation function to PostgreSQL CONCAT.",
         "STRING('REPORT_', mode_id)", "CONCAT('REPORT_', mode_id)", "medium"),
        ("asa-pg-len", 176, r"\bLEN\s*\(", "LENGTH(",
         "Convert the ASA LEN function to PostgreSQL LENGTH.", "LEN(value)", "LENGTH(value)", "low"),
    ]
    connection.executemany(
        """INSERT INTO skill_rules(skill_version_id,rule_code,priority,pattern,replacement,description,
        source_example,target_example,risk_level,category,review_status)
        VALUES (?,?,?,?,?,?,?,?,?,'string_functions','awaiting_approval')""",
        [(candidate["id"], *rule) for rule in rules],
    )
    connection.execute(
        """UPDATE skill_rules SET pattern=?,description=?,review_status='awaiting_approval',
        review_notes='Routine qualification narrowed to custom names containing an underscore.'
        WHERE skill_version_id=? AND rule_code='asa-pg-schema-qualification'""",
        (r"\b(?:FROM|JOIN|UPDATE|INTO|DELETE\s+FROM)\b|\b[A-Za-z_]\w*_[A-Za-z0-9_]*\s*\(",
         "Qualify table references and custom internal routine names containing an underscore; never qualify SQL keywords or built-ins.",
         candidate["id"]),
    )
    connection.commit()


def _seed_table_alias_rule(connection) -> None:
    """Add a client-configurable, reviewable table-alias policy."""
    skill = connection.execute(
        "SELECT id FROM migration_skills WHERE source_dialect='sybase_asa' AND target_dialect='postgresql'"
    ).fetchone()
    if skill is None:
        return
    existing = connection.execute(
        """SELECT 1 FROM skill_versions sv JOIN skill_rules sr ON sr.skill_version_id=sv.id
        WHERE sv.skill_id=? AND sr.rule_code='asa-pg-table-aliases'
        AND sv.status IN ('awaiting_approval','active') LIMIT 1""", (skill["id"],)
    ).fetchone()
    if existing:
        return
    candidate = connection.execute(
        "SELECT * FROM skill_versions WHERE skill_id=? AND status='awaiting_approval' ORDER BY version DESC LIMIT 1",
        (skill["id"],),
    ).fetchone()
    if candidate is None:
        base = connection.execute(
            "SELECT * FROM skill_versions WHERE skill_id=? AND status='active' ORDER BY version DESC LIMIT 1",
            (skill["id"],),
        ).fetchone()
        if base is None:
            return
        version = connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM skill_versions WHERE skill_id=?", (skill["id"],)
        ).fetchone()[0]
        cursor = connection.execute(
            """INSERT INTO skill_versions(skill_id,version,status,instructions,created_at,created_by)
            VALUES (?,?,'awaiting_approval',?,?,'system-table-alias-revision')""",
            (skill["id"], version, base["instructions"], now()),
        )
        candidate_id = cursor.lastrowid
        connection.execute(
            """INSERT INTO skill_rules(skill_version_id,rule_code,priority,pattern,replacement,enabled,
            description,source_example,target_example,risk_level,category,review_status,review_notes,is_custom)
            SELECT ?,rule_code,priority,pattern,replacement,enabled,description,source_example,target_example,
            risk_level,category,review_status,review_notes,is_custom FROM skill_rules WHERE skill_version_id=?""",
            (candidate_id, base["id"]),
        )
    else:
        candidate_id = candidate["id"]
    policy = json.dumps({"alias_length": 3, "aliases": {"report_items": "ret"}}, separators=(",", ":"))
    connection.execute(
        """INSERT INTO skill_rules(skill_version_id,rule_code,priority,pattern,replacement,description,
        source_example,target_example,risk_level,category,review_status)
        VALUES (?,'asa-pg-table-aliases',910,?,?,?,?,?,'medium','table_aliasing','awaiting_approval')""",
        (candidate_id, "FROM/JOIN table references and table.column qualifiers", policy,
         "Add PostgreSQL table aliases and replace source table-name column qualifiers. Replacement is editable JSON: alias_length controls fallback names; aliases stores client-specific overrides.",
         "SELECT report_items.id FROM dba.report_items",
         "SELECT ret.id FROM dba.report_items AS ret"),
    )
    connection.commit()


def list_skill_versions(path: Path = DATABASE_PATH) -> list[dict]:
    with closing(connect(path)) as db:
        rows = db.execute(
            """SELECT sv.*,ms.name,ms.source_dialect,ms.target_dialect,
            (SELECT COUNT(*) FROM skill_rules sr WHERE sr.skill_version_id=sv.id AND sr.enabled=1) AS rule_count
            FROM skill_versions sv JOIN migration_skills ms ON ms.id=sv.skill_id
            ORDER BY CASE sv.status WHEN 'awaiting_approval' THEN 0 WHEN 'draft' THEN 1 WHEN 'active' THEN 2 ELSE 3 END,
            CASE WHEN ms.source_dialect='sybase_asa' AND ms.target_dialect='postgresql' THEN 0 ELSE 1 END,
            ms.name,sv.version DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def get_skill_version_rules(version_id: int, path: Path = DATABASE_PATH) -> list[dict]:
    with closing(connect(path)) as db:
        rows = db.execute(
            "SELECT * FROM skill_rules WHERE skill_version_id=? ORDER BY priority,id", (version_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def approve_skill_version(version_id: int, reviewer: str, path: Path = DATABASE_PATH) -> None:
    if not reviewer.strip():
        raise ValueError("Enter the approver name.")
    with closing(connect(path)) as db, db:
        version = db.execute("SELECT * FROM skill_versions WHERE id=?", (version_id,)).fetchone()
        if version is None or version["status"] != "awaiting_approval":
            raise ValueError("Select a skill version awaiting approval.")
        pending = db.execute("SELECT COUNT(*) FROM skill_rules WHERE skill_version_id=? AND review_status='awaiting_approval'", (version_id,)).fetchone()[0]
        approved = db.execute("SELECT COUNT(*) FROM skill_rules WHERE skill_version_id=? AND review_status='approved' AND enabled=1", (version_id,)).fetchone()[0]
        if pending:
            raise ValueError(f"Review every rule first. {pending} rule(s) are awaiting approval.")
        if not approved:
            raise ValueError("Approve at least one enabled rule before activating this skill.")
        db.execute(
            "UPDATE skill_versions SET status='superseded' WHERE skill_id=? AND status='active'",
            (version["skill_id"],),
        )
        db.execute(
            "UPDATE skill_versions SET status='active',approved_by=?,approved_at=? WHERE id=?",
            (reviewer.strip(), now(), version_id),
        )


def update_skill_rule(rule_id: int, priority: int, pattern: str, replacement: str,
                      description: str, source_example: str, target_example: str,
                      risk_level: str, enabled: bool, category="general", review_notes="",
                      path: Path = DATABASE_PATH) -> None:
    if not pattern:
        raise ValueError("A match pattern is required.")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regular expression: {exc}") from exc
    if risk_level not in {"low", "medium", "high"}:
        raise ValueError("Risk level must be low, medium, or high.")
    with closing(connect(path)) as db, db:
        row = db.execute(
            """SELECT sv.status FROM skill_rules sr JOIN skill_versions sv ON sv.id=sr.skill_version_id
            WHERE sr.id=?""", (rule_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Skill rule not found.")
        if row["status"] not in {"draft", "awaiting_approval"}:
            raise ValueError("Only draft or awaiting-approval skill rules can be edited.")
        db.execute(
            """UPDATE skill_rules SET priority=?,pattern=?,replacement=?,description=?,source_example=?,
            target_example=?,risk_level=?,enabled=?,category=?,review_notes=?,review_status='awaiting_approval' WHERE id=?""",
            (priority, pattern, replacement, description, source_example, target_example,
             risk_level, int(enabled), category, review_notes, rule_id),
        )


def review_skill_rule(rule_id: int, decision: str, notes: str, path: Path = DATABASE_PATH) -> None:
    if decision not in {"approved", "rejected"}:
        raise ValueError("Decision must be approved or rejected.")
    with closing(connect(path)) as db, db:
        row = db.execute("""SELECT sv.status FROM skill_rules sr JOIN skill_versions sv ON sv.id=sr.skill_version_id WHERE sr.id=?""", (rule_id,)).fetchone()
        if row is None or row["status"] not in {"draft", "awaiting_approval"}:
            raise ValueError("This rule is not reviewable.")
        db.execute("UPDATE skill_rules SET review_status=?,review_notes=?,enabled=? WHERE id=?",
                   (decision, notes, int(decision == "approved"), rule_id))


def add_custom_skill_rule(version_id: int, rule_code: str, category: str, priority: int,
                          pattern: str, replacement: str, description: str,
                          source_example: str, target_example: str, risk_level: str,
                          path: Path = DATABASE_PATH) -> int:
    if not rule_code.strip() or not pattern:
        raise ValueError("Rule code and match pattern are required.")
    re.compile(pattern)
    with closing(connect(path)) as db, db:
        version = db.execute("SELECT status FROM skill_versions WHERE id=?", (version_id,)).fetchone()
        if version is None or version["status"] not in {"draft", "awaiting_approval"}:
            raise ValueError("Custom rules require a draft or awaiting-approval version.")
        cursor = db.execute("""INSERT INTO skill_rules
            (skill_version_id,rule_code,category,priority,pattern,replacement,description,source_example,target_example,risk_level,review_status,is_custom)
            VALUES (?,?,?,?,?,?,?,?,?,?,'awaiting_approval',1)""",
            (version_id,rule_code.strip(),category,priority,pattern,replacement,description,source_example,target_example,risk_level))
        return cursor.lastrowid


def get_masking_rules(path: Path = DATABASE_PATH) -> list[dict]:
    with closing(connect(path)) as db:
        rows = db.execute("SELECT * FROM masking_rules WHERE enabled=1 ORDER BY sort_order").fetchall()
    return [dict(row) for row in rows]


def get_unmasking_rule(dialect: str, path: Path = DATABASE_PATH) -> dict:
    with closing(connect(path)) as db:
        row = db.execute("SELECT * FROM unmasking_rules WHERE dialect=?", (dialect,)).fetchone()
        if row is None:
            row = db.execute("SELECT * FROM unmasking_rules WHERE dialect='generic'").fetchone()
    return dict(row)


def get_migration_skill(source_dialect: str, target_dialect: str, path: Path = DATABASE_PATH):
    with closing(connect(path)) as db:
        row = db.execute(
            "SELECT * FROM migration_skills WHERE source_dialect=? AND target_dialect=? AND enabled=1",
            (source_dialect, target_dialect),
        ).fetchone()
    return dict(row) if row else None


def get_active_skill_version(source_dialect: str, target_dialect: str, path: Path = DATABASE_PATH):
    with closing(connect(path)) as db:
        version = db.execute(
            """SELECT sv.*, ms.name, ms.source_dialect, ms.target_dialect
            FROM skill_versions sv JOIN migration_skills ms ON ms.id=sv.skill_id
            WHERE ms.source_dialect=? AND ms.target_dialect=? AND ms.enabled=1 AND sv.status='active'
            ORDER BY sv.version DESC LIMIT 1""", (source_dialect, target_dialect)
        ).fetchone()
        if version is None:
            return None
        rules = db.execute(
            "SELECT * FROM skill_rules WHERE skill_version_id=? AND enabled=1 AND review_status='approved' ORDER BY priority,id",
            (version["id"],),
        ).fetchall()
    value = dict(version)
    value["rules"] = [dict(rule) for rule in rules]
    return value


def record_deployment_attempt(processing_run_id, project_id, skill_version_id, sql, status,
                              sqlstate=None, error_message=None, error_position=None,
                              path: Path = DATABASE_PATH) -> int:
    with closing(connect(path)) as db, db:
        cursor = db.execute(
            """INSERT INTO deployment_attempts
            (processing_run_id,project_id,skill_version_id,target_dialect,deployed_sql,status,sqlstate,error_message,error_position,attempted_at)
            VALUES (?,?,?,'postgresql',?,?,?,?,?,?)""",
            (processing_run_id, project_id, skill_version_id, sql, status, sqlstate, error_message, error_position, now()),
        )
        return cursor.lastrowid


def create_change_proposal(attempt_id: int, skill_version_id: int, title: str, rationale: str,
                           path: Path = DATABASE_PATH) -> int:
    with closing(connect(path)) as db, db:
        cursor = db.execute(
            """INSERT INTO skill_change_proposals
            (deployment_attempt_id,base_skill_version_id,title,rationale,proposed_at)
            VALUES (?,?,?,?,?)""", (attempt_id, skill_version_id, title, rationale, now())
        )
        return cursor.lastrowid


def list_change_proposals(path: Path = DATABASE_PATH) -> list[dict]:
    with closing(connect(path)) as db:
        rows = db.execute(
            """SELECT p.*, ms.name AS skill_name, sv.version AS base_version
            FROM skill_change_proposals p
            JOIN skill_versions sv ON sv.id=p.base_skill_version_id
            JOIN migration_skills ms ON ms.id=sv.skill_id ORDER BY p.proposed_at DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


def update_proposal_rule(proposal_id: int, pattern: str, replacement: str, reviewer: str,
                         path: Path = DATABASE_PATH) -> None:
    with closing(connect(path)) as db, db:
        proposal = db.execute(
            """SELECT p.*,d.deployed_sql FROM skill_change_proposals p
            JOIN deployment_attempts d ON d.id=p.deployment_attempt_id WHERE p.id=?""", (proposal_id,)
        ).fetchone()
        if proposal is None:
            raise ValueError("Correction proposal not found.")
        try:
            corrected, count = re.subn(pattern, replacement, proposal["deployed_sql"], flags=re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid correction pattern: {exc}") from exc
        if count == 0 or corrected == proposal["deployed_sql"]:
            raise ValueError("The correction rule did not change the failed SQL.")
        db.execute(
            """UPDATE skill_change_proposals SET pattern=?,replacement=?,status='awaiting_approval',
            test_status='passed',reviewed_by=?,reviewed_at=? WHERE id=?""",
            (pattern, replacement, reviewer, now(), proposal_id),
        )
        db.execute(
            "INSERT INTO skill_regression_results(proposal_id,test_name,passed,details,tested_at) VALUES (?,?,?,?,?)",
            (proposal_id, "correction pattern application", 1, f"Rule changed {count} occurrence(s).", now()),
        )


def approve_change_proposal(proposal_id: int, reviewer: str, path: Path = DATABASE_PATH) -> int:
    with closing(connect(path)) as db, db:
        proposal = db.execute("SELECT * FROM skill_change_proposals WHERE id=?", (proposal_id,)).fetchone()
        if proposal is None or proposal["status"] != "awaiting_approval" or proposal["test_status"] != "passed":
            raise ValueError("The proposal must be tested and awaiting approval.")
        base = db.execute("SELECT * FROM skill_versions WHERE id=?", (proposal["base_skill_version_id"],)).fetchone()
        next_version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM skill_versions WHERE skill_id=?", (base["skill_id"],)).fetchone()[0]
        cursor = db.execute(
            """INSERT INTO skill_versions
            (skill_id,version,status,instructions,created_at,created_by,approved_by,approved_at)
            VALUES (?,?,'active',?,?,?,?,?)""",
            (base["skill_id"], next_version, base["instructions"], now(), reviewer, reviewer, now()),
        )
        new_id = cursor.lastrowid
        old_rules = db.execute("SELECT * FROM skill_rules WHERE skill_version_id=?", (base["id"],)).fetchall()
        db.executemany(
            """INSERT INTO skill_rules(skill_version_id,rule_code,priority,pattern,replacement,enabled,
            description,source_example,target_example,risk_level,category,review_status,review_notes,is_custom)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(new_id, row["rule_code"], row["priority"], row["pattern"], row["replacement"], row["enabled"],
              row["description"], row["source_example"], row["target_example"], row["risk_level"],row["category"],
              row["review_status"],row["review_notes"],row["is_custom"]) for row in old_rules],
        )
        max_priority = max((row["priority"] for row in old_rules), default=0)
        db.execute(
            """INSERT INTO skill_rules(skill_version_id,rule_code,priority,pattern,replacement,category,
            review_status,review_notes,is_custom) VALUES (?,?,?,?,?,'custom','approved',?,1)""",
            (new_id, f"proposal-{proposal_id}", max_priority + 1, proposal["pattern"], proposal["replacement"], proposal["rationale"]),
        )
        db.execute("UPDATE skill_versions SET status='superseded' WHERE id=?", (base["id"],))
        db.execute(
            """UPDATE skill_change_proposals SET status='approved',reviewed_by=?,reviewed_at=?,resulting_skill_version_id=?
            WHERE id=?""", (reviewer, now(), new_id, proposal_id)
        )
        return new_id


def fetch_projects(path: Path = DATABASE_PATH) -> list[dict]:
    with closing(connect(path)) as db, db:
        rows = db.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    return [{**dict(row), "database": row["database_name"]} for row in rows]


def upsert_projects(projects, path: Path = DATABASE_PATH) -> None:
    with closing(connect(path)) as db, db:
        for project in projects:
            value = asdict(project)
            db.execute(
                """INSERT INTO projects
                (id,name,source_database,target_database,host,port,database_name,username,created_at,archive_path,workspace,default_operation,object_scope,input_type,target_host,target_port,target_database_name,target_username,formatter_indent)
                VALUES (:id,:name,:source_database,:target_database,:host,:port,:database,:username,:created_at,:archive_path,:workspace,:default_operation,:object_scope,:input_type,:target_host,:target_port,:target_database_name,:target_username,:formatter_indent)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, source_database=excluded.source_database,
                target_database=excluded.target_database, host=excluded.host, port=excluded.port,
                database_name=excluded.database_name, username=excluded.username,
                archive_path=excluded.archive_path, workspace=excluded.workspace,
                default_operation=excluded.default_operation, object_scope=excluded.object_scope,
                input_type=excluded.input_type,target_host=excluded.target_host,target_port=excluded.target_port,
                target_database_name=excluded.target_database_name,target_username=excluded.target_username,
                formatter_indent=excluded.formatter_indent""",
                value,
            )


def delete_project(project_id: str, path: Path = DATABASE_PATH) -> None:
    """Delete one project and its dependent upload/object records."""
    with closing(connect(path)) as db, db:
        db.execute("DELETE FROM projects WHERE id=?", (project_id,))


def record_upload(project, files: list[Path], path: Path = DATABASE_PATH) -> None:
    root = Path(project.workspace)
    with closing(connect(path)) as db, db:
        db.execute(
            "INSERT INTO uploads(project_id,archive_path,uploaded_at,file_count) VALUES (?,?,?,?)",
            (project.id, project.archive_path, now(), len(files)),
        )
        db.execute("DELETE FROM project_objects WHERE project_id=?", (project.id,))
        db.executemany(
            "INSERT INTO project_objects(project_id,relative_path,absolute_path,selected,discovered_at) VALUES (?,?,?,?,?)",
            [(project.id, str(file.relative_to(root)), str(file), 1, now()) for file in files],
        )


def save_object_selection(project_id: str, selected_paths: list[Path], path: Path = DATABASE_PATH) -> None:
    selected = {str(item.resolve()) for item in selected_paths}
    with closing(connect(path)) as db, db:
        rows = db.execute("SELECT id,absolute_path FROM project_objects WHERE project_id=?", (project_id,)).fetchall()
        db.executemany(
            "UPDATE project_objects SET selected=? WHERE id=?",
            [(int(str(Path(row["absolute_path"]).resolve()) in selected), row["id"]) for row in rows],
        )


def record_processing(project_id: str | None, object_path: str, operation: str, dialect: str,
                      input_ddl: str, output_ddl: str, mapping=None, status="completed", error_message=None,
                      path: Path = DATABASE_PATH, source_dialect=None, target_dialect=None,
                      migration_skill_id=None, skill_version_id=None, target_object_type=None,
                      classification_reason=None, skill_trace=None, classification_rule=None,
                      human_override=None, routine_analysis=None, routine_language=None,
                      technical_status="success", review_status="pending_review", diagnostics=None) -> int:
    with closing(connect(path)) as db, db:
        cursor = db.execute(
            """INSERT INTO processing_runs
            (project_id,object_path,operation,dialect,input_ddl,output_ddl,mapping_json,status,error_message,processed_at,source_dialect,target_dialect,migration_skill_id,skill_version_id,target_object_type,classification_reason,skill_trace_json,classification_rule,human_override,routine_analysis_json,routine_language,technical_status,review_status,diagnostics_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (project_id, object_path, operation, dialect, input_ddl, output_ddl,
             json.dumps(mapping, sort_keys=True) if mapping is not None else None,
             status, error_message, now(), source_dialect or dialect, target_dialect or dialect,
             migration_skill_id, skill_version_id, target_object_type, classification_reason,
             json.dumps(skill_trace, sort_keys=True) if skill_trace is not None else None,
             classification_rule, human_override,
             json.dumps(routine_analysis, sort_keys=True) if routine_analysis is not None else None,
             routine_language, technical_status, review_status,
             json.dumps(diagnostics or [], sort_keys=True)),
        )
        return cursor.lastrowid


def get_processing_run(run_id: int, path: Path = DATABASE_PATH) -> dict | None:
    with closing(connect(path)) as db:
        row = db.execute("SELECT * FROM processing_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result['diagnostics'] = json.loads(result.get('diagnostics_json') or '[]')
    return result


def set_processing_review(run_id: int, decision: str, reviewer: str = "", notes: str = "",
                          path: Path = DATABASE_PATH) -> None:
    """Apply a human review decision while preventing approval with unresolved errors."""
    allowed = {'pending_review', 'needs_modification', 'approved', 'rejected'}
    if decision not in allowed:
        raise ValueError(f"Unsupported routine review status: {decision}")
    if decision in {'approved', 'rejected'} and not reviewer.strip():
        raise ValueError("Enter the reviewer name.")
    with closing(connect(path)) as db, db:
        row = db.execute(
            "SELECT technical_status,diagnostics_json FROM processing_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Migration run not found.")
        diagnostics = json.loads(row['diagnostics_json'] or '[]')
        unresolved_errors = [item for item in diagnostics
                             if item.get('severity') == 'error' and not item.get('resolved')]
        if decision == 'approved' and unresolved_errors:
            raise ValueError(
                f"Cannot approve: {len(unresolved_errors)} unresolved migration error(s) remain."
            )
        db.execute(
            """UPDATE processing_runs
               SET review_status=?,reviewed_by=?,reviewed_at=?,review_notes=?
               WHERE id=?""",
            (decision, reviewer.strip() or None, now() if reviewer.strip() else None, notes.strip(), run_id),
        )


def latest_mapping(project_id: str, object_path: str = "", path: Path = DATABASE_PATH):
    """Return the newest stored mapping for the object, then fall back to its project."""
    with closing(connect(path)) as db, db:
        row = None
        if object_path:
            row = db.execute(
                """SELECT mapping_json FROM processing_runs
                WHERE project_id=? AND object_path=? AND mapping_json IS NOT NULL
                ORDER BY processed_at DESC, id DESC LIMIT 1""", (project_id, object_path)
            ).fetchone()
        if row is None:
            row = db.execute(
                """SELECT mapping_json FROM processing_runs
                WHERE project_id=? AND mapping_json IS NOT NULL
                ORDER BY processed_at DESC, id DESC LIMIT 1""", (project_id,)
            ).fetchone()
    return json.loads(row["mapping_json"]) if row else None
