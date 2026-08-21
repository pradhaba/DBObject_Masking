"""PostgreSQL validation and controlled skill-correction capture."""

from __future__ import annotations

from database import create_change_proposal, record_deployment_attempt


def test_postgresql_deployment(project, sql: str, processing_run_id: int, skill_version_id: int, password: str):
    """Compile DDL inside a transaction and always roll it back."""
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("Install PostgreSQL support with: pip install psycopg[binary]") from exc

    connection = None
    try:
        connection = psycopg.connect(
            host=project.target_host, port=project.target_port,
            dbname=project.target_database_name, user=project.target_username,
            password=password, connect_timeout=5,
        )
        with connection.cursor() as cursor:
            cursor.execute(sql)
        connection.rollback()
        attempt_id = record_deployment_attempt(
            processing_run_id, project.id, skill_version_id, sql, "passed"
        )
        return {"passed": True, "attempt_id": attempt_id, "proposal_id": None}
    except Exception as exc:
        if connection is not None:
            connection.rollback()
        diag = getattr(exc, "diag", None)
        sqlstate = getattr(exc, "sqlstate", None)
        position = getattr(diag, "statement_position", None) if diag else None
        attempt_id = record_deployment_attempt(
            processing_run_id, project.id, skill_version_id, sql, "failed",
            sqlstate, str(exc), int(position) if position else None,
        )
        proposal_id = create_change_proposal(
            attempt_id, skill_version_id,
            f"PostgreSQL {sqlstate or 'validation'} correction",
            str(exc),
        )
        return {"passed": False, "attempt_id": attempt_id, "proposal_id": proposal_id,
                "sqlstate": sqlstate, "error": str(exc), "position": position}
    finally:
        if connection is not None:
            connection.close()
