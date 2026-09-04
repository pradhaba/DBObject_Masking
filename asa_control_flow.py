"""Structural SAP SQL Anywhere control-flow conversion for PL/pgSQL."""

from __future__ import annotations

import re
from collections.abc import Callable


IDENT = r'[A-Za-z_][A-Za-z0-9_$]*'


def convert_asa_control_flow(sql: str, cursor_converter: Callable[[str], str]) -> str:
    """Convert supported ASA loops while preserving nested block structure."""
    converted = cursor_converter(sql)

    # SQL/PSM WHILE ... DO / END WHILE is equivalent to PL/pgSQL's LOOP form.
    converted = re.sub(r'\bWHILE\s+(?P<condition>.*?)\s+DO\b',
                       lambda m: f"WHILE {m.group('condition').strip()} LOOP",
                       converted, flags=re.I | re.S)
    converted = re.sub(r'\bEND\s+WHILE\s*;?', 'END LOOP;', converted, flags=re.I)

    # ASA labels precede a loop with `name:`; PL/pgSQL uses `<<name>>`.
    converted = re.sub(
        rf'(?im)^(?P<indent>\s*)(?P<label>{IDENT})\s*:\s*'
        r'(?P<header>(?:WHILE\s+.*?\s+)?LOOP)\b',
        lambda m: f"{m.group('indent')}<<{m.group('label')}>>\n{m.group('indent')}{m.group('header')}",
        converted,
    )
    # The opening PL/pgSQL label is sufficient; retain no ASA closing label.
    converted = re.sub(rf'\bEND\s+LOOP\s+{IDENT}\s*;?', 'END LOOP;', converted, flags=re.I)
    converted = re.sub(rf'\bLEAVE(?:\s+(?P<label>{IDENT}))?\s*;',
                       lambda m: f"EXIT{(' ' + m.group('label')) if m.group('label') else ''};",
                       converted, flags=re.I)
    # SQL Anywhere 17 permits an omitted semicolon before a block boundary.
    converted = re.sub(rf'\bLEAVE\s+(?P<label>{IDENT})(?=\s+(?:END|ELSE|ELSIF)\b)',
                       lambda m: f"EXIT {m.group('label')};", converted, flags=re.I)
    converted = re.sub(r'(?im)^([ \t]*)BREAK\s*;?\s*$', r'\1EXIT;', converted)
    return converted


def control_flow_diagnostics(source: str, converted: str) -> list[dict]:
    """Return unsupported or structurally unresolved loop constructs."""
    checks = [
        ('ASA_DYNAMIC_CURSOR_USING', r'\bCURSOR\s+USING\b',
         'Dynamic ASA cursor output columns cannot be bound safely without parsing the runtime SQL.'),
        ('ASA_CURSOR_CALL_UNSUPPORTED', r'\bCURSOR\s+FOR\s+CALL\b',
         'Replace the procedure-result cursor with a migrated set-returning function or an explicit result contract.'),
        ('ASA_CURSOR_WITH_HOLD_UNSUPPORTED', r'\bOPEN\s+' + IDENT + r'\s+WITH\s+HOLD\b',
         'Review transaction boundaries before replacing the ASA holdable cursor.'),
        ('ASA_NUMERIC_FOR_UNSUPPORTED', rf'\bFOR\s+{IDENT}\s*=.*?\bTO\b',
         'Review this non-cursor FOR syntax and convert it to PostgreSQL FOR variable IN lower..upper LOOP.'),
        ('ASA_TSQL_WHILE_UNSUPPORTED', r'\bWHILE\b[^;]*?\bBEGIN\b',
         'Convert the Transact-SQL WHILE BEGIN/END block to WHILE condition LOOP/END LOOP.'),
        ('ASA_LEAVE_UNRESOLVED', r'\bLEAVE\b',
         'The LEAVE target could not be converted to a PostgreSQL EXIT statement.'),
        ('ASA_FOR_LOOP_UNRESOLVED', r'\bEND\s+FOR\b|\bFOR\b.*?\bCURSOR\b',
         'The ASA cursor FOR loop could not be converted structurally.'),
    ]
    diagnostics = []
    for code, pattern, suggestion in checks:
        match = re.search(pattern, converted if 'UNRESOLVED' in code else source, re.I | re.S)
        if match:
            diagnostics.append(_diagnostic(code, source, match.group(0), suggestion))

    labels = {item.lower() for item in re.findall(rf'<<\s*({IDENT})\s*>>', converted, re.I)}
    for match in re.finditer(rf'\b(?:EXIT|CONTINUE)\s+(?P<label>{IDENT})\b', converted, re.I):
        label = match.group('label')
        if label.lower() not in labels:
            diagnostics.append(_diagnostic(
                'LOOP_LABEL_NOT_FOUND', source, label,
                f'Add or correct the loop label referenced by {match.group(0)}.',
            ))
    return diagnostics


def _diagnostic(code: str, source: str, expression: str, suggestion: str) -> dict:
    position = source.lower().find(expression.lower())
    return {
        'code': code, 'severity': 'error', 'category': 'control_flow',
        'message': f'Unsupported or unresolved ASA loop construct: {expression.strip()}.',
        'expression': expression.strip(),
        'line': source.count('\n', 0, position) + 1 if position >= 0 else None,
        'column': position - source.rfind('\n', 0, position) if position >= 0 else None,
        'suggestion': suggestion, 'migration_continued': True, 'resolved': False,
    }
