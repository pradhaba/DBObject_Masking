#!/usr/bin/env python3
"""DDL Masking Tool

This script masks database object names and column names in SQL DDL text, producing a masked DDL plus a mapping.
It can also reverse the masking using the original mapping.
"""

import argparse
import json
import os
import re
import sys

try:
    import sqlparse
    SQLPARSE_AVAILABLE = True
except ImportError:
    SQLPARSE_AVAILABLE = False

MAP_COMMENT_START = "-- DDL_MASKER_MAPPING_START"
MAP_COMMENT_END = "-- DDL_MASKER_MAPPING_END"

OBJECT_TYPE_PREFIX = {
    "table": "TBL",
    "view": "VW",
    "procedure": "PROC",
    "function": "FUNC",
    "trigger": "TRG",
    "index": "IDX",
    "sequence": "SEQ",
    "type": "TYPE",
    "column": "COL",
    "parameter": "PARAM",
}

SUPPORTED_DIALECTS = ('generic', 'sybase_asa', 'postgresql')

IDENTIFIER = r'(?:"(?:[^"]|"")+"|\[(?:[^\]]|\]\])+\]|[\w#$@]+)'
QUALIFIED_IDENTIFIER = rf'{IDENTIFIER}(?:\s*\.\s*{IDENTIFIER})*'

# DDL seen in real schema dumps includes ALTER statements, SQL Server/Sybase's
# PROC abbreviation, and database.schema.object names.  The former expression
# only understood CREATE and at most one qualifier.
OBJECT_DEFINITION_PATTERN = re.compile(
    rf"\b(?:CREATE\s+(?:OR\s+(?:REPLACE|ALTER)\s+)?|ALTER\s+)"
    rf"(?P<type>TABLE|VIEW|INDEX|FUNCTION|PROCEDURE|PROC|TRIGGER|SEQUENCE|TYPE)\s+"
    rf"(?:IF\s+NOT\s+EXISTS\s+)?(?P<qualified_name>{QUALIFIED_IDENTIFIER})",
    re.IGNORECASE,
)

IDENTIFIER_QUOTED_PATTERN = re.compile(r'^(?:"(?P<dq>.+)"|\[(?P<br>.+)\]|(?P<raw>[\w]+))$')

COLUMN_NAME_PATTERN = re.compile(
    r'^(?P<name>"[^"]+"|\[[^\]]+\]|[\w]+)\s+',
    re.IGNORECASE,
)


def normalize_name(name):
    m = IDENTIFIER_QUOTED_PATTERN.match(name.strip())
    if not m:
        return name.strip()
    return m.group("dq") or m.group("br") or m.group("raw")


def final_identifier(qualified_name):
    """Return the object portion of a possibly qualified SQL name."""
    parts = re.findall(IDENTIFIER, qualified_name)
    return normalize_name(parts[-1]) if parts else normalize_name(qualified_name)


def quote_name(name, quote_char='"'):
    if re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
        return name
    if quote_char == '"':
        return f'"{name}"'
    return f'[{name}]'


def find_balanced_parentheses(text, start_index):
    depth = 0
    for i in range(start_index, len(text)):
        ch = text[i]
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return i
    return -1


def extract_table_column_names(text):
    column_names = []
    for match in re.finditer(
        rf"\bCREATE\s+(?:OR\s+(?:REPLACE|ALTER)\s+)?"
        rf"(?:(?:GLOBAL|LOCAL)\s+TEMPORARY\s+|TEMP(?:ORARY)?\s+)?TABLE\s+"
        rf"(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>{QUALIFIED_IDENTIFIER})",
        text,
        re.IGNORECASE,
    ):
        body_start = text.find('(', match.end())
        if body_start == -1:
            continue
        body_end = find_balanced_parentheses(text, body_start)
        if body_end == -1:
            continue
        body = text[body_start + 1:body_end]
        parts = []
        current = []
        depth = 0
        for ch in body:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            if ch == ',' and depth == 0:
                parts.append(''.join(current))
                current = []
            else:
                current.append(ch)
        if current:
            parts.append(''.join(current))
        for part in parts:
            line = part.strip()
            if not line:
                continue
            if line.upper().startswith(('CONSTRAINT ', 'PRIMARY ', 'UNIQUE ', 'FOREIGN ', 'CHECK ', 'REFERENCES ')):
                continue
            col_match = COLUMN_NAME_PATTERN.match(line)
            if col_match:
                raw_name = normalize_name(col_match.group('name'))
                if raw_name:
                    column_names.append(raw_name)
    return column_names


def extract_query_names(text):
    """Find relation names and qualified columns used inside SQL statements."""
    tables = set()
    columns = set()
    qualifiers = set()

    # Covers the common DML relation positions without mistaking the schema in
    # a routine declaration for a table name.
    relation_pattern = re.compile(
        rf'\b(?:FROM|JOIN|UPDATE|INTO)\s+(?P<name>{QUALIFIED_IDENTIFIER})',
        re.IGNORECASE,
    )
    for match in relation_pattern.finditer(text):
        table_name = final_identifier(match.group('name'))
        if table_name:
            tables.add(table_name)
            qualifiers.add(table_name.lower())

        # Record a simple table alias, if present. SQL clause words are not
        # aliases and must not make arbitrary qualified expressions eligible.
        tail = text[match.end():]
        alias_match = re.match(
            rf'\s+(?:AS\s+)?(?P<alias>{IDENTIFIER})', tail, re.IGNORECASE
        )
        if alias_match:
            alias = final_identifier(alias_match.group('alias'))
            if alias.upper() not in {
                'WHERE', 'JOIN', 'LEFT', 'RIGHT', 'FULL', 'INNER', 'OUTER',
                'CROSS', 'ON', 'GROUP', 'ORDER', 'HAVING', 'UNION', 'END',
            }:
                qualifiers.add(alias.lower())

    qualified_column_pattern = re.compile(
        rf'(?P<qualifier>{IDENTIFIER})\s*\.\s*(?P<column>{IDENTIFIER})',
        re.IGNORECASE,
    )
    for match in qualified_column_pattern.finditer(text):
        qualifier = normalize_name(match.group('qualifier'))
        if qualifier.lower() in qualifiers:
            columns.add(normalize_name(match.group('column')))

    # Assignment targets in UPDATE statements are columns even when they are
    # not qualified with a table name.
    set_clause_pattern = re.compile(
        r'\bSET\s+(?P<body>.*?)(?=\bWHERE\b|\bFROM\b|\bRETURNING\b|\bEND\b|;|$)',
        re.IGNORECASE | re.DOTALL,
    )
    assignment_pattern = re.compile(
        rf'(?:^|,)\s*(?P<column>{QUALIFIED_IDENTIFIER})\s*=',
        re.IGNORECASE,
    )
    for set_clause in set_clause_pattern.finditer(text):
        for assignment in assignment_pattern.finditer(set_clause.group('body')):
            column = final_identifier(assignment.group('column'))
            if column and not column.startswith(('@', ':')):
                columns.add(column)

    # Capture the left-hand column in common predicates such as
    # ``WHERE mail_merge_id = @mail_merge_id`` and JOIN ``ON`` expressions.
    predicate_column_pattern = re.compile(
        rf'(?P<column>{QUALIFIED_IDENTIFIER})\s*'
        rf'(?:=|<>|!=|<=|>=|<|>|\bLIKE\b|\bIN\b|\bIS\b|\bBETWEEN\b)',
        re.IGNORECASE,
    )
    for clause in re.finditer(
        r'\b(?:WHERE|ON|HAVING)\b(?P<body>.*?)(?=\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|\bRETURNING\b|\bEND\b|;|$)',
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        for predicate in predicate_column_pattern.finditer(clause.group('body')):
            column = final_identifier(predicate.group('column'))
            if column and not column.startswith(('@', ':')):
                columns.add(column)

    return tables, columns


def split_sql_list(text):
    """Split a comma-separated SQL list while respecting nested type brackets."""
    parts = []
    current = []
    depth = 0
    for ch in text:
        if ch == '(':
            depth += 1
        elif ch == ')' and depth:
            depth -= 1
        if ch == ',' and depth == 0:
            parts.append(''.join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current))
    return parts


def extract_parameter_names(text):
    """Extract named routine parameters and locally declared variables."""
    parameters = set()
    for match in OBJECT_DEFINITION_PATTERN.finditer(text):
        if match.group('type').lower() not in ('procedure', 'proc', 'function'):
            continue
        open_paren = text.find('(', match.end())
        if open_paren == -1:
            continue
        # Do not consume a parenthesis belonging to the routine body.
        between = text[match.end():open_paren]
        if re.search(r'\b(?:AS|BEGIN)\b', between, re.IGNORECASE):
            continue
        close_paren = find_balanced_parentheses(text, open_paren)
        if close_paren == -1:
            continue
        for declaration in split_sql_list(text[open_paren + 1:close_paren]):
            param_match = re.match(
                rf'\s*(?:(?:INOUT|IN|OUT)\s+)?(?P<name>{IDENTIFIER})\s+(?P<type>{IDENTIFIER})',
                declaration,
                re.IGNORECASE,
            )
            if param_match:
                parameters.add(normalize_name(param_match.group('name')))

    for match in re.finditer(
        rf'\bDECLARE\s+(?P<name>{IDENTIFIER})\s+(?P<type>{IDENTIFIER})',
        text,
        re.IGNORECASE,
    ):
        parameters.add(normalize_name(match.group('name')))
    return parameters


def extract_object_map(text, dialect='generic'):
    object_map = {
        "tables": set(),
        "views": set(),
        "procedures": set(),
        "functions": set(),
        "triggers": set(),
        "indexes": set(),
        "sequences": set(),
        "types": set(),
        "columns": set(),
        "parameters": set(),
    }
    if dialect == 'sybase_asa':
        text = text.replace('CREATE PROCEDURE', 'CREATE PROCEDURE')
        text = text.replace('CREATE FUNCTION', 'CREATE FUNCTION')
    elif dialect == 'postgresql':
        text = text.replace('CREATE OR REPLACE FUNCTION', 'CREATE OR REPLACE FUNCTION')
        text = text.replace('CREATE OR REPLACE PROCEDURE', 'CREATE OR REPLACE PROCEDURE')
    for match in OBJECT_DEFINITION_PATTERN.finditer(text):
        object_type = match.group('type').lower()
        if object_type == 'proc':
            object_type = 'procedure'
        name = final_identifier(match.group('qualified_name'))
        if not name:
            continue
        if object_type == 'table':
            object_map['tables'].add(name)
        elif object_type == 'view':
            object_map['views'].add(name)
        elif object_type == 'procedure':
            object_map['procedures'].add(name)
        elif object_type == 'function':
            object_map['functions'].add(name)
        elif object_type == 'trigger':
            object_map['triggers'].add(name)
        elif object_type == 'index':
            object_map['indexes'].add(name)
        elif object_type == 'sequence':
            object_map['sequences'].add(name)
        elif object_type == 'type':
            object_map['types'].add(name)
    column_names = extract_table_column_names(text)
    for c in column_names:
        object_map['columns'].add(c)
    query_tables, query_columns = extract_query_names(text)
    object_map['tables'].update(query_tables)
    object_map['columns'].update(query_columns)
    object_map['parameters'].update(extract_parameter_names(text))
    return object_map


def build_mapping(object_map):
    mapping = {}
    counter = 1
    for object_type in ('tables', 'views', 'procedures', 'functions', 'triggers', 'indexes', 'sequences', 'types', 'columns', 'parameters'):
        mapping[object_type] = {}
        prefix = OBJECT_TYPE_PREFIX.get(object_type[:-1] if object_type.endswith('s') else object_type, 'OBJ')
        for original_name in sorted(object_map[object_type], key=lambda o: o.lower()):
            sigil = original_name[0] if original_name.startswith(('@', ':')) else ''
            mapping[object_type][original_name] = f"{sigil}{prefix}_{counter}"
            counter += 1
    return mapping


def suggest_mapping_filename(text):
    """Build a mapping filename from the first procedure or table declaration."""
    for match in OBJECT_DEFINITION_PATTERN.finditer(text):
        object_type = match.group('type').lower()
        if object_type not in ('table', 'procedure', 'proc'):
            continue
        name = final_identifier(match.group('qualified_name'))
        # Keep the original identifier recognizable while producing a filename
        # that is valid on Windows and other common platforms.
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')
        if safe_name:
            return f'{safe_name}_mapping.json'
    return 'ddl_mapping.json'


def mapping_to_text(mapping):
    return json.dumps(mapping, indent=2, sort_keys=True)


def embed_mapping_comment(text, mapping):
    mapping_text = mapping_to_text(mapping)
    lines = [MAP_COMMENT_START] + [f"-- {line}" for line in mapping_text.splitlines()] + [MAP_COMMENT_END]
    return text.rstrip() + '\n\n' + '\n'.join(lines) + '\n'


def extract_mapping_from_text(text):
    start = text.find(MAP_COMMENT_START)
    end = text.find(MAP_COMMENT_END)
    if start == -1 or end == -1 or end < start:
        return None
    block = text[start + len(MAP_COMMENT_START):end].strip()
    json_lines = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith('--'):
            json_lines.append(stripped[2:].strip())
        else:
            json_lines.append(stripped)
    try:
        return json.loads('\n'.join(json_lines))
    except json.JSONDecodeError:
        return None


def replace_identifiers(text, replacements):
    sorted_pairs = sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True)
    result = text
    for original, replacement in sorted_pairs:
        patterns = [
            rf'"{re.escape(original)}"',
            rf'\[{re.escape(original)}\]',
            rf'(?<![\w#$@]){re.escape(original)}(?![\w#$@])',
        ]
        for pattern in patterns:
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return result


def mask_text(text, dialect='generic', embed_mapping=True):
    object_map = extract_object_map(text, dialect)
    mapping = build_mapping(object_map)
    replacements = {}
    for object_type, name_map in mapping.items():
        for original, masked in name_map.items():
            replacements[original] = masked
    masked_text = replace_identifiers(text, replacements)
    if embed_mapping:
        masked_text = embed_mapping_comment(masked_text, mapping)
    return masked_text, mapping


def split_text_and_mapping_block(text):
    start = text.find(MAP_COMMENT_START)
    if start == -1:
        return text, ''
    end = text.find(MAP_COMMENT_END, start)
    if end == -1:
        return text, ''
    end += len(MAP_COMMENT_END)
    before = text[:start]
    suffix = text[start:end]
    if end < len(text):
        suffix += text[end:]
    return before, suffix


def parameter_name_for_dialect(original, dialect, placeholder_has_sigil):
    """Restore a parameter name using conventions of the target dialect."""
    bare_name = original.lstrip('@:')
    if dialect == 'postgresql':
        return bare_name if bare_name.lower().startswith('p_') else f'p_{bare_name}'
    if dialect == 'sybase_asa':
        return original if original.startswith('@') else f'@{bare_name}'
    # Generic mode follows the placeholder form, allowing translated SQL that
    # removed a source-dialect sigil to remain valid.
    return original if placeholder_has_sigil else bare_name


def unmask_text(text, mapping, dialect='generic'):
    if mapping is None:
        raise ValueError('No mapping provided for unmasking.')
    reverse_replacements = {}
    for object_type, name_map in mapping.items():
        for original, masked in name_map.items():
            if object_type == 'parameters':
                bare_masked = masked.lstrip('@:')
                reverse_replacements[masked] = parameter_name_for_dialect(
                    original, dialect, masked.startswith(('@', ':'))
                )
                # Translators commonly remove @ when converting Sybase/SQL
                # Server routines to PostgreSQL. Accept that token as well.
                reverse_replacements[bare_masked] = parameter_name_for_dialect(
                    original, dialect, False
                )
            else:
                reverse_replacements[masked] = original
    body, suffix = split_text_and_mapping_block(text)
    unmasked_body = replace_identifiers(body, reverse_replacements)
    return unmasked_body + suffix


def load_text(path):
    if path:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    return sys.stdin.read()


def write_text(path, text):
    if path:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
    else:
        sys.stdout.write(text)


def load_mapping(path):
    if path and os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


def parse_args():
    parser = argparse.ArgumentParser(description='Mask or unmask DDL object names in SQL text.')
    subparsers = parser.add_subparsers(dest='command', required=True)

    mask_parser = subparsers.add_parser('mask', help='Mask object names in SQL DDL.')
    mask_parser.add_argument('--input', '-i', help='Source SQL file path. If omitted, stdin is used.')
    mask_parser.add_argument('--output', '-o', help='Output file path. If omitted, stdout is used.')
    mask_parser.add_argument('--mapping', '-m', help='Write mapping JSON to this file.')
    mask_parser.add_argument('--embed-mapping', action='store_true', help='Embed the mapping as SQL comments in the output.')
    mask_parser.add_argument('--dialect', choices=SUPPORTED_DIALECTS, default='generic', help='Source dialect for SQL parsing.')

    unmask_parser = subparsers.add_parser('unmask', help='Replace masked names with original names.')
    unmask_parser.add_argument('--input', '-i', help='Source SQL file path. If omitted, stdin is used.')
    unmask_parser.add_argument('--output', '-o', help='Output file path. If omitted, stdout is used.')
    unmask_parser.add_argument('--mapping', '-m', help='Load mapping JSON from this file. If omitted, the tool will attempt to read an embedded mapping comment in the input.')
    unmask_parser.add_argument('--dialect', choices=SUPPORTED_DIALECTS, default='generic', help='Target dialect for SQL parsing.')

    return parser.parse_args()


def main():
    args = parse_args()
    if args.command == 'mask':
        sql_text = load_text(args.input)
        masked_text, mapping = mask_text(sql_text, args.dialect, embed_mapping=args.embed_mapping)
        if args.mapping:
            with open(args.mapping, 'w', encoding='utf-8') as f:
                json.dump(mapping, f, indent=2, sort_keys=True)
        write_text(args.output, masked_text)
        return 0

    if args.command == 'unmask':
        sql_text = load_text(args.input)
        mapping = None
        if args.mapping:
            mapping = load_mapping(args.mapping)
        if mapping is None:
            mapping = extract_mapping_from_text(sql_text)
        if mapping is None:
            print('Error: mapping file not found and no embedded mapping present.', file=sys.stderr)
            return 2
        unmasked = unmask_text(sql_text, mapping, args.dialect)
        write_text(args.output, unmasked)
        return 0

    return 1


if __name__ == '__main__':
    sys.exit(main())
