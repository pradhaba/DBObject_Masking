"""Deterministic formatting for generated PostgreSQL routines."""

from __future__ import annotations

import re


INDENT_STYLES = {"2 spaces": "  ", "4 spaces": "    ", "Tabs": "\t"}


def format_postgresql_routine(sql: str, indent_style: str = "4 spaces") -> str:
    """Format generated FUNCTION/PROCEDURE DDL using four-space indentation."""
    indent_unit = INDENT_STYLES.get(indent_style)
    if indent_unit is None:
        raise ValueError(f"Unsupported PostgreSQL indentation style: {indent_style}")
    raw_lines = sql.replace("\t", "    ").splitlines()
    lines = []
    for raw in raw_lines:
        line = raw.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        return_select = re.match(r"RETURN\s+QUERY\s+SELECT\b(.*)", line, re.IGNORECASE)
        if return_select:
            lines.extend(["RETURN QUERY", f"SELECT{return_select.group(1)}"])
        else:
            lines.append(line)

    output = []
    procedural_indent = 0
    in_body = False
    in_query = False
    query_indent = 0
    select_columns = False
    paren_depth = 0
    header_parens = False
    returns_table = False
    sql_case_depth = 0

    for line in lines:
        if line == "":
            if output and output[-1] != "":
                output.append("")
            continue
        upper = line.upper()

        if not in_body:
            if re.match(r"CREATE\s+OR\s+REPLACE\s+(?:FUNCTION|PROCEDURE)\b", line, re.IGNORECASE):
                line = re.sub(r"^CREATE\s+OR\s+REPLACE\s+", "CREATE OR REPLACE ", line, flags=re.IGNORECASE)
                output.append(line)
                header_parens = line.rstrip().endswith("(")
                continue
            if upper.startswith("RETURNS TABLE"):
                output.append("RETURNS TABLE (")
                returns_table = True
                header_parens = False
                continue
            if line == ")":
                output.append(")")
                header_parens = False
                returns_table = False
                continue
            if upper.startswith("LANGUAGE "):
                output.append(re.sub(r"^LANGUAGE\s+", "LANGUAGE ", line, flags=re.IGNORECASE))
                continue
            if re.fullmatch(r"AS\s+\$\$", line, re.IGNORECASE):
                output.append("AS $$")
                in_body = True
                continue
            if header_parens or returns_table:
                output.append(indent_unit + line)
            else:
                output.append(line)
            continue

        if line == "$$;":
            output.append("$$;")
            in_body = False
            continue

        case_end = bool(sql_case_depth and re.match(r"END\b(?!\s+IF\b)", line, re.IGNORECASE))
        if case_end:
            sql_case_depth = max(0, sql_case_depth - 1)

        if re.match(r"END\s+IF\s*;?$", line, re.IGNORECASE):
            procedural_indent = max(0, procedural_indent - 1)
            line = "END IF;"
            in_query = False
        elif re.match(r"ELSE\b", line, re.IGNORECASE) and paren_depth == 0 and sql_case_depth == 0:
            procedural_indent = max(0, procedural_indent - 1)
            line = re.sub(r"^ELSE\b", "ELSE", line, flags=re.IGNORECASE)
            in_query = False
        elif re.match(r"END\s*;?$", line, re.IGNORECASE):
            procedural_indent = max(0, procedural_indent - 1)
            line = "END;"
            in_query = False

        indent = procedural_indent
        if re.match(r"RETURN\s+QUERY$", line, re.IGNORECASE):
            line = "RETURN QUERY"
            in_query = True
            query_indent = procedural_indent
            select_columns = False
        elif re.match(r"SELECT\b", line, re.IGNORECASE) and paren_depth == 0:
            line = re.sub(r"^SELECT\b", "SELECT", line, flags=re.IGNORECASE)
            indent = query_indent if in_query else procedural_indent
            select_columns = True
        elif in_query and paren_depth == 0 and re.match(r"FROM\b", line, re.IGNORECASE):
            line = re.sub(r"^FROM\b", "FROM", line, flags=re.IGNORECASE)
            indent = query_indent
            select_columns = False
        elif in_query and paren_depth == 0 and re.match(r"(?:CROSS\s+)?JOIN\b", line, re.IGNORECASE):
            join_match = re.match(r"(?P<join>(?:CROSS\s+)?JOIN\s+.*?)(?:\s+ON\s+(?P<on>.+))?$", line, re.IGNORECASE)
            join_text = re.sub(r"^(?:CROSS\s+)?JOIN\b", lambda m: m.group(0).upper(), join_match.group("join"), flags=re.IGNORECASE)
            output.append(indent_unit * query_indent + join_text)
            if join_match.group("on"):
                output.append(indent_unit * (query_indent + 1) + "ON " + join_match.group("on"))
            paren_depth += _parenthesis_delta(line)
            continue
        elif in_query and paren_depth == 0 and re.match(r"WHERE\b", line, re.IGNORECASE):
            line = re.sub(r"^WHERE\b", "WHERE", line, flags=re.IGNORECASE)
            indent = query_indent
        elif in_query and paren_depth == 0 and re.match(r"(?:AND|OR)\b", line, re.IGNORECASE):
            line = re.sub(r"^(AND|OR)\b", lambda m: m.group(1).upper(), line, flags=re.IGNORECASE)
            indent = query_indent + 1
        elif in_query and select_columns:
            indent = query_indent + 1
        elif in_query and paren_depth > 0:
            indent = query_indent + 2
        elif sql_case_depth:
            indent = procedural_indent + sql_case_depth

        output.append(indent_unit * indent + line)
        paren_depth = max(0, paren_depth + _parenthesis_delta(line))

        if re.match(r"(?:BEGIN|IF\b.*\bTHEN|ELSIF\b.*\bTHEN|ELSE)$", line, re.IGNORECASE):
            procedural_indent += 1
        if re.match(r"CASE\b", line, re.IGNORECASE):
            sql_case_depth += 1
        if line.rstrip().endswith(";") and in_query and paren_depth == 0:
            in_query = False
            select_columns = False

    while output and output[-1] == "":
        output.pop()
    output = _use_leading_declaration_commas(output)
    return "\n".join(_use_leading_select_commas(output)) + "\n"


def _use_leading_declaration_commas(lines: list[str]) -> list[str]:
    """Use leading commas for routine parameters and RETURNS TABLE columns."""
    formatted = list(lines)
    in_declarations = False
    comma_pending = False

    for index, line in enumerate(formatted):
        stripped = line.strip()
        if (
            re.match(r"CREATE\s+OR\s+REPLACE\s+(?:FUNCTION|PROCEDURE)\b", stripped, re.IGNORECASE)
            and stripped.endswith("(")
        ) or re.fullmatch(r"RETURNS\s+TABLE\s*\(", stripped, re.IGNORECASE):
            in_declarations = True
            comma_pending = False
            continue
        if not in_declarations:
            continue
        if stripped == ")":
            in_declarations = False
            comma_pending = False
            continue
        if not stripped:
            continue

        leading = line[:len(line) - len(line.lstrip())]
        content = line.lstrip()
        if comma_pending:
            content = ", " + content
            comma_pending = False
        if content.rstrip().endswith(","):
            content = content.rstrip()[:-1].rstrip()
            comma_pending = True
        formatted[index] = leading + content

    return formatted


def _use_leading_select_commas(lines: list[str]) -> list[str]:
    """Move top-level SELECT-list delimiters to the following column line."""
    formatted = list(lines)
    in_select_list = False
    depth = 0
    comma_pending = False

    for index, line in enumerate(formatted):
        stripped = line.strip()
        if re.fullmatch(r"SELECT", stripped, re.IGNORECASE):
            in_select_list = True
            depth = 0
            comma_pending = False
            continue
        if not in_select_list:
            continue
        if depth == 0 and re.match(r"FROM\b", stripped, re.IGNORECASE):
            in_select_list = False
            comma_pending = False
            continue
        if not stripped:
            continue

        leading = line[:len(line) - len(line.lstrip())]
        content = line.lstrip()
        if comma_pending:
            content = ", " + content
            comma_pending = False

        next_depth = max(0, depth + _parenthesis_delta(content))
        if next_depth == 0 and content.rstrip().endswith(","):
            content = content.rstrip()[:-1].rstrip()
            comma_pending = True
        formatted[index] = leading + content
        depth = next_depth

    return formatted


def _parenthesis_delta(line: str) -> int:
    delta = 0
    quote = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == quote:
                if index + 1 < len(line) and line[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "(":
            delta += 1
        elif char == ")":
            delta -= 1
        index += 1
    return delta
