"""Project persistence, archive handling, and database connection helpers."""

from __future__ import annotations

import re
import shutil
import tarfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


APP_DIR = Path.home() / ".ddl_masker"
WORKSPACE_DIR = APP_DIR / "workspaces"
SUPPORTED_DATABASES = ("SQL Anywhere ASA", "PostgreSQL", "SAP ASE", "Oracle", "SQL Server")


@dataclass
class Project:
    id: str
    name: str
    source_database: str
    target_database: str
    host: str
    port: int
    database: str
    username: str
    created_at: str
    archive_path: str = ""
    workspace: str = ""
    default_operation: str = "mask"
    object_scope: str = "all"
    input_type: str = "archive"
    target_host: str = "localhost"
    target_port: int = 5432
    target_database_name: str = ""
    target_username: str = ""
    formatter_indent: str = "4 spaces"

    @classmethod
    def from_dict(cls, value: dict) -> "Project":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value.get(key, "") for key in allowed})


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-").lower()
    return slug or "project"


def create_project(**details) -> Project:
    stamp = datetime.now(timezone.utc)
    project_id = f"{slugify(details['name'])}-{stamp.strftime('%Y%m%d%H%M%S%f')}"
    return Project(id=project_id, created_at=stamp.isoformat(), **details)


def load_projects(path=None) -> list[Project]:
    from database import DATABASE_PATH, fetch_projects
    return [Project.from_dict(item) for item in fetch_projects(path or DATABASE_PATH)]


def save_projects(projects: list[Project], path=None) -> None:
    from database import DATABASE_PATH, upsert_projects
    upsert_projects(projects, path or DATABASE_PATH)


def remove_project(project: Project, path=None, workspace_dir: Path = WORKSPACE_DIR) -> None:
    """Remove a project, its imported copies, and its persisted project record."""
    from database import DATABASE_PATH, delete_project
    clear_project_files(project, workspace_dir)
    delete_project(project.id, path or DATABASE_PATH)


def safe_extract_sql_archive(archive: Path, destination: Path) -> list[Path]:
    """Extract supported SQL files from ZIP/TAR without allowing path traversal."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    extracted: list[Path] = []
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            members = ((i.filename, i.is_dir(), lambda item=i: bundle.open(item)) for i in bundle.infolist())
            extracted.extend(_extract_members(members, root, destination))
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive, "r:*") as bundle:
            members = ((i.name, not i.isfile(), lambda item=i: bundle.extractfile(item)) for i in bundle.getmembers())
            extracted.extend(_extract_members(members, root, destination))
    else:
        raise ValueError("Select a valid ZIP, TAR, TAR.GZ, or TGZ archive.")
    return sorted(extracted, key=lambda item: str(item).lower())


def _extract_members(members, root: Path, destination: Path) -> list[Path]:
    extracted = []
    for name, is_directory, opener in members:
        if is_directory or Path(name).suffix.lower() not in {".sql", ".ddl", ".txt"}:
            continue
        target = (destination / name).resolve()
        if root not in target.parents:
            raise ValueError(f"Unsafe path in archive: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        source = opener()
        if source is None:
            continue
        with source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
        extracted.append(target)
    return extracted


def import_sql_files(files: list[Path], destination: Path) -> list[Path]:
    """Copy one or more loose SQL object files into a project's workspace."""
    destination.mkdir(parents=True, exist_ok=True)
    imported = []
    for index, source in enumerate(files, start=1):
        if source.suffix.lower() not in {".sql", ".ddl", ".txt"}:
            continue
        target = destination / source.name
        if target.exists() and target.resolve() != source.resolve():
            target = destination / f"{index}_{source.name}"
        shutil.copy2(source, target)
        imported.append(target.resolve())
    return imported


def list_project_files(project: Project) -> list[Path]:
    root = Path(project.workspace)
    if not root.exists():
        return []
    return sorted(
        (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".sql", ".ddl", ".txt"}),
        key=lambda item: str(item).lower(),
    )


def clear_project_files(project: Project, workspace_dir: Path = WORKSPACE_DIR) -> None:
    """Remove imported copies for one project without touching their source files."""
    expected = (Path(workspace_dir) / project.id).resolve()
    workspace = Path(project.workspace).resolve() if project.workspace else expected
    if workspace != expected:
        raise ValueError("Refusing to clear files outside this project's workspace.")
    if workspace.exists():
        shutil.rmtree(workspace)


def dialect_for(database: str) -> str:
    return {
        "PostgreSQL": "postgresql", "Oracle": "oracle", "SQL Server": "sqlserver",
        "SAP ASE": "sybase_ase", "SAP ASA": "sybase_asa", "SQL Anywhere ASA": "sybase_asa",
    }.get(database, "generic")


def test_database_connection(database_type: str, details: dict, password: str) -> None:
    """Open and immediately close a connection using an optional DB driver."""
    connectors: dict[str, Callable[[], object]] = {
        "PostgreSQL": lambda: _postgres_connection(details, password),
        "Oracle": lambda: _oracle_connection(details, password),
        "SQL Server": lambda: _sqlserver_connection(details, password),
        "SAP ASE": lambda: _sap_connection(details, password, "SAP ASE ODBC Driver"),
        "SAP ASA": lambda: _sap_connection(details, password, "SQL Anywhere 17"),
        "SQL Anywhere ASA": lambda: _sap_connection(details, password, "SQL Anywhere 17"),
    }
    connection = connectors[database_type]()
    connection.close()


def _postgres_connection(d, password):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("PostgreSQL driver missing. Install it with: pip install psycopg[binary]") from exc
    return psycopg.connect(host=d["host"], port=d["port"], dbname=d["database"], user=d["username"], password=password, connect_timeout=5)


def _oracle_connection(d, password):
    try:
        import oracledb
    except ImportError as exc:
        raise RuntimeError("Oracle driver missing. Install it with: pip install oracledb") from exc
    return oracledb.connect(user=d["username"], password=password, dsn=f"{d['host']}:{d['port']}/{d['database']}")


def _sqlserver_connection(d, password):
    try:
        import pyodbc
    except ImportError as exc:
        raise RuntimeError("SQL Server driver missing. Install pyodbc and an ODBC Driver for SQL Server.") from exc
    connection_string = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={d['host']},{d['port']};DATABASE={d['database']};UID={d['username']};PWD={password};"
        "Encrypt=yes;TrustServerCertificate=yes;Connection Timeout=5"
    )
    return pyodbc.connect(connection_string)


def _sap_connection(d, password, driver):
    try:
        import pyodbc
    except ImportError as exc:
        raise RuntimeError("SAP database connection requires pyodbc and the appropriate SAP ODBC driver.") from exc
    return pyodbc.connect(
        f"DRIVER={{{driver}}};HOST={d['host']}:{d['port']};DBN={d['database']};UID={d['username']};PWD={password};",
        timeout=5,
    )
