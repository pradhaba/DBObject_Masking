"""Infer PostgreSQL RETURNS TABLE contracts from migrated-table metadata."""

from __future__ import annotations

import re


IDENT = r'[A-Za-z_][A-Za-z0-9_$]*'
SQL_IDENT = rf'(?:"[^"]+"|{IDENT})'


def _unquote_identifier(value: str) -> str:
    return value[1:-1].replace('""', '"') if value.startswith('"') and value.endswith('"') else value


def infer_returns_table(sql: str, connection, default_schema: str = "dba") -> str:
    """Return a PostgreSQL column contract shared by every outer result SELECT."""
    select_contexts = _outer_select_contexts(sql, default_schema)
    if not select_contexts:
        raise ValueError("No result-producing SELECT was found for RETURNS TABLE inference.")
    parameters = _parameter_types(sql)
    contracts = [
        [_resolve_expression(item, connection, parameters, default_schema, sources) for item in _split_sql_list(body)]
        for body, sources in select_contexts
    ]
    expected = contracts[0]
    for branch, contract in enumerate(contracts[1:], start=2):
        if len(contract) != len(expected):
            raise ValueError(f"Result SELECT {branch} returns {len(contract)} columns; expected {len(expected)}.")
        for position, (left, right) in enumerate(zip(expected, contract), start=1):
            if left[0].lower() != right[0].lower() or left[1].lower() != right[1].lower():
                raise ValueError(
                    f"Result SELECT {branch}, column {position} is {right[0]} {right[1]}; "
                    f"expected {left[0]} {left[1]}."
                )
    names = [name.lower() for name, _ in expected]
    if len(names) != len(set(names)):
        raise ValueError("RETURNS TABLE output column names must be unique.")
    return ",\n    ".join(f"{name} {data_type}" for name, data_type in expected)


def align_result_selects(sql: str) -> str:
    """Reorder equivalent result SELECT lists to the first branch's contract."""
    spans = _outer_select_spans(sql)
    if len(spans) < 2:
        return sql
    lists = [_split_sql_list(sql[start:end]) for start, end in spans]
    canonical_names = [_expression_name(item) for item in lists[0]]
    if None in canonical_names or len(canonical_names) != len(set(name.lower() for name in canonical_names)):
        return sql
    replacements = []
    for (start, end), expressions in zip(spans[1:], lists[1:]):
        by_name = {_expression_name(item).lower(): item for item in expressions if _expression_name(item)}
        if set(by_name) != {name.lower() for name in canonical_names}:
            continue
        ordered = [by_name[name.lower()] for name in canonical_names]
        if ordered != expressions:
            original = sql[start:end]
            leading = re.match(r'\s*', original).group(0)
            trailing = re.search(r'\s*$', original).group(0)
            replacements.append((start, end, leading + ",\n    ".join(ordered) + trailing))
    for start, end, replacement in reversed(replacements):
        sql = sql[:start] + replacement + sql[end:]
    return sql


def qualify_unqualified_result_columns(sql: str, connection, default_schema: str = "dba") -> str:
    """Qualify direct result columns when destination metadata identifies one source table."""
    replacements = []
    order_qualifiers: dict[str, str] = {}
    for start, end in _outer_select_spans(sql):
        sources = _select_source_tables(sql, end, default_schema)
        expressions = _split_sql_list(sql[start:end])
        changed = False
        for position, expression in enumerate(expressions):
            direct = re.fullmatch(rf'\s*(?P<column>{SQL_IDENT})\s*', expression)
            if not direct:
                continue
            column_token = direct.group('column')
            column = _unquote_identifier(column_token)
            matches = []
            for schema, table, alias in sources:
                if _find_column_type(connection, schema, table, column):
                    matches.append((table, alias))
            if len(matches) != 1:
                continue
            table, alias = matches[0]
            qualifier = alias if alias.lower() != table.lower() else table
            expressions[position] = f'{_quote_identifier(qualifier)}.{column_token}'
            order_qualifiers[column.lower()] = f'{_quote_identifier(qualifier)}.{column_token}'
            changed = True
        if changed:
            original = sql[start:end]
            leading = re.match(r'\s*', original).group(0)
            trailing = re.search(r'\s*$', original).group(0)
            replacements.append((start, end, leading + ',\n    '.join(expressions) + trailing))
    for start, end, replacement in reversed(replacements):
        sql = sql[:start] + replacement + sql[end:]
    for column, qualified in order_qualifiers.items():
        sql = re.sub(
            rf'(\bORDER\s+BY\s+)"?{re.escape(column)}"?(?=\s+(?:ASC|DESC)\b|\s*[,;])',
            lambda match: match.group(1) + qualified,
            sql,
            flags=re.IGNORECASE,
        )
    return sql


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _outer_select_lists(sql: str) -> list[str]:
    return [sql[start:end].strip() for start, end in _outer_select_spans(sql)]


def _outer_select_contexts(sql: str, default_schema: str) -> list[tuple[str, list[tuple[str, str, str]]]]:
    contexts = []
    for start, end in _outer_select_spans(sql):
        contexts.append((sql[start:end].strip(), _select_source_tables(sql, end, default_schema)))
    return contexts


def _select_source_tables(sql: str, from_position: int, default_schema: str) -> list[tuple[str, str, str]]:
    """Return (schema, table, qualifier) relations in one outer SELECT scope."""
    start_match = re.match(r'\bFROM\b', sql[from_position:], re.IGNORECASE)
    if not start_match:
        return []
    start = from_position + start_match.end()
    boundary = re.compile(r'\b(?:WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|UNION|LIMIT|OFFSET|FETCH)\b|;|\bEND\b', re.I)
    depth = 0
    quote = None
    cursor = start
    while cursor < len(sql):
        char = sql[cursor]
        if quote:
            if char == quote:
                if cursor + 1 < len(sql) and sql[cursor + 1] == quote:
                    cursor += 2
                    continue
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth = max(0, depth - 1)
        elif depth == 0 and boundary.match(sql, cursor):
            break
        cursor += 1
    from_body = sql[start:cursor]
    relation = re.compile(
        rf'(?:^|,|\bJOIN\b)\s*(?:(?P<schema>{SQL_IDENT})\.)?(?P<table>{SQL_IDENT})'
        rf'(?:\s+(?:AS\s+)?(?P<alias>{SQL_IDENT}))?',
        re.I,
    )
    reserved = {'on', 'where', 'join', 'left', 'right', 'full', 'inner', 'outer', 'cross'}
    sources = []
    for match in relation.finditer(from_body):
        schema = _unquote_identifier(match.group('schema')) if match.group('schema') else default_schema
        table = _unquote_identifier(match.group('table'))
        alias = _unquote_identifier(match.group('alias')) if match.group('alias') else table
        if alias.lower() in reserved:
            alias = table
        sources.append((schema, table, alias))
    return sources


def _outer_select_spans(sql: str) -> list[tuple[int, int]]:
    results = []
    quote = None
    depth = 0
    index = 0
    select_word = re.compile(r'\bSELECT\b', re.IGNORECASE)
    from_word = re.compile(r'\bFROM\b', re.IGNORECASE)
    while index < len(sql):
        char = sql[index]
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth = max(0, depth - 1)
        elif depth == 0:
            selected = select_word.match(sql, index)
            if selected:
                cursor = selected.end()
                local_depth = 0
                local_quote = None
                while cursor < len(sql):
                    current = sql[cursor]
                    if local_quote:
                        if current == local_quote:
                            local_quote = None
                    elif current in ("'", '"'):
                        local_quote = current
                    elif current == '(':
                        local_depth += 1
                    elif current == ')':
                        local_depth = max(0, local_depth - 1)
                    elif local_depth == 0 and from_word.match(sql, cursor):
                        results.append((selected.end(), cursor))
                        index = cursor
                        break
                    cursor += 1
        index += 1
    return results


def _expression_name(expression: str):
    alias = re.search(rf'\s+AS\s+(?P<alias>{SQL_IDENT})\s*$', expression, re.IGNORECASE)
    if alias:
        return _unquote_identifier(alias.group('alias'))
    direct = re.fullmatch(rf'\s*(?:{SQL_IDENT}\.)?(?P<column>{SQL_IDENT})\s*', expression)
    return _unquote_identifier(direct.group('column')) if direct else None


def _split_sql_list(text: str) -> list[str]:
    parts, start, depth, quote = [], 0, 0, None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth = max(0, depth - 1)
        elif char == ',' and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _parameter_types(sql: str) -> dict[str, str]:
    declaration = re.search(r'\bCREATE\s+(?:OR\s+REPLACE\s+)?PROC(?:EDURE)?\b[^\(]*\((.*?)\)\s*BEGIN\b', sql, re.IGNORECASE | re.DOTALL)
    if not declaration:
        return {}
    result = {}
    for item in _split_sql_list(declaration.group(1)):
        match = re.match(rf'\s*(?:IN|OUT|INOUT)?\s*(?P<name>{IDENT})\s+(?P<type>{IDENT}(?:\s*\([^)]*\))?)', item, re.IGNORECASE)
        if match:
            result[match.group('name').lower()] = _normalize_declared_type(match.group('type'))
    return result


def _normalize_declared_type(value: str) -> str:
    replacements = {"integer": "integer", "smallint": "smallint", "bigint": "bigint", "decimal": "numeric", "datetime": "timestamp"}
    base = re.match(IDENT, value).group(0).lower()
    return replacements.get(base, value.lower()) + value[len(base):] if '(' in value else replacements.get(base, value.lower())


def _resolve_expression(expression: str, connection, parameters: dict, default_schema: str,
                        source_tables: list[tuple[str, str, str]] | None = None):
    alias_match = re.search(rf'\s+AS\s+(?P<alias>{SQL_IDENT})\s*$', expression, re.IGNORECASE)
    output_name = _unquote_identifier(alias_match.group('alias')) if alias_match else None
    value = expression[:alias_match.start()].strip() if alias_match else expression.strip()
    parameter = re.fullmatch(IDENT, value)
    if parameter and parameter.group(0).lower() in parameters:
        return output_name or parameter.group(0), parameters[parameter.group(0).lower()]

    unqualified = re.fullmatch(SQL_IDENT, value)
    if unqualified and source_tables:
        column = _unquote_identifier(unqualified.group(0))
        return output_name or column, _unique_source_column_type(connection, source_tables, column)

    if output_name and source_tables:
        matching_type = _unique_source_column_type(connection, source_tables, output_name, required=False)
        if matching_type:
            return output_name, matching_type

    scalar = re.search(
        rf'\bSELECT\s+(?P<qual>{SQL_IDENT})\.(?P<column>{SQL_IDENT})\s+FROM\s+'
        rf'(?:(?P<schema>{SQL_IDENT})\.)?(?P<table>{SQL_IDENT})(?:\s+(?:AS\s+)?(?P<table_alias>{SQL_IDENT}))?',
        value, re.IGNORECASE | re.DOTALL,
    )
    if scalar:
        column = _unquote_identifier(scalar.group('column'))
        schema = _unquote_identifier(scalar.group('schema')) if scalar.group('schema') else default_schema
        table = _unquote_identifier(scalar.group('table'))
        return output_name or column, _column_type(connection, schema, table, column)

    direct = re.search(rf'(?P<qual>{SQL_IDENT})\.(?P<column>{SQL_IDENT})', value)
    if direct:
        column = _unquote_identifier(direct.group('column'))
        qualifier = _unquote_identifier(direct.group('qual'))
        if source_tables:
            matches = [(schema, table) for schema, table, alias in source_tables
                       if qualifier.lower() in {table.lower(), alias.lower()}]
            if len(matches) == 1:
                return output_name or column, _column_type(connection, matches[0][0], matches[0][1], column)
        return output_name or column, _column_type(connection, default_schema, qualifier, column)
    raise ValueError(f"Cannot infer a datatype for result expression: {expression}")


def _unique_source_column_type(connection, source_tables: list[tuple[str, str, str]], column: str,
                               required: bool = True) -> str | None:
    matches = []
    for schema, table, _alias in source_tables:
        data_type = _find_column_type(connection, schema, table, column)
        if data_type:
            matches.append((schema, table, data_type))
    if len(matches) == 1:
        return matches[0][2]
    if len(matches) > 1:
        locations = ', '.join(f'{schema}.{table}' for schema, table, _ in matches)
        raise ValueError(f'Ambiguous unqualified result column {column}; found in {locations}.')
    if required:
        raise ValueError(f'PostgreSQL metadata not found for unqualified result column {column}.')
    return None


def _column_type(connection, schema: str, table: str, column: str) -> str:
    data_type = _find_column_type(connection, schema, table, column)
    if data_type is None:
        raise ValueError(f"PostgreSQL metadata not found for {schema}.{table}.{column}.")
    return data_type


def _find_column_type(connection, schema: str, table: str, column: str) -> str | None:
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT pg_catalog.format_type(a.atttypid,a.atttypmod)
               FROM pg_catalog.pg_attribute a
               JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
               JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
               WHERE lower(n.nspname)=lower(%s) AND lower(c.relname)=lower(%s)
                 AND lower(a.attname)=lower(%s) AND a.attnum>0 AND NOT a.attisdropped""",
            (schema, table, column),
        )
        row = cursor.fetchone()
    return row[0] if row else None
