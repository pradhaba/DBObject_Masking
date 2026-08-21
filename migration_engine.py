"""Database-backed migration skill execution."""

from __future__ import annotations

import re

from database import get_active_skill_version
from masker import mask_text, unmask_text


def migrate_text(text: str, source_dialect: str, target_dialect: str, database_path=None, target_override="auto"):
    """Mask identifiers, apply the selected DB skill, then restore target names."""
    if source_dialect == "sybase_asa" and not re.search(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?PROC(?:EDURE)?\b", text, re.IGNORECASE):
        raise ValueError("The active SAP ASA skill currently supports procedures only.")
    if database_path is None:
        skill = get_active_skill_version(source_dialect, target_dialect)
    else:
        skill = get_active_skill_version(source_dialect, target_dialect, database_path)
    if skill is None:
        raise ValueError(f"No active approved migration skill for {source_dialect} → {target_dialect}.")
    masked, mapping = mask_text(text, source_dialect, embed_mapping=False)
    migrated, trace = _apply_rules_with_trace(masked, skill["rules"], mapping, source_dialect, target_dialect)
    analysis = analyze_asa_procedure(text) if source_dialect == "sybase_asa" else {}
    target_type, reason, classification_rule = classify_postgresql_routine(text, target_override)
    if source_dialect == "sybase_asa" and target_dialect == "postgresql":
        migrated, renderer_trace, routine_language = render_postgresql_routine(migrated, target_type)
        trace.extend(renderer_trace)
    else:
        routine_language = None
    restored = unmask_text(migrated, mapping, target_dialect)
    if target_dialect != "postgresql":
        target_type, reason, classification_rule = "procedure", "Target is not PostgreSQL.", "non-postgresql-target"
    skill = dict(skill)
    skill["target_object_type"] = target_type
    skill["classification_reason"] = reason
    skill["classification_rule"] = classification_rule
    skill["analysis"] = analysis
    skill["human_override"] = target_override if target_override != "auto" else None
    skill["routine_language"] = routine_language
    skill["trace"] = trace
    return restored, mapping, skill


def _apply_rules_with_trace(masked_text: str, rules: list[dict], mapping, source_dialect: str, target_dialect: str):
    """Apply deterministic rules line by line and retain a complete audit trace."""
    output_lines = []
    trace = []
    for line_number, original_line in enumerate(masked_text.splitlines(), start=1):
        current = original_line
        applied = []
        for rule in rules:
            if rule["rule_code"] == "asa-pg-schema-qualification":
                changed, count = _qualify_schema_references(current, rule["replacement"])
            else:
                changed, count = re.subn(rule["pattern"], rule["replacement"], current, flags=re.IGNORECASE)
            if count:
                applied.append({
                    "rule_id": rule["id"], "rule_code": rule["rule_code"],
                    "priority": rule["priority"], "matches": count,
                })
                current = changed
        output_lines.append(current)
        trace.append({
            "line": line_number,
            "source": unmask_text(original_line, mapping, source_dialect),
            "output": unmask_text(current, mapping, target_dialect),
            "rules": applied,
        })
    return "\n".join(output_lines), trace


def _qualify_schema_references(line: str, schema: str) -> tuple[str, int]:
    """Qualify structural table references and non-built-in routine calls."""
    count = 0
    relation = re.compile(
        r"(?P<prefix>\b(?:FROM|JOIN|UPDATE|INTO|DELETE\s+FROM)\s+)"
        r"(?![\"\w]+\s*\.)(?P<name>\"?[A-Za-z_][A-Za-z0-9_$]*\"?)",
        re.IGNORECASE,
    )
    line, relation_count = relation.subn(lambda m: f"{m.group('prefix')}{schema}.{m.group('name')}", line)
    count += relation_count
    builtins = {
        "abs","avg","cast","ceil","ceiling","coalesce","count","current_date","current_time",
        "current_timestamp","date_part","extract","greatest","length","lower","max","min","mod",
        "nullif","octet_length","position","round","substring","sum","trim","upper",
        "dba","if","in","values","varchar","char","text","integer","numeric","timestamp","boolean",
    }
    routine = re.compile(r"(?<![\w.\"'])(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\s*(?=\()")
    def qualify_call(match):
        nonlocal count
        name = match.group("name")
        if name.lower() in builtins:
            return match.group(0)
        count += 1
        return f"{schema}.{name}"
    line = routine.sub(qualify_call, line)
    return line, count


def analyze_asa_procedure(text: str) -> dict:
    """Extract deterministic signals used by the classification subskill."""
    select_bodies = re.findall(r"\bSELECT\b(?P<body>.*?)(?=;|\bEND\b|$)", text, re.IGNORECASE | re.DOTALL)
    result_select = any(not re.search(r"\bINTO\b", body, re.IGNORECASE) for body in select_bodies)
    return {
        "transaction_control": bool(re.search(r"\b(?:COMMIT|ROLLBACK)\b", text, re.IGNORECASE)),
        "result_clause": bool(re.search(r"\bRESULT\s*\(", text, re.IGNORECASE)),
        "result_set_select": result_select,
        "scalar_return": bool(re.search(r"\bRETURN\s+(?!QUERY\b|NEXT\b)[^;]+", text, re.IGNORECASE)),
        "data_modification": bool(re.search(r"\b(?:INSERT|UPDATE|DELETE|MERGE)\b", text, re.IGNORECASE)),
        "output_parameters": bool(re.search(r"\b(?:OUT|INOUT)\s+[\w@]+|[\w@]+\s+[^,()]+\s+OUTPUT\b", text, re.IGNORECASE)),
        "simple_select_only": _simple_select_body(text) is not None,
    }


def classify_postgresql_routine(text: str, override="auto") -> tuple[str, str, str]:
    """Choose a PostgreSQL routine type using explicit, auditable criteria."""
    if override in {"function", "procedure"}:
        return override, f"Human/project override selected PostgreSQL {override}.", "human-override"
    analysis = analyze_asa_procedure(text)
    if analysis["transaction_control"]:
        return "procedure", "Source routine controls transactions.", "transaction-control-procedure"
    if analysis["result_clause"] or analysis["result_set_select"]:
        return "function", "Source routine produces a query result set.", "result-set-function"
    if analysis["scalar_return"]:
        return "function", "Source routine calculates or returns a scalar value.", "scalar-return-function"
    if analysis["data_modification"]:
        return "procedure", "Source routine primarily performs data modification.", "operation-procedure"
    return "procedure", "No explicit return contract; preserve CALL-style behavior.", "default-procedure"


def render_postgresql_routine(masked_text: str, target_type: str):
    """Render a masked ASA procedure as a PostgreSQL PL/pgSQL routine."""
    match = re.search(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?PROC(?:EDURE)?\s+(?P<name>[\w.$\"\[\]]+)", masked_text, re.IGNORECASE)
    if not match:
        raise ValueError("Unable to locate the ASA procedure declaration for PostgreSQL rendering.")
    begin = re.search(r"\bBEGIN\b", masked_text[match.end():], re.IGNORECASE)
    if not begin:
        raise ValueError("Unable to locate the ASA procedure BEGIN block.")
    begin_at = match.end() + begin.start()
    header_tail = masked_text[match.end():begin_at]
    header_tail = re.sub(r"\bAS\s*$", "", header_tail, flags=re.IGNORECASE).strip()
    header_tail, result_columns = _extract_result_clause(header_tail)
    params = header_tail if header_tail.startswith("(") else f"({header_tail.strip().strip(',')})"
    body = masked_text[begin_at:].strip()
    body = re.sub(r";?\s*$", "", body)
    name = match.group("name")
    trace = []
    if target_type == "function":
        returns = f"RETURNS TABLE ({result_columns})" if result_columns else "RETURNS SETOF RECORD"
        simple_select = _simple_select_body(body)
        if simple_select is not None:
            rendered = f"CREATE OR REPLACE FUNCTION {name}{params}\n{returns}\nLANGUAGE sql\nAS $$\n{simple_select.rstrip(';')};\n$$;"
            renderer = "postgresql-sql-function-renderer"
            routine_language = "sql"
        else:
            body = _convert_top_level_result_selects(body)
            declarations, body = _normalize_plpgsql_body(body)
            declare_block = f"DECLARE\n{declarations}\n" if declarations else ""
            rendered = f"CREATE OR REPLACE FUNCTION {name}{params}\n{returns}\nLANGUAGE plpgsql\nAS $$\n{declare_block}{body}\n$$;"
            renderer = "postgresql-plpgsql-function-renderer"
            routine_language = "plpgsql"
    else:
        declarations, body = _normalize_plpgsql_body(body)
        declare_block = f"DECLARE\n{declarations}\n" if declarations else ""
        rendered = f"CREATE OR REPLACE PROCEDURE {name}{params}\nLANGUAGE plpgsql\nAS $$\n{declare_block}{body}\n$$;"
        renderer = "postgresql-procedure-renderer"
        routine_language = "plpgsql"
    trace.append({"line": "renderer", "source": match.group(0), "output": rendered.splitlines()[0],
                  "rules": [{"rule_id": renderer, "rule_code": renderer, "priority": 2000, "matches": 1}]})
    return rendered, trace, routine_language


def _normalize_plpgsql_body(body: str) -> tuple[str, str]:
    """Hoist ASA declarations and convert SET assignments to PL/pgSQL syntax."""
    declarations = []
    output = []
    waiting_for_assignment = False
    for line in body.splitlines():
        declaration = re.match(r"^\s*DECLARE\s+(.+?;?)\s*$", line, re.IGNORECASE)
        if declaration:
            declarations.append(f"    {declaration.group(1).rstrip(';')};")
            continue
        if re.match(r"^\s*SET\s*$", line, re.IGNORECASE):
            waiting_for_assignment = True
            continue
        direct_set = re.match(r"^(?P<indent>\s*)SET\s+(?P<name>@?[\w$]+)\s*=\s*(?P<value>.+?)\s*;?\s*$", line, re.IGNORECASE)
        if direct_set:
            output.append(f"{direct_set.group('indent')}{direct_set.group('name')} := {direct_set.group('value').rstrip(';')};")
            continue
        if waiting_for_assignment:
            assignment = re.match(r"^(?P<indent>\s*)(?P<name>@?[\w$]+)\s*=\s*(?P<value>.+?)\s*;?\s*$", line)
            if assignment:
                output.append(f"{assignment.group('indent')}{assignment.group('name')} := {assignment.group('value').rstrip(';')};")
                waiting_for_assignment = False
                continue
            waiting_for_assignment = False
        output.append(line)
    return "\n".join(declarations), "\n".join(output)


def _simple_select_body(text: str):
    """Return a sole SELECT body, or None when procedural behavior is present."""
    begin = re.search(r"\bBEGIN\b", text, re.IGNORECASE)
    if begin:
        ends = list(re.finditer(r"\bEND\b\s*;?", text, re.IGNORECASE))
        if not ends or ends[-1].start() <= begin.end():
            return None
        body = text[begin.end():ends[-1].start()].strip()
    else:
        body = text.strip()
    if not re.match(r"SELECT\b", body, re.IGNORECASE):
        return None
    if re.search(
        r"\b(?:DECLARE|IF|ELSE|ELSIF|LOOP|WHILE|FOR|INSERT|UPDATE|DELETE|MERGE|SET|"
        r"RETURN|INTO|EXCEPTION|EXECUTE|COMMIT|ROLLBACK|SIGNAL|RAISERROR)\b",
        body, re.IGNORECASE,
    ):
        return None
    statements = [statement.strip() for statement in body.split(";") if statement.strip()]
    return body if len(statements) == 1 else None


def _convert_top_level_result_selects(body: str) -> str:
    """Prefix only top-level result SELECT statements with RETURN QUERY."""
    lines = body.splitlines()
    depth = 0
    converted = []
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        is_select = re.match(r"SELECT\b", stripped, re.IGNORECASE) is not None
        if is_select and depth == 0:
            statement = []
            for following in lines[index:]:
                statement.append(following)
                if ";" in following:
                    break
            statement_text = "\n".join(statement)
            if not re.search(r"\bINTO\b", statement_text, re.IGNORECASE):
                indent = line[:len(line) - len(stripped)]
                line = f"{indent}RETURN QUERY {stripped}"
        converted.append(line)
        depth = max(0, depth + _parenthesis_delta(line))
    return "\n".join(converted)


def _parenthesis_delta(line: str) -> int:
    """Count structural parentheses while ignoring strings and line comments."""
    delta = 0
    quote = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote is None and line[index:index + 2] == "--":
            break
        if char in {"'", '"'}:
            if quote == char:
                if index + 1 < len(line) and line[index + 1] == char:
                    index += 2
                    continue
                quote = None
            elif quote is None:
                quote = char
        elif quote is None:
            if char == "(":
                delta += 1
            elif char == ")":
                delta -= 1
        index += 1
    return delta


def _extract_result_clause(header: str) -> tuple[str, str]:
    """Remove ASA RESULT(...) using balanced parentheses and return its columns."""
    result = re.search(r"\bRESULT\s*\(", header, re.IGNORECASE)
    if result is None:
        return header, ""
    open_at = header.find("(", result.start())
    depth = 0
    close_at = -1
    quote = None
    for index in range(open_at, len(header)):
        char = header[index]
        if char in {"'", '"'}:
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
            continue
        if quote is not None:
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                close_at = index
                break
    if close_at < 0:
        raise ValueError("ASA RESULT clause has unbalanced parentheses.")
    columns = header[open_at + 1:close_at].strip()
    remaining = (header[:result.start()] + header[close_at + 1:]).strip()
    return remaining, columns
