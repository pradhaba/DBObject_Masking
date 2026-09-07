"""Versioned, section-aware SQL dialect language catalogue."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from routine_sections import RoutineDocument, RoutineSection, split_asa_routine


CATALOG_DIR = Path(__file__).resolve().parent / "skills" / "catalogs"
VALID_KINDS = {"datatype", "keyword", "function", "operator", "statement", "structural"}
VALID_MODES = {"regex", "renderer", "diagnostic"}
VALID_DISPOSITIONS = {
    "same", "rename", "signature_rewrite", "expression_rewrite",
    "statement_rewrite", "type_dependent", "emulation", "unsupported",
    "not_applicable",
}
VALID_SECTIONS = {
    "declaration", "parameters", "return_contract", "header_options",
    "declarations", "body", "exception_handlers",
}


def sync_bundled_catalogs(connection) -> None:
    """Load immutable JSON catalogue releases into SQLite once per version."""
    if not CATALOG_DIR.exists():
        return
    for path in sorted(CATALOG_DIR.glob("*.json")):
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        _validate_catalog(payload, path)
        source = payload["source_dialect"]
        target = payload["target_dialect"]
        version = int(payload["catalog_version"])
        digest = hashlib.sha256(raw).hexdigest()
        existing = connection.execute(
            """SELECT content_hash FROM language_catalog_releases
            WHERE source_dialect=? AND target_dialect=? AND catalog_version=?""",
            (source, target, version),
        ).fetchone()
        if existing:
            if existing["content_hash"] != digest:
                raise ValueError(
                    f"Catalogue {source} -> {target} v{version} changed after loading; "
                    "publish a new catalog_version instead."
                )
            continue
        for entry in payload["elements"]:
            connection.execute(
                """INSERT INTO language_elements
                (source_dialect,target_dialect,element_code,element_kind,disposition,source_pattern,
                 target_template,match_mode,renderer,section_scopes_json,priority,
                 risk_level,review_status,notes,catalog_version,enabled,source_version,
                 target_version,verification_status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (source, target, entry["code"], entry["kind"],
                 entry.get("disposition", "rename"), entry["source_pattern"],
                 entry.get("target_template", ""), entry.get("match_mode", "regex"),
                 entry.get("renderer", ""), json.dumps(entry.get("sections", [])),
                 entry.get("priority", 1000), entry.get("risk_level", "low"),
                 entry.get("review_status", "approved"), entry.get("notes", ""),
                 version, int(entry.get("enabled", True)),
                 payload.get("source_version", ""), payload.get("target_version", ""),
                 entry.get("verification_status", "verified")),
            )
        connection.execute(
            """INSERT INTO language_catalog_releases
            (source_dialect,target_dialect,catalog_version,content_hash,loaded_at)
            VALUES (?,?,?,?,datetime('now'))""", (source, target, version, digest),
        )
        connection.commit()


def get_language_elements(connection, source_dialect: str, target_dialect: str,
                          approved_only: bool = True) -> list[dict]:
    version = connection.execute(
        """SELECT MAX(catalog_version) FROM language_catalog_releases
        WHERE source_dialect=? AND target_dialect=?""", (source_dialect, target_dialect),
    ).fetchone()[0]
    if version is None:
        return []
    approval = "AND review_status='approved'" if approved_only else ""
    rows = connection.execute(
        f"""SELECT e.* FROM language_elements e WHERE e.source_dialect=? AND e.target_dialect=?
        AND e.catalog_version<=? AND e.enabled=1 {approval}
        AND e.catalog_version=(SELECT MAX(newer.catalog_version) FROM language_elements newer
            WHERE newer.source_dialect=e.source_dialect AND newer.target_dialect=e.target_dialect
            AND newer.element_code=e.element_code AND newer.catalog_version<=?)
        ORDER BY e.priority,e.id""",
        (source_dialect, target_dialect, version, version),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["sections"] = json.loads(item.pop("section_scopes_json"))
        result.append(item)
    return result


def apply_language_catalog(sql: str, elements: list[dict]) -> tuple[str, list[dict]]:
    """Apply approved lexical entries only within their declared routine sections."""
    document = split_asa_routine(sql)
    output = []
    trace = []
    for section in document.sections:
        value = section.text
        for entry in elements:
            if entry["match_mode"] != "regex" or section.kind not in entry["sections"]:
                continue
            value, count = _protected_subn(
                entry["source_pattern"], entry["target_template"], value
            )
            if count:
                trace.append({
                    "line": section.kind,
                    "source": entry["element_code"],
                    "output": entry["target_template"],
                    "rules": [{
                        "rule_id": entry["element_code"],
                        "rule_code": entry["element_code"],
                        "priority": entry["priority"],
                        "matches": count,
                    }],
                })
        output.append(value)
    return "".join(output), trace


def catalog_coverage(sql: str, elements: list[dict]) -> list[dict]:
    """Report known constructs that require a renderer or human decision."""
    document = split_asa_routine(sql)
    issues = []
    for section in document.sections:
        for entry in elements:
            if section.kind not in entry["sections"] or entry["match_mode"] == "regex":
                continue
            if re.search(entry["source_pattern"], section.text, re.I | re.M):
                issues.append({
                    "code": entry["element_code"],
                    "kind": entry["element_kind"],
                    "section": section.kind,
                    "handling": entry["match_mode"],
                    "renderer": entry["renderer"],
                    "risk_level": entry["risk_level"],
                    "notes": entry["notes"],
                })
    return issues


def preview_language_element(entry: dict, source: str) -> tuple[str, int, str]:
    """Preview one catalogue entry without changing database state."""
    if entry["match_mode"] == "regex":
        converted, count = _protected_subn(
            entry["source_pattern"], entry["target_template"], source
        )
        return converted, count, "Safe token-aware mapping preview."
    handler = entry.get("renderer") or entry["match_mode"]
    count = len(re.findall(entry["source_pattern"], source, re.I | re.M))
    return source, count, f"Requires {handler}; no blind replacement was performed."


def _protected_subn(pattern: str, replacement: str, text: str) -> tuple[str, int]:
    """Regex substitution outside strings, quoted identifiers, and comments."""
    protected = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\r\n]*|/\*.*?\*/", re.S)
    parts, at, count = [], 0, 0
    for match in protected.finditer(text):
        converted, hits = re.subn(pattern, replacement, text[at:match.start()], flags=re.I | re.M)
        parts.extend((converted, match.group(0)))
        count += hits
        at = match.end()
    converted, hits = re.subn(pattern, replacement, text[at:], flags=re.I | re.M)
    parts.append(converted)
    return "".join(parts), count + hits


def _validate_catalog(payload: dict, path: Path) -> None:
    required = {"source_dialect", "target_dialect", "catalog_version", "elements"}
    missing = required - payload.keys()
    if missing:
        raise ValueError(f"{path.name}: missing {', '.join(sorted(missing))}")
    codes = set()
    for entry in payload["elements"]:
        if entry.get("kind") not in VALID_KINDS:
            raise ValueError(f"{path.name}: invalid kind for {entry.get('code')}")
        if entry.get("match_mode", "regex") not in VALID_MODES:
            raise ValueError(f"{path.name}: invalid match mode for {entry.get('code')}")
        if entry.get("disposition", "rename") not in VALID_DISPOSITIONS:
            raise ValueError(f"{path.name}: invalid disposition for {entry.get('code')}")
        if not set(entry.get("sections", [])) <= VALID_SECTIONS:
            raise ValueError(f"{path.name}: invalid section for {entry.get('code')}")
        if entry.get("code") in codes:
            raise ValueError(f"{path.name}: duplicate code {entry.get('code')}")
        codes.add(entry.get("code"))
        re.compile(entry["source_pattern"])
