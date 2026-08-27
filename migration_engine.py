"""Database-backed migration skill execution."""

from __future__ import annotations

import re
import json

from database import get_active_skill_version
from masker import mask_text, unmask_text


def migrate_text(text: str, source_dialect: str, target_dialect: str, database_path=None,
                 target_override="auto", metadata_connection=None, formatter_indent="4 spaces"):
    """Mask identifiers, apply the selected DB skill, then restore target names."""
    text = _strip_sql_comments(text)
    _validate_sql_quoted_tokens(text)
    if source_dialect == "sybase_asa" and not re.search(
        r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:PROC(?:EDURE)?|FUNCTION)\b", text, re.IGNORECASE
    ):
        raise ValueError("The active SAP ASA skill supports procedures and functions only.")
    if database_path is None:
        skill = get_active_skill_version(source_dialect, target_dialect)
    else:
        skill = get_active_skill_version(source_dialect, target_dialect, database_path)
    if skill is None:
        raise ValueError(f"No active approved migration skill for {source_dialect} → {target_dialect}.")
    if source_dialect == "sybase_asa" and target_dialect == "postgresql":
        from dynamic_temp_renderer import inline_simple_dynamic_sql, render_dynamic_temp_report, supports_dynamic_temp_report
        text, dynamic_inline_count = inline_simple_dynamic_sql(text)
        if supports_dynamic_temp_report(text):
            _, mapping = mask_text(text, source_dialect, embed_mapping=False)
            from postgres_formatter import format_postgresql_routine
            rendered = _cast_returns_table_text_outputs(render_dynamic_temp_report(text))
            rendered = format_postgresql_routine(rendered, formatter_indent)
            from cte_analyzer import analyze_cte_suitability, cte_trace
            cte_analysis = analyze_cte_suitability(rendered)
            skill = dict(skill)
            skill.update({
                "target_object_type": "function",
                "classification_reason": "Dynamic report returns rows; converted to a static parameterized temp-table function.",
                "classification_rule": "dynamic-temp-result-function",
                "analysis": analyze_asa_procedure(text),
                "human_override": target_override if target_override != "auto" else None,
                "routine_language": "plpgsql",
                "cte_analysis": cte_analysis,
                "diagnostics": [],
                "technical_status": "success",
                "trace": [{
                    "line": "renderer", "source": "EXECUTE IMMEDIATE with tmp_records",
                    "output": "Static parameterized INSERT branches with a PostgreSQL temporary table",
                    "rules": [{"rule_id": "dynamic-temp-static-renderer", "rule_code": "dynamic-temp-static-renderer", "priority": 1950, "matches": 1}],
                }] + cte_trace(cte_analysis),
            })
            return rendered, mapping, skill
    else:
        dynamic_inline_count = 0
    working_text = text
    source_scalar_return_type = _source_scalar_return_type(text)
    if source_dialect == "sybase_asa" and target_dialect == "postgresql":
        from result_metadata import align_result_selects, qualify_unqualified_result_columns
        if metadata_connection is not None and source_scalar_return_type is None:
            working_text = qualify_unqualified_result_columns(working_text, metadata_connection)
        working_text = align_result_selects(working_text)
    if _is_already_masked(working_text):
        masked, mapping = working_text, _identity_mapping(working_text)
    else:
        masked, mapping = mask_text(working_text, source_dialect, embed_mapping=False)
    migrated, trace = _apply_rules_with_trace(masked, skill["rules"], mapping, source_dialect, target_dialect)
    analysis = analyze_asa_procedure(text) if source_dialect == "sybase_asa" else {}
    target_type, reason, classification_rule = classify_postgresql_routine(text, target_override)
    inferred_result_columns = None
    diagnostics = []
    if (target_type == "function" and target_dialect == "postgresql"
            and metadata_connection is not None and source_scalar_return_type is None):
        from result_metadata import infer_returns_table
        try:
            inferred_result_columns = infer_returns_table(working_text, metadata_connection)
        except ValueError as exc:
            from result_metadata import collect_result_inference_errors
            try:
                issues = collect_result_inference_errors(working_text, metadata_connection)
            except Exception:
                issues = []
            if issues:
                diagnostics.extend(
                    _recoverable_migration_diagnostic(working_text, issue, expression)
                    for expression, issue in issues
                )
            else:
                diagnostics.append(_recoverable_migration_diagnostic(working_text, exc))
    if source_dialect == "sybase_asa" and target_dialect == "postgresql":
        migrated, renderer_trace, routine_language = render_postgresql_routine(
            migrated, target_type, inferred_result_columns
        )
        trace.extend(renderer_trace)
        if dynamic_inline_count:
            trace.insert(0, {
                "line": "preprocessor", "source": "SET dynamic SQL; EXECUTE IMMEDIATE",
                "output": "Static SQL",
                "rules": [{"rule_id": "inline-simple-dynamic-sql", "rule_code": "inline-simple-dynamic-sql", "priority": 1960, "matches": dynamic_inline_count}],
            })
    else:
        routine_language = None
    restored = unmask_text(migrated, mapping, target_dialect)
    if target_dialect == "postgresql":
        from cte_analyzer import apply_readability_ctes
        restored, implemented_ctes = apply_readability_ctes(restored)
        from postgres_formatter import format_postgresql_routine
        restored = format_postgresql_routine(restored, formatter_indent)
        restored = _annotate_unresolved_metadata(restored, diagnostics)
        from cte_analyzer import analyze_cte_suitability, cte_trace
        cte_analysis = implemented_ctes + analyze_cte_suitability(restored)
        trace.extend(cte_trace(cte_analysis))
    else:
        cte_analysis = []
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
    skill["cte_analysis"] = cte_analysis
    skill["diagnostics"] = diagnostics
    skill["technical_status"] = "needs_modification" if any(
        item["severity"] == "error" and not item["resolved"] for item in diagnostics
    ) else "success"
    return restored, mapping, skill


def _annotate_unresolved_metadata(sql: str, diagnostics: list[dict]) -> str:
    """Keep unresolved result expressions and mark them visibly in draft SQL."""
    unresolved = [
        item for item in diagnostics
        if not item.get('resolved') and item.get('code') in {
            'COLUMN_METADATA_NOT_FOUND', 'FUNCTION_METADATA_NOT_FOUND',
            'FUNCTION_OVERLOAD_UNRESOLVED', 'AMBIGUOUS_RESULT_EXPRESSION',
            'RESULT_DATATYPE_UNRESOLVED',
        }
    ]
    if not unresolved:
        return sql

    annotated = sql
    for item in unresolved:
        expression = (item.get('expression') or '').strip()
        if not expression:
            continue
        # Formatting may change whitespace and keyword case, so match tokens
        # case-insensitively while preserving the rendered expression itself.
        pattern = re.escape(expression)
        pattern = re.sub(r'(?:\\\s)+', r'\\s+', pattern)
        marker = (
            f" /* MIGRATION REVIEW [{item['code']}]: datatype unidentified; "
            "original expression preserved */"
        )
        if marker in annotated:
            continue
        annotated, count = re.subn(
            pattern,
            lambda match: match.group(0) + marker,
            annotated,
            count=1,
            flags=re.IGNORECASE,
        )
        if count == 0:
            # Table aliasing can turn table.column into ali.column.  Match the
            # terminal result column within SELECT lists, not arbitrary uses in
            # JOIN/WHERE clauses, and annotate the rendered expression.
            identifiers = re.findall(r'"([^"]+)"|\b([A-Za-z_][A-Za-z0-9_$]*)\b', expression)
            names = [quoted or plain for quoted, plain in identifiers]
            terminal_column = names[-1] if names else ''
            from result_metadata import _outer_select_spans
            for start, end in reversed(_outer_select_spans(annotated)):
                body = annotated[start:end]
                expressions = _split_sql_expressions(body)
                changed = False
                for index, rendered_expression in enumerate(expressions):
                    if re.search(
                        rf'(?:^|\.)\s*"?{re.escape(terminal_column)}"?(?:\s+AS\s+|\s*$)',
                        rendered_expression,
                        re.IGNORECASE,
                    ):
                        expressions[index] = rendered_expression.rstrip() + marker
                        changed = True
                        break
                if changed:
                    leading = re.match(r'\s*', body).group(0)
                    trailing = re.search(r'\s*$', body).group(0)
                    annotated = annotated[:start] + leading + ',\n    '.join(expressions) + trailing + annotated[end:]
                    break

    if re.search(r'\bRETURNS\s+SETOF\s+RECORD\b', annotated, re.IGNORECASE):
        annotated = re.sub(
            r'(\bRETURNS\s+SETOF\s+RECORD\b)',
            r'\1 /* MIGRATION REVIEW: one or more result datatypes are unidentified; see Error Review */',
            annotated,
            count=1,
            flags=re.IGNORECASE,
        )
    return annotated


def _recoverable_migration_diagnostic(source: str, error: ValueError, expression: str = "") -> dict:
    """Convert a metadata inference failure into an actionable review issue."""
    message = str(error)
    if not expression:
        match = re.search(r'(?:result expression|for)\s*:\s*(.+?)(?:\.|$)', message, re.IGNORECASE | re.DOTALL)
        if match:
            expression = match.group(1).strip()
    position = source.lower().find(expression.lower()) if expression else -1
    line = source.count('\n', 0, position) + 1 if position >= 0 else None
    column = position - source.rfind('\n', 0, position) if position >= 0 else None
    if 'function metadata not found' in message.lower():
        code = 'FUNCTION_METADATA_NOT_FOUND'
        category = 'function_return_type'
        suggestion = 'Migrate the called function first or manually specify/cast the result datatype.'
    elif 'overload' in message.lower():
        code = 'FUNCTION_OVERLOAD_UNRESOLVED'
        category = 'function_return_type'
        suggestion = 'Qualify the function and cast its arguments to select one destination overload.'
    elif 'metadata not found' in message.lower():
        code = 'COLUMN_METADATA_NOT_FOUND'
        category = 'result_datatype'
        suggestion = 'Verify the destination schema/table/column or manually specify the return datatype.'
    elif 'ambiguous' in message.lower():
        code = 'AMBIGUOUS_RESULT_EXPRESSION'
        category = 'result_datatype'
        suggestion = 'Qualify the column or function call with its table/schema and migrate again.'
    else:
        code = 'RESULT_DATATYPE_UNRESOLVED'
        category = 'result_datatype'
        suggestion = 'Review the expression and manually provide a compatible PostgreSQL return datatype or cast.'
    return {
        'code': code,
        'severity': 'error',
        'category': category,
        'message': message,
        'expression': expression,
        'line': line,
        'column': column,
        'suggestion': suggestion,
        'migration_continued': True,
        'resolved': False,
    }


def _strip_sql_comments(text: str) -> str:
    """Remove SQL comments without altering strings or source line structure."""
    output = []
    index = 0
    quote = None
    while index < len(text):
        char = text[index]
        if quote:
            output.append(char)
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    output.append(text[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            output.append(char)
            index += 1
            continue
        if text[index:index + 2] in {'--', '//'}:
            index += 2
            while index < len(text) and text[index] not in '\r\n':
                index += 1
            continue
        if text[index:index + 2] == '/*':
            index += 2
            output.append(' ')
            while index < len(text) and text[index:index + 2] != '*/':
                if text[index] in '\r\n':
                    output.append(text[index])
                index += 1
            index += 2 if index < len(text) else 0
            continue
        output.append(char)
        index += 1
    return ''.join(output)


def _validate_sql_quoted_tokens(text: str) -> None:
    """Fail early when a SQL string or quoted identifier is not terminated."""
    quote = None
    quote_start = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
                quote_start = None
        elif char in ("'", '"'):
            quote = char
            quote_start = index
        index += 1
    if quote is None:
        return
    line = text.count('\n', 0, quote_start) + 1
    previous_newline = text.rfind('\n', 0, quote_start)
    column = quote_start - previous_newline
    token = 'string literal' if quote == "'" else 'double-quoted identifier'
    raise ValueError(f"Unterminated {token} at line {line}, column {column}.")


def _is_already_masked(text: str) -> bool:
    """Recognize standalone masked DDL so it is not masked a second time."""
    declared = re.search(
        r'\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:PROC(?:EDURE)?|FUNCTION)\s+'
        r'(?:"?[A-Za-z_]\w*"?\.)?"?(?:PROC|FUNC)_\d+"?',
        text,
        re.IGNORECASE,
    )
    return bool(
        declared
        and len(re.findall(r'\bTBL_\d+\b', text, re.IGNORECASE)) >= 1
        and len(re.findall(r'\bCOL_\d+\b', text, re.IGNORECASE)) >= 1
    )


def _identity_mapping(text: str) -> dict:
    """Supply token metadata while preserving names in an already-masked file."""
    categories = {
        'tables': 'TBL', 'views': 'VW', 'procedures': 'PROC', 'functions': 'FUNC',
        'triggers': 'TRG', 'indexes': 'IDX', 'sequences': 'SEQ', 'types': 'TYPE',
        'columns': 'COL',
    }
    mapping = {}
    for category, prefix in categories.items():
        tokens = sorted(set(re.findall(rf'\b{prefix}_\d+\b', text, re.IGNORECASE)), key=str.lower)
        if tokens:
            mapping[category] = {token: token for token in tokens}
    return mapping


def _apply_rules_with_trace(masked_text: str, rules: list[dict], mapping, source_dialect: str, target_dialect: str):
    """Apply deterministic rules line by line and retain a complete audit trace."""
    alias_rule = next((rule for rule in rules if rule["rule_code"] == "asa-pg-table-aliases"), None)
    original_lines = masked_text.splitlines()
    if alias_rule is not None:
        masked_text, _ = _apply_table_alias_policy(masked_text, alias_rule["replacement"], mapping)
    output_lines = []
    trace = []
    for line_number, original_line in enumerate(masked_text.splitlines(), start=1):
        current = original_line
        applied = []
        source_line = original_lines[line_number - 1] if line_number <= len(original_lines) else ""
        if alias_rule is not None and source_line != original_line:
            applied.append({
                "rule_id": alias_rule["id"], "rule_code": alias_rule["rule_code"],
                "priority": alias_rule["priority"], "matches": 1,
            })
        for rule in rules:
            if rule["rule_code"] == "asa-pg-table-aliases":
                continue
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
            "source": unmask_text(source_line, mapping, source_dialect),
            "output": unmask_text(current, mapping, target_dialect),
            "rules": applied,
        })
    return "\n".join(output_lines), trace


def _apply_table_alias_policy(sql: str, policy_json: str, mapping) -> tuple[str, int]:
    """Add aliases to FROM/JOIN tables and rewrite matching table.column qualifiers.

    The rule replacement is JSON so each client can supply explicit aliases while
    retaining a deterministic, collision-safe fallback for other tables.
    """
    try:
        policy = json.loads(policy_json or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("The table-alias skill replacement must be valid JSON.") from exc
    alias_length = max(1, int(policy.get("alias_length", 3)))
    overrides = {str(k).lower(): str(v) for k, v in policy.get("aliases", {}).items()}
    reverse_tables = {token.lower(): name for name, token in mapping.get("tables", {}).items()}
    used: set[str] = set()
    aliases: dict[str, str] = {}
    changes = 0
    reserved = r"(?:ON|WHERE|JOIN|LEFT|RIGHT|FULL|INNER|OUTER|CROSS|GROUP|ORDER|HAVING|UNION|LIMIT|OFFSET|FETCH|RETURNING|SET|END|ELSE|ELSIF|EXCEPTION)"
    relation = re.compile(
        rf"(?P<prefix>(?:\b(?:FROM|JOIN)\s+|,\s*))(?P<relation>(?:(?:\"?[A-Za-z_]\w*\"?)\.)?(?P<table>\"?TBL_\d+\"?))(?!\s*\.)"
        rf"(?P<alias_part>\s+(?:AS\s+)?(?P<alias>(?!{reserved}\b)[A-Za-z_]\w*))?",
        re.IGNORECASE,
    )

    def alias_base(table_name: str) -> str:
        words = [word for word in re.split(r'[^A-Za-z]+', table_name.lower()) if word]
        if not words:
            return "tbl"[:alias_length]
        if len(words) >= alias_length:
            return "".join(word[0] for word in words[:alias_length])
        allocation = [1] * len(words)
        remaining = alias_length - len(words)
        word_index = 0
        while remaining:
            if allocation[word_index] < len(words[word_index]):
                allocation[word_index] += 1
                remaining -= 1
            word_index = (word_index + 1) % len(words)
        return "".join(word[:size] for word, size in zip(words, allocation))

    def unique_alias(base: str, table_name: str = "") -> str:
        candidate = re.sub(r"\W+", "_", base).strip("_") or "tbl"
        if candidate[0].isdigit():
            candidate = f"t_{candidate}"
        root = candidate.lower()
        if candidate.lower() in used:
            alternatives = []
            letters = re.sub(r'[^a-z]', '', table_name.lower())
            if len(root) > 1:
                alternatives.extend(root[:-1] + letter for letter in letters)
                alternatives.extend(root[:-1] + letter for letter in "abcdefghijklmnopqrstuvwxyz")
            else:
                alternatives.extend(root + letter for letter in "abcdefghijklmnopqrstuvwxyz")
            for alternative in alternatives:
                if alternative.lower() not in used:
                    candidate = alternative
                    break
            else:
                width = 2
                while candidate.lower() in used:
                    candidate = root + ("a" * width)
                    width += 1
        used.add(candidate.lower())
        return candidate

    def add_alias(match):
        nonlocal changes
        prefix = re.sub(r'\s+$', ' ', match.group('prefix'))
        token = match.group("table").strip('"')
        token_key = token.lower()
        existing = match.group("alias")
        if token_key in aliases:
            if existing:
                return f"{prefix}{match.group('relation')}{match.group('alias_part')}"
            # A table token can occur in several SELECT statements or nested
            # subqueries. Reuse its established alias in every query scope;
            # otherwise later FROM lists contain unaliased relations and cannot
            # be converted to JOIN clauses.
            changes += 1
            return f"{prefix}{match.group('relation')} AS {aliases[token_key]}"
        if existing:
            aliases[token_key] = unique_alias(existing, reverse_tables.get(token_key, token))
            return f"{prefix}{match.group('relation')}{match.group('alias_part')}"
        original = reverse_tables.get(token_key, token)
        configured = overrides.get(original.lower())
        alias = unique_alias(configured or alias_base(original), original)
        aliases[token_key] = alias
        changes += 1
        return f"{prefix}{match.group('relation')} AS {alias}"

    sql = relation.sub(add_alias, sql)
    for token, alias in aliases.items():
        qualifier = re.compile(rf"(?<![\w.])\"?{re.escape(token)}\"?\s*\.", re.IGNORECASE)
        sql, replaced = qualifier.subn(f"{alias}.", sql)
        changes += replaced
    sql, join_changes = _convert_comma_tables_to_joins(sql)
    changes += join_changes
    return sql, changes


def _split_top_level(text: str, delimiter_pattern: str) -> list[str]:
    """Split SQL text on a delimiter while ignoring strings and parentheses."""
    parts = []
    start = 0
    depth = 0
    quote = None
    index = 0
    delimiter = re.compile(delimiter_pattern, re.IGNORECASE)
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
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
            match = delimiter.match(text, index)
            if match:
                parts.append(text[start:index].strip())
                start = match.end()
                index = match.end()
                continue
        index += 1
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _strip_enclosing_parentheses(text: str) -> str:
    """Remove parentheses that wrap an entire SQL condition block."""
    value = text.strip()
    while value.startswith('(') and value.endswith(')'):
        depth = 0
        quote = None
        encloses_all = True
        for index, char in enumerate(value):
            if quote:
                if char == quote:
                    quote = None
                continue
            if char in ("'", '"'):
                quote = char
            elif char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth == 0 and index != len(value) - 1:
                    encloses_all = False
                    break
        if not encloses_all or depth != 0:
            break
        value = value[1:-1].strip()
    return value


def _split_conjuncts(text: str) -> list[str]:
    """Flatten parenthesized AND groups while preserving OR expressions."""
    value = _strip_enclosing_parentheses(text)
    parts = _split_top_level(value, r'\bAND\b')
    if len(parts) == 1:
        # Retain grouping around OR so joining the residual predicates with AND
        # cannot change Boolean precedence.
        if len(_split_top_level(value, r'\bOR\b')) > 1:
            return [text.strip()]
        return parts
    flattened = []
    for part in parts:
        flattened.extend(_split_conjuncts(part))
    return flattened


def _has_outer_alias_reference(predicate: str, alias: str) -> bool:
    """Find an alias reference without counting references in scalar subqueries."""
    value = _strip_enclosing_parentheses(predicate)
    target = re.compile(rf'(?<!\w){re.escape(alias)}\s*\.', re.IGNORECASE)
    visible = []
    quote = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote:
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            visible.append(' ')
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            visible.append(' ')
        elif char == '(' and re.match(r'\s*SELECT\b', value[index + 1:], re.IGNORECASE):
            depth = 1
            index += 1
            nested_quote = None
            while index < len(value) and depth:
                nested_char = value[index]
                if nested_quote:
                    if nested_char == nested_quote:
                        nested_quote = None
                elif nested_char in ("'", '"'):
                    nested_quote = nested_char
                elif nested_char == '(':
                    depth += 1
                elif nested_char == ')':
                    depth -= 1
                index += 1
            visible.append(' ')
            continue
        else:
            visible.append(char)
        index += 1
    return bool(target.search(''.join(visible)))


def _convert_comma_tables_to_joins(sql: str) -> tuple[str, int]:
    """Turn comma FROM lists into JOIN clauses using relationship predicates."""
    def find_boundary(start: int, patterns: list[str], semicolon: bool = False):
        depth = 0
        quote = None
        index = start
        compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
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
            elif char == '(':
                depth += 1
            elif char == ')':
                depth = max(0, depth - 1)
            elif depth == 0:
                if semicolon and char == ';':
                    return index, index + 1
                for pattern in compiled:
                    match = pattern.match(sql, index)
                    if match:
                        return index, match.end()
            index += 1
        return len(sql), len(sql)

    def from_positions():
        positions = []
        quote = None
        index = 0
        keyword = re.compile(r'\bFROM\b', re.IGNORECASE)
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
            match = keyword.match(sql, index)
            if match:
                positions.append((match.start(), match.end()))
                index = match.end()
                continue
            index += 1
        return positions

    def convert_block(from_start, from_end, where_start, where_end, condition_end):
        relations = _split_top_level(sql[from_end:where_start], r',')
        if len(relations) < 2 or any(re.search(r'\bJOIN\b', item, re.IGNORECASE) for item in relations):
            return None
        conditions = sql[where_end:condition_end]
        predicates = _split_conjuncts(conditions)
        alias_pattern = re.compile(r'(?:\bAS\s+|\s+)([A-Za-z_]\w*)\s*$', re.IGNORECASE)
        relation_aliases = []
        for relation_text in relations:
            alias_match = alias_pattern.search(relation_text)
            if not alias_match:
                return None
            relation_aliases.append(alias_match.group(1))

        remaining = list(predicates)
        joined = [relation_aliases[0]]
        join_lines = [relations[0]]
        for relation_text, alias in zip(relations[1:], relation_aliases[1:]):
            predicate_index = next(
                (i for i, predicate in enumerate(remaining)
                 if _has_outer_alias_reference(predicate, alias)
                 and any(_has_outer_alias_reference(predicate, item) for item in joined)),
                None,
            )
            if predicate_index is None:
                join_lines.append(f'CROSS JOIN {relation_text}')
            else:
                predicate = remaining.pop(predicate_index)
                join_lines.append(f'JOIN {relation_text} ON {predicate}')
            joined.append(alias)

        where_sql = ''
        if remaining:
            where_sql = f"\nWHERE\n{'\nAND '.join(remaining)}"
        return f"FROM {chr(10).join(join_lines)}{where_sql}"

    replacements = []
    for from_start, from_end in from_positions():
        where_start, where_end = find_boundary(
            from_end,
            [r'\bWHERE\b', r'\bGROUP\s+BY\b', r'\bORDER\s+BY\b', r'\bUNION\b'],
            semicolon=True,
        )
        if not re.match(r'\bWHERE\b', sql[where_start:where_end], re.IGNORECASE):
            continue
        condition_end, _ = find_boundary(
            where_end,
            [r'\bGROUP\s+BY\b', r'\bORDER\s+BY\b', r'\bHAVING\b', r'\bUNION\b',
             r'\bRETURNING\b', r'(?m)^[ \t]*ELSE\b', r'(?m)^[ \t]*END\s+IF\b'],
            semicolon=True,
        )
        replacement = convert_block(from_start, from_end, where_start, where_end, condition_end)
        if replacement is not None:
            if re.match(r'[ \t]*ELSE\b', sql[condition_end:], re.IGNORECASE):
                replacement = replacement.rstrip() + ';\n'
            elif re.match(r'[ \t]*END\s+IF\b', sql[condition_end:], re.IGNORECASE):
                replacement += '\n'
            elif re.match(r'\s*(?:GROUP\s+BY|ORDER\s+BY|HAVING|UNION|RETURNING)\b', sql[condition_end:], re.IGNORECASE):
                replacement = replacement.rstrip() + '\n'
            replacements.append((from_start, condition_end, replacement))

    # Nested candidates can overlap an outer SELECT. Only apply non-overlapping
    # blocks from right to left so offsets remain stable.
    converted = sql
    applied_start = len(sql) + 1
    count = 0
    for start, end, replacement in sorted(replacements, reverse=True):
        if end > applied_start:
            continue
        converted = converted[:start] + replacement + converted[end:]
        applied_start = start
        count += 1
    return converted, count


def _qualify_schema_references(line: str, schema: str) -> tuple[str, int]:
    """Qualify structural table references and non-built-in routine calls."""
    from postgresql_vocabulary import is_postgresql_builtin

    count = 0
    relation = re.compile(
        r"(?P<prefix>\b(?:FROM|JOIN|UPDATE|INTO|DELETE\s+FROM)\s+)"
        r"(?![\"\w]+\s*\.)(?P<name>\"?[A-Za-z_][A-Za-z0-9_$]*\"?)",
        re.IGNORECASE,
    )
    line, relation_count = relation.subn(lambda m: f"{m.group('prefix')}{schema}.{m.group('name')}", line)
    count += relation_count
    # ASA compatibility functions are rewritten by migration rules and must
    # not be mistaken for project routines before those rules run.
    source_builtins = {'len', 'list', 'string', 'isnull'}
    routine = re.compile(r"(?<![\w.\"'])(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\s*(?=\()")
    def qualify_call(match):
        nonlocal count
        name = match.group("name")
        if is_postgresql_builtin(name) or name.lower() in source_builtins or "_" not in name:
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
    if re.search(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\b", text, re.IGNORECASE):
        return "function", "Source object is explicitly declared as a function.", "source-function"
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


def render_postgresql_routine(masked_text: str, target_type: str, inferred_result_columns: str | None = None):
    """Render a masked ASA procedure or function as a PostgreSQL routine."""
    match = re.search(
        r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?P<kind>PROC(?:EDURE)?|FUNCTION)\s+"
        r"(?P<name>[\w.$\"\[\]]+)", masked_text, re.IGNORECASE,
    )
    if not match:
        raise ValueError("Unable to locate the ASA procedure or function declaration for PostgreSQL rendering.")
    begin = re.search(r"\bBEGIN\b", masked_text[match.end():], re.IGNORECASE)
    if not begin:
        raise ValueError("Unable to locate the ASA routine BEGIN block.")
    begin_at = match.end() + begin.start()
    header_tail = masked_text[match.end():begin_at]
    header_tail = re.sub(r"\bAS\s*$", "", header_tail, flags=re.IGNORECASE).strip()
    header_tail, result_columns = _extract_result_clause(header_tail)
    header_tail, scalar_return_type = _extract_scalar_returns_clause(header_tail)
    params = header_tail if header_tail.startswith("(") else f"({header_tail.strip().strip(',')})"
    body = masked_text[begin_at:].strip()
    body = re.sub(r";?\s*$", "", body)
    name = match.group("name")
    trace = []
    if target_type == "function":
        raw_table_columns = result_columns or inferred_result_columns
        table_columns = _normalize_returns_table_types(raw_table_columns)
        normalized_scalar_type = _normalize_scalar_return_type(scalar_return_type)
        if table_columns:
            returns = f"RETURNS TABLE (\n    {table_columns}\n)"
        elif normalized_scalar_type:
            returns = f"RETURNS {normalized_scalar_type}"
        else:
            returns = "RETURNS SETOF RECORD"
        simple_select = None if normalized_scalar_type else _simple_select_body(body)
        if simple_select is not None:
            rendered = f"CREATE OR REPLACE FUNCTION {name}{params}\n{returns}\nLANGUAGE sql\nAS $$\n{simple_select.rstrip(';')};\n$$;"
            renderer = "postgresql-sql-function-renderer"
            routine_language = "sql"
        else:
            if not normalized_scalar_type:
                body = _convert_top_level_result_selects(body)
            declarations, body = _normalize_plpgsql_body(body)
            body = _terminate_return_queries(body)
            declare_block = f"DECLARE\n{declarations}\n" if declarations else ""
            rendered = f"CREATE OR REPLACE FUNCTION {name}{params}\n{returns}\nLANGUAGE plpgsql\nAS $$\n{declare_block}{body}\n$$;"
            renderer = "postgresql-plpgsql-function-renderer"
            routine_language = "plpgsql"
        if table_columns:
            cast_targets = _return_cast_targets(table_columns, inferred_result_columns)
            if inferred_result_columns is None:
                # Without metadata, character normalization is still known and
                # PostgreSQL requires VARCHAR/CHAR expressions to match TEXT.
                cast_targets.update({
                    position: "TEXT"
                    for position, column in enumerate(_split_sql_expressions(table_columns))
                    if _canonical_pg_type(_column_declaration_type(column)) == "text"
                })
            rendered = _cast_returns_table_outputs(rendered, cast_targets)
    else:
        declarations, body = _normalize_plpgsql_body(body)
        declare_block = f"DECLARE\n{declarations}\n" if declarations else ""
        rendered = f"CREATE OR REPLACE PROCEDURE {name}{params}\nLANGUAGE plpgsql\nAS $$\n{declare_block}{body}\n$$;"
        renderer = "postgresql-procedure-renderer"
        routine_language = "plpgsql"
    trace.append({"line": "renderer", "source": match.group(0), "output": rendered.splitlines()[0],
                  "rules": [{"rule_id": renderer, "rule_code": renderer, "priority": 2000, "matches": 1}]})
    return rendered, trace, routine_language


def _normalize_returns_table_types(columns: str | None) -> str | None:
    """Apply target character-type policy to a RETURNS TABLE contract."""
    if not columns:
        return columns
    return re.sub(
        r'\b(?:CHARACTER\s+VARYING|VARCHAR|CHARACTER|CHAR)\s*\(\s*(?:\*|\d+)\s*\)',
        'TEXT',
        columns,
        flags=re.IGNORECASE,
    )


def _character_return_positions(columns: str | None) -> set[int]:
    """Return output positions whose declared character type was normalized to TEXT."""
    if not columns:
        return set()
    positions = set()
    for index, column in enumerate(_split_sql_expressions(columns)):
        if re.search(
            r'\b(?:CHARACTER\s+VARYING|VARCHAR|CHARACTER|CHAR)\s*\(\s*(?:\*|\d+)\s*\)',
            column, re.IGNORECASE,
        ):
            positions.add(index)
    return positions


def _cast_returns_table_text_outputs(sql: str, positions: set[int] | None = None) -> str:
    """Cast result expressions to TEXT where the RETURNS TABLE contract requires it."""
    contract = re.search(r'\bRETURNS\s+TABLE\s*\((.*?)\)\s*LANGUAGE\b', sql, re.IGNORECASE | re.DOTALL)
    if not contract:
        return sql
    columns = _split_sql_expressions(contract.group(1))
    if positions is None:
        positions = {index for index, column in enumerate(columns) if re.search(r'\bTEXT\b', column, re.IGNORECASE)}
    return _cast_returns_table_outputs(sql, {position: "TEXT" for position in positions})


def _return_cast_targets(expected_columns: str | None, actual_columns: str | None) -> dict[int, str]:
    """Map output positions to declared target types when metadata types differ."""
    if not expected_columns or not actual_columns:
        return {}
    expected = [_column_declaration_type(item) for item in _split_sql_expressions(expected_columns)]
    actual = [_column_declaration_type(item) for item in _split_sql_expressions(actual_columns)]
    if len(expected) != len(actual):
        return {}
    return {
        index: expected_type
        for index, (expected_type, actual_type) in enumerate(zip(expected, actual))
        if _canonical_pg_type(expected_type) != _canonical_pg_type(actual_type)
    }


def _column_declaration_type(column: str) -> str:
    match = re.match(r'\s*(?:"[^"]+"|[A-Za-z_]\w*)\s+(.+?)\s*$', column, re.DOTALL)
    return match.group(1).strip() if match else ""


def _canonical_pg_type(value: str) -> str:
    normalized = re.sub(r'\s+', ' ', value.strip().lower())
    normalized = re.sub(r'\s*,\s*', ',', normalized)
    normalized = re.sub(r'\(\s*', '(', normalized)
    normalized = re.sub(r'\s*\)', ')', normalized)
    aliases = {
        "int": "integer", "int4": "integer",
        "int2": "smallint", "int8": "bigint",
        "decimal": "numeric", "float8": "double precision",
        "bool": "boolean", "varchar": "character varying",
    }
    base = re.match(r'[a-z ]+', normalized)
    if base:
        original = base.group(0).strip()
        replacement = aliases.get(original, original)
        normalized = replacement + normalized[base.end():]
    return normalized


def _cast_returns_table_outputs(sql: str, cast_targets: dict[int, str]) -> str:
    """Apply positional casts to result-producing SELECT expressions."""
    if not cast_targets:
        return sql

    from result_metadata import _outer_select_spans
    contract = re.search(r'\bRETURNS\s+TABLE\s*\((.*?)\)\s*LANGUAGE\b', sql, re.IGNORECASE | re.DOTALL)
    if not contract:
        return sql
    columns = _split_sql_expressions(contract.group(1))
    replacements = []
    language_sql = bool(re.search(r'\bLANGUAGE\s+sql\b', sql, re.IGNORECASE))
    for start, end in _outer_select_spans(sql):
        prefix = sql[max(0, start - 100):start]
        if not re.search(r'RETURN\s+QUERY\s+SELECT\s*$', prefix, re.IGNORECASE) and not language_sql:
            continue
        expressions = _split_sql_expressions(sql[start:end])
        if len(expressions) != len(columns):
            continue
        changed = False
        for position, target_type in cast_targets.items():
            if position < len(expressions):
                casted = _cast_expression_to_type(expressions[position], target_type)
                if casted != expressions[position]:
                    expressions[position] = casted
                    changed = True
        if changed:
            replacements.append((start, end, "\n    " + ",\n    ".join(expressions) + "\n"))
    for start, end, replacement in reversed(replacements):
        sql = sql[:start] + replacement + sql[end:]
    return sql


def _cast_expression_to_type(expression: str, target_type: str) -> str:
    alias = re.search(r'\s+AS\s+("?[A-Za-z_]\w*"?)\s*$', expression, re.IGNORECASE)
    value = expression[:alias.start()].strip() if alias else expression.strip()
    suffix = f" AS {alias.group(1)}" if alias else ""
    target_pattern = re.escape(target_type).replace(r'\ ', r'\s+')
    if re.search(rf'::\s*{target_pattern}\s*$', value, re.IGNORECASE) or re.match(
        rf'^CAST\s*\(.*\s+AS\s+{target_pattern}\s*\)$', value, re.IGNORECASE | re.DOTALL
    ):
        return value + suffix
    if re.fullmatch(r'NULL', value, re.IGNORECASE):
        return f"NULL::{target_type}" + suffix
    identifier = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
    if re.fullmatch(rf'{identifier}(?:\s*\.\s*{identifier})*', value):
        return f"{value}::{target_type}{suffix}"
    return f"({value})::{target_type}{suffix}"


def _split_sql_expressions(text: str) -> list[str]:
    parts, start, depth, quote = [], 0, 0, None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth = max(0, depth - 1)
        elif char == ',' and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
        index += 1
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _normalize_plpgsql_body(body: str) -> tuple[str, str]:
    """Hoist ASA declarations and convert SET assignments to PL/pgSQL syntax."""
    body = _convert_asa_if_expressions(body)
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
    normalized_body = "\n".join(output)
    # The renderer owns the dollar-quoted routine wrapper, so its final PL/pgSQL
    # END must carry the semicolon inside that wrapper.
    normalized_body = re.sub(r'(?im)^(?P<indent>\s*)END\s*$', r'\g<indent>END;', normalized_body)
    normalized_body = re.sub(r'\bEND\s*$', 'END;', normalized_body, flags=re.IGNORECASE)
    return "\n".join(declarations), normalized_body


def _terminate_return_queries(body: str) -> str:
    """Terminate RETURN QUERY statements before the next PL/pgSQL boundary."""
    lines = body.splitlines()
    output = []
    in_return_query = False
    sql_case_depth = 0
    for line in lines:
        stripped = line.strip()
        boundary = bool(
            in_return_query
            and sql_case_depth == 0
            and re.match(r'^(?:ELSE|ELSIF\b.*|END\s+IF\b|EXCEPTION\b|END(?:\s+(?:LOOP|WHILE|FOR))?)', stripped, re.I)
        )
        if boundary:
            for index in range(len(output) - 1, -1, -1):
                if output[index].strip():
                    if not output[index].rstrip().endswith(';'):
                        output[index] = output[index].rstrip() + ';'
                    break
            in_return_query = False

        output.append(line)
        if re.match(r'^RETURN\s+QUERY\b', stripped, re.I):
            in_return_query = True
        if in_return_query:
            sql_case_depth += len(re.findall(r'\bCASE\b', stripped, re.I))
            sql_case_depth -= len(re.findall(r'\bEND\b(?!\s+IF\b)', stripped, re.I))
            sql_case_depth = max(0, sql_case_depth)
            if stripped.endswith(';'):
                in_return_query = False
                sql_case_depth = 0
    return "\n".join(output)


def _convert_asa_if_expressions(body: str) -> str:
    """Convert ASA's IF expression form to a PostgreSQL CASE expression."""
    expression = re.compile(
        r'\bIF\s+(?P<condition>(?:(?!\bIF\b).)*?)\s+THEN\s+'
        r'(?P<when_true>(?:(?!\bIF\b).)*?)\s+ELSE\s+'
        r'(?P<when_false>.*?)\s+ENDIF\b',
        re.IGNORECASE | re.DOTALL,
    )

    def convert(match):
        condition = match.group('condition').strip()
        when_true = match.group('when_true').strip()
        when_false = match.group('when_false').strip()
        return f"CASE WHEN {condition} THEN {when_true} ELSE {when_false} END"

    return expression.sub(convert, body)


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


def _extract_scalar_returns_clause(header: str) -> tuple[str, str | None]:
    """Remove a scalar ASA RETURNS clause from a routine header."""
    returns = re.search(r'\bRETURNS\b', header, re.IGNORECASE)
    if returns is None:
        return header, None
    declared = header[returns.end():].strip()
    if not declared or re.match(r'(?:TABLE|SETOF)\b', declared, re.IGNORECASE):
        return header, None
    return header[:returns.start()].strip(), declared


def _source_scalar_return_type(text: str) -> str | None:
    """Read an explicitly declared scalar return type from an ASA function."""
    declaration = re.search(
        r'\bCREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+[\w.$"\[\]]+', text, re.IGNORECASE
    )
    if declaration is None:
        return None
    begin = re.search(r'\bBEGIN\b', text[declaration.end():], re.IGNORECASE)
    if begin is None:
        return None
    header = text[declaration.end():declaration.end() + begin.start()]
    header, _result_columns = _extract_result_clause(header)
    _remaining, scalar_type = _extract_scalar_returns_clause(header)
    return scalar_type


def _normalize_scalar_return_type(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r'\bLONG\s+VARCHAR\b', 'TEXT', value.strip(), flags=re.IGNORECASE)
    normalized = _normalize_returns_table_types(normalized)
    normalized = re.sub(r'\bDATETIME\b', 'TIMESTAMP', normalized, flags=re.IGNORECASE)
    return normalized
