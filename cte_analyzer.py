"""Conservative PostgreSQL temporary-table to CTE suitability analysis."""

from __future__ import annotations

import re


def apply_readability_ctes(sql: str) -> tuple[str, list[dict]]:
    """Promote provably independent complex FROM-derived relations to WITH.

    To keep the rewrite unambiguous, only one candidate per SQL statement is
    transformed, and statements that already contain WITH are left unchanged.
    """
    candidates = _derived_candidates(sql)
    conversions = []
    for candidate in reversed(candidates):
        statement_start = max(sql.rfind(';', 0, candidate['from_start']) + 1, 0)
        return_query = sql.rfind('RETURN QUERY', statement_start, candidate['from_start'])
        search_start = return_query + len('RETURN QUERY') if return_query >= 0 else statement_start
        select_match = re.search(r'\bSELECT\b', sql[search_start:candidate['from_start']], re.I)
        if not select_match:
            continue
        select_start = search_start + select_match.start()
        if re.search(r'\bWITH\b', sql[search_start:select_start], re.I):
            continue
        statement_end = sql.find(';', candidate['alias_end'])
        statement_end = len(sql) if statement_end < 0 else statement_end
        peers = [item for item in candidates if select_start <= item['from_start'] < statement_end]
        if len(peers) != 1:
            continue
        with_clause = f"WITH {candidate['alias']} AS (\n{candidate['body']}\n)\n"
        replacement = f"{candidate['keyword']} {candidate['alias']}"
        sql = (
            sql[:select_start] + with_clause + sql[select_start:candidate['from_start']]
            + replacement + sql[candidate['alias_end']:]
        )
        conversions.append({
            'table': candidate['alias'], 'kind': 'derived_query', 'eligible': True,
            'mode': 'implemented_readability_cte', 'insert_count': 0, 'read_count': 1,
            'reasons': ['complex independent derived relation was promoted to a named CTE'],
        })
    conversions.reverse()
    return sql, conversions


def analyze_cte_suitability(sql: str) -> list[dict]:
    """Return one strict decision for every temporary table created in *sql*.

    This analyzer recommends; it does not rewrite.  A recommendation is emitted
    only when the intermediate relation is append-only, has compatible INSERT
    projections, and is consumed by exactly one later query.
    """
    creations = list(re.finditer(
        r'\bCREATE\s+TEMP(?:ORARY)?\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?'
        r'(?:(?:pg_temp|[A-Za-z_]\w*)\.)?"?([A-Za-z_]\w*)"?\s*\(',
        sql,
        re.I,
    ))
    decisions = []
    for creation in creations:
        table = creation.group(1)
        quoted = re.escape(table)
        inserts = list(re.finditer(
            rf'\bINSERT\s+INTO\s+(?:(?:pg_temp)\.)?"?{quoted}"?\s*'
            r'(?:\((.*?)\))?\s*SELECT\b',
            sql,
            re.I | re.S,
        ))
        mutations = []
        for operation, pattern in (
            ('UPDATE', rf'\bUPDATE\s+(?:(?:pg_temp)\.)?"?{quoted}"?\b'),
            ('DELETE', rf'\bDELETE\s+FROM\s+(?:(?:pg_temp)\.)?"?{quoted}"?\b'),
            ('TRUNCATE', rf'\bTRUNCATE(?:\s+TABLE)?\s+(?:(?:pg_temp)\.)?"?{quoted}"?\b'),
            ('INDEX', rf'\bCREATE\s+(?:UNIQUE\s+)?INDEX\b[^;]*\bON\s+(?:(?:pg_temp)\.)?"?{quoted}"?\b'),
        ):
            if re.search(pattern, sql, re.I | re.S):
                mutations.append(operation)

        reads = list(re.finditer(
            rf'\b(?:FROM|JOIN)\s+(?:(?:pg_temp)\.)?"?{quoted}"?(?:\s+AS)?\s+[A-Za-z_]\w*|'
            rf'\b(?:FROM|JOIN)\s+(?:(?:pg_temp)\.)?"?{quoted}"?\b',
            sql,
            re.I,
        ))
        reasons = []
        if not inserts:
            reasons.append('no INSERT ... SELECT population statement was found')
        if mutations:
            reasons.append('intermediate table is modified by ' + ', '.join(mutations))
        if len(reads) != 1:
            reasons.append(f'intermediate table is consumed by {len(reads)} query references; exactly one is required')

        column_lists = []
        for insert in inserts:
            raw = insert.group(1)
            column_lists.append(tuple(_normalize_identifier(item) for item in _split_columns(raw)) if raw else ())
        if len(inserts) > 1:
            if not all(column_lists) or len(set(column_lists)) != 1:
                reasons.append('multiple INSERT branches do not have the same explicit target columns')
            if not _inserts_are_guarded_or_sequentially_append_only(sql, inserts):
                reasons.append('multiple INSERT branches cannot be proven append-only')

        eligible = not reasons
        mode = 'single_cte' if eligible and len(inserts) == 1 else ('union_all_cte' if eligible else 'keep_temporary_table')
        decisions.append({
            'table': table,
            'eligible': eligible,
            'mode': mode,
            'insert_count': len(inserts),
            'read_count': len(reads),
            'reasons': reasons or [
                'append-only intermediate result has compatible branches and one final consumer'
            ],
        })
    decisions.extend(analyze_query_cte_opportunities(sql))
    return decisions


def analyze_query_cte_opportunities(sql: str) -> list[dict]:
    """Find strict readability CTE candidates that do not use temp tables.

    Only complex derived relations in FROM/JOIN are recommended. Scalar and
    correlated subqueries are intentionally excluded because lifting them can
    change cardinality or evaluation semantics.
    """
    decisions = []
    for candidate in _derived_candidates(sql):
        decisions.append({
            'table': candidate['alias'],
            'kind': 'derived_query',
            'eligible': True,
            'mode': 'readability_cte',
            'insert_count': 0,
            'read_count': 1,
            'reasons': [
                'complex non-LATERAL derived relation can be named for readability ('
                + ', '.join(candidate['features']) + ')'
            ],
        })
    return decisions


def _derived_candidates(sql: str) -> list[dict]:
    candidates = []
    pattern = re.compile(r'\b(FROM|JOIN)\s*\(', re.I)
    position = 0
    while match := pattern.search(sql, position):
        open_paren = sql.find('(', match.start())
        close_paren = _matching_parenthesis(sql, open_paren)
        if close_paren is None:
            break
        body = sql[open_paren + 1:close_paren].strip()
        alias_match = re.match(r'\s*(?:AS\s+)?"?([A-Za-z_]\w*)"?', sql[close_paren + 1:], re.I)
        alias = alias_match.group(1) if alias_match else ''
        complex_features = []
        for label, expression in (
            ('aggregation', r'\bGROUP\s+BY\b|\bHAVING\b'),
            ('set operation', r'\b(?:UNION|INTERSECT|EXCEPT)\b'),
            ('window calculation', r'\bOVER\s*\('),
            ('distinct projection', r'\bSELECT\s+DISTINCT\b'),
            ('nested query stage', r'\(\s*SELECT\b'),
        ):
            if re.search(expression, body, re.I):
                complex_features.append(label)
        if (
            alias
            and re.match(r'^(?:SELECT|WITH)\b', body, re.I)
            and complex_features
            and not re.search(r'\bLATERAL\b', sql[max(0, match.start() - 12):open_paren], re.I)
        ):
            alias_offset = alias_match.end() if alias_match else 0
            candidates.append({
                'keyword': match.group(1).upper(), 'from_start': match.start(),
                'alias_end': close_paren + 1 + alias_offset,
                'body': body, 'alias': alias, 'features': complex_features,
            })
        position = close_paren + 1
    return candidates


def _matching_parenthesis(text: str, opening: int) -> int | None:
    depth = 0
    quote = None
    index = opening
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in "'\"":
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _split_columns(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(',') if item.strip()]


def _normalize_identifier(value: str) -> str:
    return value.strip().strip('"').lower()


def _inserts_are_guarded_or_sequentially_append_only(sql: str, inserts: list[re.Match]) -> bool:
    """Reject multiple writes if destructive operations occur between them."""
    start, end = inserts[0].start(), inserts[-1].end()
    between = sql[start:end]
    return not re.search(r'\b(?:UPDATE|DELETE\s+FROM|TRUNCATE)\b', between, re.I)


def cte_trace(decisions: list[dict]) -> list[dict]:
    trace = []
    for decision in decisions:
        recommendation = {
            'single_cte': 'Suitable for conversion to one CTE',
            'union_all_cte': 'Suitable for a CTE with UNION ALL branches',
            'readability_cte': 'Suitable for a named readability CTE',
            'implemented_readability_cte': 'Converted to a named readability CTE',
            'keep_temporary_table': 'Retain PostgreSQL temporary table',
        }[decision['mode']]
        trace.append({
            'line': 'cte-analysis',
            'source': (
                f"Derived query {decision['table']}"
                if decision.get('kind') == 'derived_query'
                else f"Temporary table {decision['table']}"
            ),
            'output': f"{recommendation}: {'; '.join(decision['reasons'])}",
            'rules': [{
                'rule_id': 'strict-cte-suitability',
                'rule_code': 'strict-cte-suitability',
                'priority': 1970,
                'matches': 1,
            }],
        })
    return trace
