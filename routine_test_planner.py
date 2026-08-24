"""Static planning for parameterized source/target routine validation."""

from __future__ import annotations

import datetime
import re


_IDENTIFIER = r'(?:(?:"[^"]+")|(?:[A-Za-z_]\w*))'


def build_routine_test_plan(source_sql: str, target_sql: str = "") -> dict:
    """Describe test inputs and data dependencies without executing either routine."""
    routine_name = _routine_name(source_sql) or _routine_name(target_sql) or "unknown_routine"
    target_name = _routine_name(target_sql) or routine_name
    target_kind, invocation_style = _target_invocation(target_sql)
    parameters = _parameters(source_sql)
    parameter_names = {item["name"].lower(): item for item in parameters}
    parameter_names.update({f"p_{item['name'].lower()}": item for item in parameters})
    analysis_sql = source_sql + "\n" + target_sql
    conditions = []

    for match in re.finditer(
        r'\b(?:IF|ELSIF|AND|OR|WHERE)\s+\(*\s*@?([A-Za-z_]\w*)\s*'
        r'(=|<>|!=|<=|>=|<|>)\s*('
        r"'(?:''|[^'])*'|[-+]?\d+(?:\.\d+)?|NULL)",
        analysis_sql,
        re.I,
    ):
        name = match.group(1)
        if name.lower() not in parameter_names:
            continue
        operator, literal = match.group(2), match.group(3)
        finding = {
            "parameter": parameter_names[name.lower()]["name"],
            "kind": "literal_condition",
            "condition": match.group(0).strip(),
            "operator": operator,
            "literal": literal,
            "candidates": _boundary_candidates(literal, operator),
            "source": "procedure condition",
        }
        if not any(item["parameter"] == finding["parameter"] and item["operator"] == operator and item["literal"] == literal for item in conditions):
            conditions.append(finding)

    column_links = []
    comparison = re.compile(
        rf'(?:(?P<table>{_IDENTIFIER})\.)?(?P<column>{_IDENTIFIER})\s*=\s*@?(?P<parameter>[A-Za-z_]\w*)'
        rf'|@?(?P<reverse_parameter>[A-Za-z_]\w*)\s*=\s*(?:(?P<reverse_table>{_IDENTIFIER})\.)?(?P<reverse_column>{_IDENTIFIER})',
        re.I,
    )
    aliases = _table_aliases(analysis_sql)
    for match in comparison.finditer(analysis_sql):
        parameter = match.group("parameter") or match.group("reverse_parameter")
        if not parameter or parameter.lower() not in parameter_names:
            continue
        table_token = _unquote(match.group("table") or match.group("reverse_table") or "")
        column = _unquote(match.group("column") or match.group("reverse_column"))
        table = aliases.get(table_token.lower(), table_token) if table_token else ""
        link = {
            "parameter": parameter_names[parameter.lower()]["name"],
            "kind": "column_value",
            "condition": match.group(0),
            "table": table,
            "column": column,
            "candidates": [],
            "source": f"{table + '.' if table else ''}{column}",
        }
        if link not in column_links:
            column_links.append(link)

    between_pattern = re.compile(
        r'(?:(?P<table>[A-Za-z_]\w*)\.)?"?(?P<column>[A-Za-z_]\w*)"?\s+BETWEEN\s+'
        r'@?(?P<first>[A-Za-z_]\w*)\s+AND\s+@?(?P<second>[A-Za-z_]\w*)',
        re.I,
    )
    for match in between_pattern.finditer(analysis_sql):
        table_token = match.group("table") or ""
        table = aliases.get(table_token.lower(), table_token) if table_token else ""
        for parameter in (match.group("first"), match.group("second")):
            if parameter.lower() not in parameter_names:
                continue
            link = {
                "parameter": parameter_names[parameter.lower()]["name"], "kind": "column_value",
                "condition": match.group(0), "table": table, "column": match.group("column"),
                "candidates": [], "source": f"{table + '.' if table else ''}{match.group('column')}",
            }
            if not any(item["parameter"] == link["parameter"] and item.get("table") == table and item.get("column") == link["column"] for item in column_links):
                column_links.append(link)

    suggestions = conditions + column_links
    linked = {item["parameter"].lower() for item in suggestions}
    for parameter in parameters:
        if parameter["name"].lower() not in linked:
            suggestions.append({
                "parameter": parameter["name"], "kind": "manual",
                "condition": "No inferable comparison", "candidates": [],
                "source": "manual value required",
            })

    temporary_names = {
        item.lower() for item in re.findall(
            r'\bDECLARE\s+LOCAL\s+TEMPORARY\s+TABLE\s+"?([A-Za-z_]\w*)"?', source_sql, re.I
        )
    }
    by_base = {}
    for table in _tables(source_sql) + _tables(target_sql):
        base = table["name"].split('.')[-1].lower()
        if base not in temporary_names:
            by_base[base] = table
    tables = list(by_base.values())
    qualified_by_base = {item["name"].split('.')[-1].lower(): item["name"] for item in tables}
    for link in column_links:
        if link.get("table"):
            link["table"] = qualified_by_base.get(link["table"].split('.')[-1].lower(), link["table"])
            link["source"] = f"{link['table']}.{link['column']}"
    for link in column_links:
        if link["table"] and link["table"].lower() not in {item["name"].lower() for item in tables}:
            tables.append({"name": link["table"], "source_rows": None, "target_rows": None, "status": "not_checked"})

    return {
        "routine_name": routine_name,
        "target_routine_name": target_name,
        "target_kind": target_kind,
        "invocation_style": invocation_style,
        "parameters": parameters,
        "suggestions": suggestions,
        "tables": tables,
        "branch_count": len(conditions),
        "validation_mode": "compare_both",
        "approved": False,
    }


def generate_invocation_sql(plan: dict, max_cases: int = 12) -> list[str]:
    """Generate branch-oriented PostgreSQL calls using current suggested values."""
    parameters = [item for item in plan["parameters"] if item.get("mode", "IN") != "OUT"]
    candidates = {}
    for parameter in parameters:
        values = []
        for suggestion in plan["suggestions"]:
            if suggestion["parameter"].lower() != parameter["name"].lower():
                continue
            for value in suggestion.get("candidates", []):
                if value not in values:
                    values.append(value)
        candidates[parameter["name"].lower()] = values

    base = [candidates[item["name"].lower()][0] if candidates[item["name"].lower()] else _Missing(item["name"]) for item in parameters]
    cases = [base]
    for index, parameter in enumerate(parameters):
        for alternative in candidates[parameter["name"].lower()][1:]:
            case = list(base); case[index] = alternative
            if case not in cases:
                cases.append(case)
            if len(cases) >= max_cases:
                break
        if len(cases) >= max_cases:
            break

    name = plan.get("target_routine_name") or plan["routine_name"]
    style = plan.get("invocation_style", "table_function")
    statements = []
    for case in cases:
        arguments = ", ".join(_sql_literal(value, parameter["datatype"]) for value, parameter in zip(case, parameters))
        if style == "procedure":
            statements.append(f"CALL {name}({arguments});")
        elif style == "scalar_function":
            statements.append(f"SELECT {name}({arguments});")
        else:
            statements.append(f"SELECT * FROM {name}({arguments});")
    return statements


def apply_data_findings(plan: dict, side: str, row_counts: dict[str, int | None], samples: dict[tuple[str, str], list]) -> dict:
    """Attach read-only database findings to an existing plan."""
    for table in plan["tables"]:
        count = row_counts.get(table["name"].lower())
        table[f"{side}_rows"] = count
        table[f"{side}_status"] = "missing" if count is None else ("empty" if count == 0 else "available")
        statuses = [table.get("source_status"), table.get("target_status")]
        table["status"] = "missing" if "missing" in statuses else ("empty" if "empty" in statuses else "available")
    for suggestion in plan["suggestions"]:
        if suggestion["kind"] != "column_value" or not suggestion.get("table"):
            continue
        values = samples.get((suggestion["table"].lower(), suggestion["column"].lower()), [])
        existing = suggestion.setdefault("candidates", [])
        for value in values:
            if value not in existing:
                existing.append(value)
    return plan


def collect_data_findings(connection, plan: dict, side: str, database_type: str = "PostgreSQL") -> dict:
    """Check table availability and collect a few distinct parameter values read-only."""
    row_counts: dict[str, int | None] = {}
    samples: dict[tuple[str, str], list] = {}
    with connection.cursor() as cursor:
        for table in plan["tables"]:
            name = table["name"]
            try:
                cursor.execute(f"SELECT 1 FROM {_quote_qualified(name)}")
                row_counts[name.lower()] = 1 if cursor.fetchone() is not None else 0
            except Exception:
                _rollback_read_error(connection)
                row_counts[name.lower()] = None
        for suggestion in plan["suggestions"]:
            if suggestion["kind"] != "column_value" or not suggestion.get("table"):
                continue
            table, column = suggestion["table"], suggestion["column"]
            if row_counts.get(table.lower()) is None:
                continue
            try:
                column_sql = _quote_identifier(column)
                table_sql = _quote_qualified(table)
                if database_type in {"SQL Anywhere ASA", "SAP ASA", "SAP ASE", "SQL Server"}:
                    query = f"SELECT DISTINCT TOP 5 {column_sql} FROM {table_sql} WHERE {column_sql} IS NOT NULL"
                elif database_type == "Oracle":
                    query = f"SELECT DISTINCT {column_sql} FROM {table_sql} WHERE {column_sql} IS NOT NULL FETCH FIRST 5 ROWS ONLY"
                else:
                    query = f"SELECT DISTINCT {column_sql} FROM {table_sql} WHERE {column_sql} IS NOT NULL LIMIT 5"
                cursor.execute(query)
                samples[(table.lower(), column.lower())] = [row[0] for row in cursor.fetchmany(5)]
            except Exception:
                _rollback_read_error(connection)
    return apply_data_findings(plan, side, row_counts, samples)


def _quote_identifier(value: str) -> str:
    if not re.fullmatch(r'[A-Za-z_]\w*', value):
        raise ValueError(f"Unsafe database identifier: {value}")
    return '"' + value.replace('"', '""') + '"'


def _quote_qualified(value: str) -> str:
    return '.'.join(_quote_identifier(part) for part in value.split('.'))


def _rollback_read_error(connection) -> None:
    rollback = getattr(connection, 'rollback', None)
    if rollback:
        rollback()


def _routine_name(sql: str) -> str:
    match = re.search(r'\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:PROCEDURE|PROC|FUNCTION)\s+((?:[A-Za-z_]\w*\.)?"?[A-Za-z_]\w*"?)', sql, re.I)
    return match.group(1).replace('"', '') if match else ""


def _target_invocation(sql: str) -> tuple[str, str]:
    if re.search(r'\bCREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\b', sql, re.I):
        return "procedure", "procedure"
    if re.search(r'\bCREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\b', sql, re.I):
        if re.search(r'\bRETURNS\s+(?:TABLE\b|SETOF\b)', sql, re.I):
            return "function", "table_function"
        return "function", "scalar_function"
    return "function", "table_function"


class _Missing:
    def __init__(self, name: str):
        self.name = name


def _sql_literal(value, datatype: str) -> str:
    if isinstance(value, _Missing):
        return f"NULL /* value required: {value.name} */"
    if value is None:
        return "NULL"
    normalized = datatype.lower()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        rendered = value.isoformat()
        cast = "timestamp" if isinstance(value, datetime.datetime) else "date"
        return "'" + rendered.replace("'", "''") + f"'::{cast}"
    text = str(value)
    if re.search(r'\b(?:int|integer|smallint|bigint|numeric|decimal|real|float|double)\b', normalized) and re.fullmatch(r'[-+]?\d+(?:\.\d+)?', text):
        return text
    escaped = text.replace("'", "''")
    if re.search(r'\bdate\b', normalized) and not re.search(r'\btime', normalized):
        return f"'{escaped}'::date"
    if re.search(r'\btimestamp|datetime\b', normalized):
        return f"'{escaped}'::timestamp"
    return f"'{escaped}'"


def _parameters(sql: str) -> list[dict]:
    header = re.search(r'\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:PROCEDURE|PROC|FUNCTION)\b[^\(]*\((.*?)\)\s*(?:RESULT|RETURNS|BEGIN|LANGUAGE)', sql, re.I | re.S)
    if not header:
        return []
    result = []
    for item in _split_top_level(header.group(1)):
        match = re.match(r'\s*(?:(INOUT|IN|OUT)\s+)?@?"?([A-Za-z_]\w*)"?\s+(.+?)\s*$', item, re.I | re.S)
        if match:
            result.append({"mode": (match.group(1) or "IN").upper(), "name": match.group(2), "datatype": match.group(3).strip()})
    return result


def _split_top_level(value: str) -> list[str]:
    parts, start, depth, quote = [], 0, 0, None
    for index, char in enumerate(value):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif char == ',' and depth == 0:
            parts.append(value[start:index]); start = index + 1
    parts.append(value[start:])
    return [item.strip() for item in parts if item.strip()]


def _boundary_candidates(literal: str, operator: str) -> list:
    if literal.upper() == "NULL":
        return [None]
    if literal.startswith("'"):
        value = literal[1:-1].replace("''", "'")
        return [value, "" if value else "test_value"]
    try:
        value = float(literal) if '.' in literal else int(literal)
    except ValueError:
        return [literal]
    if operator in {"=", "!=", "<>"}:
        return [value, value + 1]
    return [value - 1, value, value + 1]


def _table_aliases(sql: str) -> dict[str, str]:
    aliases = {}
    qualified = rf'{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})?'
    for match in re.finditer(rf'\b(?:FROM|JOIN)\s+({qualified})\s+(?:AS\s+)?({_IDENTIFIER})', sql, re.I):
        table = '.'.join(_unquote(part.strip()) for part in re.split(r'\s*\.\s*', match.group(1)))
        aliases[_unquote(match.group(2)).lower()] = table
    return aliases


def _tables(sql: str) -> list[dict]:
    names = []
    qualified = rf'{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})?'
    for match in re.finditer(rf'\b(?:FROM|JOIN|UPDATE|INSERT\s+INTO)\s+({qualified})', sql, re.I):
        name = '.'.join(_unquote(part.strip()) for part in re.split(r'\s*\.\s*', match.group(1)))
        if name.lower() not in {item.lower() for item in names} and not name.lower().startswith(('select', 'pg_temp.')):
            names.append(name)
    return [{"name": name, "source_rows": None, "target_rows": None, "status": "not_checked"} for name in names]


def _unquote(value: str) -> str:
    return value[1:-1].replace('""', '"') if value.startswith('"') and value.endswith('"') else value
