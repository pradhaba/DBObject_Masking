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
    parameters = _declared_symbol_types(sql)
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


def collect_result_inference_errors(sql: str, connection, default_schema: str = "dba") -> list[tuple[str, ValueError]]:
    """Collect every independently unresolved result expression for review."""
    parameters = _declared_symbol_types(sql)
    issues = []
    seen = set()
    for body, sources in _outer_select_contexts(sql, default_schema):
        for expression in _split_sql_list(body):
            try:
                _resolve_expression(expression, connection, parameters, default_schema, sources)
            except ValueError as exc:
                key = (expression.strip().lower(), str(exc))
                if key not in seen:
                    seen.add(key)
                    issues.append((expression.strip(), exc))
    return issues


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
    for start, end, replacement in sorted(replacements, key=lambda item: item[0], reverse=True):
        sql = sql[:start] + replacement + sql[end:]
    return sql


def qualify_unqualified_result_columns(sql: str, connection, default_schema: str = "dba", source_catalog=None) -> str:
    """Qualify unqualified columns throughout every SELECT query scope.

    A name is bound only when exactly one table visible to that SELECT contains
    it. PostgreSQL catalog metadata is supplemented with columns declared by
    local temporary tables in the routine being migrated.
    """
    replacements: list[tuple[int, int, str]] = []
    order_qualifiers: dict[str, str] = {}
    local_columns = _local_temporary_table_columns(sql)
    metadata_cache: dict[tuple[str, str, str], str | None] = {}
    occupied: set[tuple[int, int]] = set()
    nested_scopes: list[tuple[int, int]] = []

    def column_type(schema: str, table: str, column: str) -> str | None:
        local = local_columns.get(table.lower(), {})
        if column.lower() in local:
            return local[column.lower()]
        if source_catalog is not None:
            source_type = source_catalog.column_type(schema, table, column)
            if source_type:
                return source_type
        if not hasattr(connection, 'cursor'):
            return None
        key = (schema.lower(), table.lower(), column.lower())
        if key not in metadata_cache:
            metadata_cache[key] = _find_column_type(connection, schema, table, column)
        return metadata_cache[key]

    def unique_match(sources, column):
        matches = [(table, alias) for schema, table, alias in sources if column_type(schema, table, column)]
        return matches[0] if len(matches) == 1 else None

    # Innermost/rightmost queries are handled first. Their complete ranges are
    # then excluded from parent scopes so correlated nesting cannot bind a
    # child column to an unrelated outer table.
    for start, end in reversed(_all_select_spans(sql)):
        sources = _select_source_tables(sql, end, default_schema)
        if not sources:
            continue
        expressions = _split_sql_list(sql[start:end])
        expression_cursor = start
        for expression in expressions:
            expression_at = sql.find(expression, expression_cursor, end)
            expression_cursor = expression_at + len(expression) if expression_at >= 0 else expression_cursor
            direct = re.fullmatch(rf'\s*(?P<column>{SQL_IDENT})\s*', expression)
            if not direct or expression_at < 0:
                continue
            column_token = direct.group('column')
            column = _unquote_identifier(column_token)
            match = unique_match(sources, column)
            if not match:
                continue
            table, alias = match
            qualifier = alias if alias.lower() != table.lower() else table
            qualified = f'{_quote_identifier(qualifier)}.{column_token}'
            token_offset = expression.find(column_token)
            token_span = (expression_at + token_offset, expression_at + token_offset + len(column_token))
            replacements.append((*token_span, qualified))
            occupied.add(token_span)
            order_qualifiers[column.lower()] = qualified

        scope_end = _select_scope_end(sql, end)
        scope = sql[start:scope_end]
        # Query relation names, aliases, SQL words, function names, variables,
        # and already-qualified columns are naturally excluded below. Remaining
        # names are checked against every relation in this SELECT scope.
        relation_names = {
            value.lower() for _schema, table, alias in sources for value in (table, alias)
        }
        for token in re.finditer(SQL_IDENT, scope):
            absolute_start = start + token.start()
            absolute_end = start + token.end()
            if (absolute_start, absolute_end) in occupied or any(
                child_start <= absolute_start < child_end for child_start, child_end in nested_scopes
            ) or _inside_single_quoted_sql(sql, absolute_start):
                continue
            before = sql[:absolute_start]
            after = sql[absolute_end:]
            if re.search(r'(?:\.|@|:)\s*$', before) or re.match(r'\s*\.', after):
                continue
            if re.match(r'\s*\(', after):
                continue
            value = _unquote_identifier(token.group(0))
            if value.lower() in relation_names or re.search(
                r'\b(?:FROM|JOIN|AS|INTO|UPDATE|TABLE)\s*$', before, re.I
            ):
                continue
            match = unique_match(sources, value)
            if not match:
                continue
            table, alias = match
            qualifier = alias if alias.lower() != table.lower() else table
            replacements.append((absolute_start, absolute_end, f'{_quote_identifier(qualifier)}.{token.group(0)}'))
            occupied.add((absolute_start, absolute_end))
        nested_scopes.append((start - len('SELECT'), scope_end))

    for start, end, replacement in sorted(replacements, key=lambda item: item[0], reverse=True):
        sql = sql[:start] + replacement + sql[end:]
    for column, qualified in order_qualifiers.items():
        sql = re.sub(
            rf'(\bORDER\s+BY\s+)"?{re.escape(column)}"?(?=\s+(?:ASC|DESC)\b|\s*[,;])',
            lambda match: match.group(1) + qualified,
            sql,
            flags=re.IGNORECASE,
        )
    return sql


def _inside_single_quoted_sql(sql: str, position: int) -> bool:
    """Return whether a source offset occurs inside a SQL string literal."""
    quote = False
    index = 0
    while index < position:
        if sql[index] == "'":
            if quote and index + 1 < len(sql) and sql[index + 1] == "'":
                index += 2
                continue
            quote = not quote
        index += 1
    return quote


def _local_temporary_table_columns(sql: str) -> dict[str, dict[str, str]]:
    """Collect local temporary-table columns before those tables exist in PostgreSQL."""
    tables: dict[str, dict[str, str]] = {}
    declaration = re.compile(
        rf'\b(?:DECLARE\s+LOCAL\s+TEMPORARY|CREATE\s+TEMPORARY)\s+TABLE\s+'
        rf'(?:pg_temp\.)?(?P<table>{SQL_IDENT})\s*\(',
        re.I,
    )
    for match in declaration.finditer(sql):
        open_at = match.end() - 1
        depth, quote, cursor = 0, None, open_at
        while cursor < len(sql):
            char = sql[cursor]
            if quote:
                if char == quote:
                    quote = None
            elif char in ("'", '"'):
                quote = char
            elif char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        if depth:
            continue
        columns = {}
        for item in _split_sql_list(sql[open_at + 1:cursor]):
            column = re.match(rf'\s*(?P<name>{SQL_IDENT})\s+(?P<type>{IDENT}(?:\s*\([^)]*\))?)', item, re.I)
            if column:
                columns[_unquote_identifier(column.group('name')).lower()] = _normalize_declared_type(column.group('type'))
        tables[_unquote_identifier(match.group('table')).lower()] = columns
    return tables


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
            if depth == 0:
                break
            depth -= 1
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


def _all_select_spans(sql: str) -> list[tuple[int, int]]:
    """Return SELECT-list spans at every nesting depth."""
    results = []
    index = 0
    quote = None
    select_word = re.compile(r'\bSELECT\b', re.I)
    from_word = re.compile(r'\bFROM\b', re.I)
    while index < len(sql):
        char = sql[index]
        if quote:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        selected = select_word.match(sql, index)
        if not selected:
            index += 1
            continue
        cursor, depth, local_quote = selected.end(), 0, None
        while cursor < len(sql):
            current = sql[cursor]
            if local_quote:
                if current == local_quote:
                    if cursor + 1 < len(sql) and sql[cursor + 1] == local_quote:
                        cursor += 2
                        continue
                    local_quote = None
            elif current in ("'", '"'):
                local_quote = current
            elif current == '(':
                depth += 1
            elif current == ')':
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and from_word.match(sql, cursor):
                results.append((selected.end(), cursor))
                break
            cursor += 1
        index = selected.end()
    return results


def _select_scope_end(sql: str, from_position: int) -> int:
    """Find the end of the SELECT containing a known FROM position."""
    depth, quote, cursor = 0, None, from_position
    procedural = re.compile(r'\b(?:DO|LOOP|THEN|END\s+IF|END\s+LOOP|END\s+FOR)\b', re.I)
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
            if depth == 0:
                return cursor
            depth -= 1
        elif depth == 0 and (char == ';' or procedural.match(sql, cursor)):
            return cursor
        cursor += 1
    return len(sql)


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
        match = re.match(rf'\s*(?:IN|OUT|INOUT)?\s*@?(?P<name>{IDENT})\s+(?P<type>{IDENT}(?:\s*\([^)]*\))?)', item, re.IGNORECASE)
        if match:
            result[match.group('name').lower()] = _normalize_declared_type(match.group('type'))
    return result


def _declared_symbol_types(sql: str) -> dict[str, str]:
    symbols = _parameter_types(sql)
    for match in re.finditer(
        rf'\bDECLARE\s+@?(?P<name>{IDENT})\s+(?P<type>{IDENT}(?:\s*\([^)]*\))?)',
        sql,
        re.IGNORECASE,
    ):
        symbols[match.group('name').lower()] = _normalize_declared_type(match.group('type'))
    return symbols


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
    asa_symbol = re.fullmatch(rf'@(?P<name>{IDENT})', value)
    if asa_symbol and asa_symbol.group('name').lower() in parameters:
        name = asa_symbol.group('name')
        return output_name or name, parameters[name.lower()]

    if re.fullmatch(r"'(?:''|[^'])*'", value, re.DOTALL) or re.fullmatch(r'NULL', value, re.IGNORECASE):
        return output_name or 'value', 'unknown'
    if re.fullmatch(r'[+-]?\d+', value):
        return output_name or 'value', 'integer'
    if re.fullmatch(r'[+-]?(?:\d+\.\d*|\d*\.\d+)', value):
        return output_name or 'value', 'numeric'
    if re.fullmatch(r'(?:TRUE|FALSE)', value, re.IGNORECASE):
        return output_name or 'value', 'boolean'

    unqualified = re.fullmatch(SQL_IDENT, value)
    if unqualified and source_tables:
        column = _unquote_identifier(unqualified.group(0))
        return output_name or column, _unique_source_column_type(connection, source_tables, column)

    if output_name and source_tables:
        matching_type = _unique_source_column_type(connection, source_tables, output_name, required=False)
        if matching_type:
            return output_name, matching_type

    function_call = re.fullmatch(
        rf'(?:(?P<schema>{SQL_IDENT})\.)?(?P<function>{SQL_IDENT})\s*\((?P<arguments>.*)\)',
        value,
        re.IGNORECASE | re.DOTALL,
    )
    if function_call:
        schema = (_unquote_identifier(function_call.group('schema'))
                  if function_call.group('schema') else default_schema)
        function = _unquote_identifier(function_call.group('function'))
        argument_text = function_call.group('arguments').strip()
        arguments = _split_sql_list(argument_text) if argument_text else []
        argument_types = []
        for argument in arguments:
            try:
                argument_types.append(
                    _resolve_expression(argument, connection, parameters, default_schema, source_tables)[1]
                )
            except ValueError as exc:
                if not str(exc).startswith('Cannot infer a datatype for result expression:'):
                    raise
                argument_types.append('unknown')
        builtin_type = _source_builtin_return_type(function, argument_types) if not function_call.group('schema') else None
        if builtin_type:
            return output_name or function, builtin_type
        if function_call.group('schema'):
            return output_name or function, _function_return_type(
                connection, schema, function, argument_types
            )
        return output_name or function, _unqualified_function_return_type(
            connection, default_schema, function, argument_types
        )

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


def _function_return_type(connection, schema: str, function: str, argument_types: list[str]) -> str:
    """Resolve a destination PostgreSQL function overload and return its declared type."""
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT pg_catalog.format_type(p.prorettype, NULL),
                      ARRAY(
                          SELECT arg_oid
                          FROM unnest(p.proargtypes) WITH ORDINALITY AS oid_args(arg_oid, position)
                          ORDER BY position
                      ),
                      ARRAY(
                          SELECT pg_catalog.format_type(arg_type, NULL)
                          FROM unnest(p.proargtypes) WITH ORDINALITY AS args(arg_type, position)
                          ORDER BY position
                      ),
                      pg_catalog.pg_get_function_identity_arguments(p.oid),
                      p.proretset
               FROM pg_catalog.pg_proc p
               JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
               WHERE lower(n.nspname) = lower(%s)
                 AND lower(p.proname) = lower(%s)
                 AND %s BETWEEN (p.pronargs - p.pronargdefaults) AND p.pronargs""",
            (schema, function, len(argument_types)),
        )
        candidates = cursor.fetchall()
    signature = f"{schema}.{function}({', '.join(argument_types)})"
    if not candidates:
        raise ValueError(f"PostgreSQL function metadata not found for {signature}.")

    exact = [candidate for candidate in candidates
             if _function_arguments_match(argument_types, candidate[2])]
    compatible = exact or [
        candidate for candidate in candidates
        if _function_arguments_implicitly_castable(connection, argument_types, candidate[1])
    ]
    if not compatible:
        available = '; '.join(candidate[3] or '(no arguments)' for candidate in candidates)
        raise ValueError(
            f"No compatible PostgreSQL overload found for {signature}. Available arguments: {available}."
        )
    if len(compatible) > 1:
        available = '; '.join(candidate[3] or '(no arguments)' for candidate in compatible)
        raise ValueError(f"Ambiguous PostgreSQL function overload for {signature}: {available}.")
    return_type, _arg_oids, _arg_names, identity_arguments, returns_set = compatible[0]
    if returns_set:
        raise ValueError(
            f"Set-returning PostgreSQL function {schema}.{function}({identity_arguments}) "
            "cannot be inferred as one scalar result expression."
        )
    return return_type


def _unqualified_function_return_type(connection, default_schema: str, function: str,
                                      argument_types: list[str]) -> str:
    """Resolve PostgreSQL built-ins first, then project routines.

    pg_catalog is PostgreSQL's complete, server-version-specific built-in
    catalog.  Using it here covers extensions and overload changes without a
    permanently incomplete hard-coded return-type list.
    """
    errors = []
    for schema in ('pg_catalog', default_schema):
        try:
            return _function_return_type(connection, schema, function, argument_types)
        except ValueError as exc:
            if not str(exc).startswith('PostgreSQL function metadata not found for'):
                raise
            errors.append(exc)
    signature = f"{function}({', '.join(argument_types)})"
    raise ValueError(
        f"PostgreSQL function metadata not found for unqualified {signature}; "
        f"searched pg_catalog and {default_schema}."
    ) from errors[-1]


def _source_builtin_return_type(function: str, argument_types: list[str]) -> str | None:
    """Types for ASA built-ins that are rewritten before PostgreSQL deployment."""
    name = function.lower()
    # Unqualified aggregate calls are PostgreSQL built-ins.  Resolve their
    # result contract locally instead of looking for (for example) dba.min in
    # pg_proc.  Explicitly schema-qualified calls still take the normal
    # user-defined-function metadata path in _resolve_expression.
    if name in {'min', 'max'} and len(argument_types) == 1:
        return argument_types[0]
    if name == 'count':
        return 'bigint'
    if name == 'sum' and len(argument_types) == 1:
        argument_type = _canonical_type(argument_types[0])
        if argument_type in {'smallint', 'integer'}:
            return 'bigint'
        if argument_type == 'bigint':
            return 'numeric'
        return argument_types[0]
    if name == 'avg' and len(argument_types) == 1:
        argument_type = _canonical_type(argument_types[0])
        if argument_type in {'smallint', 'integer', 'bigint', 'numeric', 'decimal'}:
            return 'numeric'
        return argument_types[0]
    if name in {'string', 'list'}:
        return 'text'
    if name in {'len', 'length', 'char_length'}:
        return 'integer'
    if name in {'lower', 'upper', 'trim', 'ltrim', 'rtrim'}:
        return 'text'
    if name in {'coalesce', 'isnull'}:
        return next((data_type for data_type in argument_types if data_type != 'unknown'), 'unknown')
    return None


def _function_arguments_match(actual: list[str], declared: list[str]) -> bool:
    if len(actual) > len(declared):
        return False
    return all(left.lower() == 'unknown' or _canonical_type(left) == _canonical_type(right)
               for left, right in zip(actual, declared))


def _canonical_type(value: str) -> str:
    normalized = re.sub(r'\s+', ' ', value.strip().lower())
    normalized = re.sub(r'\([^)]*\)$', '', normalized).strip()
    aliases = {
        'int': 'integer', 'int4': 'integer', 'int2': 'smallint', 'int8': 'bigint',
        'varchar': 'character varying', 'decimal': 'numeric', 'bool': 'boolean',
        'timestamp without time zone': 'timestamp',
    }
    return aliases.get(normalized, normalized)


def _function_arguments_implicitly_castable(connection, actual: list[str], declared_oids: list[int]) -> bool:
    if len(actual) > len(declared_oids):
        return False
    for actual_type, declared_oid in zip(actual, declared_oids):
        if actual_type.lower() == 'unknown':
            continue
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT source.oid = %s::oid OR EXISTS (
                           SELECT 1 FROM pg_catalog.pg_cast pc
                           WHERE pc.castsource = source.oid
                             AND pc.casttarget = %s::oid
                             AND pc.castcontext = 'i'
                       )
                   FROM pg_catalog.pg_type source
                   WHERE source.oid = pg_catalog.to_regtype(%s)::oid""",
                (declared_oid, declared_oid, actual_type),
            )
            row = cursor.fetchone()
        if not row or not row[0]:
            return False
    return True
