"""Deterministic structural ASA expression and statement rewrites."""
from __future__ import annotations
import re

DATE_UNITS = {
    'yy': 'year', 'yyyy': 'year', 'year': 'year',
    'qq': 'quarter', 'q': 'quarter', 'quarter': 'quarter',
    'mm': 'month', 'm': 'month', 'month': 'month',
    'wk': 'week', 'ww': 'week', 'week': 'week',
    'dd': 'day', 'd': 'day', 'day': 'day',
    'dy': 'day', 'y': 'day', 'dayofyear': 'day',
    'dw': 'day', 'w': 'day', 'weekday': 'day',
    'hh': 'hour', 'hour': 'hour',
    'mi': 'minute', 'n': 'minute', 'minute': 'minute',
    'ss': 'second', 's': 'second', 'second': 'second',
    'ms': 'millisecond', 'millisecond': 'millisecond',
    'mcs': 'microsecond', 'microsecond': 'microsecond',
}

def convert_asa_postgresql_constructs(sql: str, target_type: str,
                                      source_catalog=None) -> tuple[str, list[dict]]:
    trace=[]
    sql,count=_convert_top_first(sql); _trace(trace,'asa-top-first-limit',count,'SELECT TOP/FIRST','LIMIT')
    sql,count=_convert_dateadd(sql); _trace(trace,'asa-dateadd-interval',count,'DATEADD','PostgreSQL interval arithmetic')
    sql,count=_convert_on_existing_skip(sql); _trace(trace,'asa-on-existing-conflict',count,'ON EXISTING SKIP','ON CONFLICT DO NOTHING')
    sql,count=_convert_list_aggregate(sql); _trace(trace,'asa-pg-function-list',count,'LIST','COALESCE(string_agg(...), \'\')')
    sql,count=_convert_locate(sql); _trace(trace,'asa-pg-function-locate',count,'LOCATE','strpos')
    sql,count=_convert_ifnull(sql); _trace(trace,'asa-pg-function-ifnull',count,'IFNULL','CASE/COALESCE')
    sql,count=_convert_character_plus(sql, source_catalog); _trace(trace,'asa-pg-operator-string-plus',count,'character + character','||')
    if target_type=='function':
        sql,count=_move_function_transactions_to_caller(sql); _trace(trace,'asa-function-caller-transaction',count,'COMMIT','caller-managed transaction')
    return sql,trace

def _trace(trace,code,count,source,output):
    if count: trace.append({'line':'preprocessor','source':source,'output':output,'rules':[{'rule_id':code,'rule_code':code,'priority':1970,'matches':count}]})

def _convert_top_first(sql):
    pattern=re.compile(r'\bSELECT\s+(?:(?:TOP\s+(?P<count>\d+))|(?P<first>FIRST))\b',re.I); total=0
    while True:
        match=pattern.search(sql)
        if not match:return sql,total
        end=_query_end(sql,match.end()); body=sql[match.end():end].rstrip()
        sql=sql[:match.start()]+f"SELECT{body}\nLIMIT {match.group('count') or '1'}"+sql[end:]; total+=1

def _query_end(sql,start):
    depth=0;quote=None;index=start;boundary=re.compile(r'\b(?:DO|LOOP|THEN|ELSE|ELSIF|END\s+IF|END\s+LOOP|END\s+FOR)\b',re.I)
    while index<len(sql):
        char=sql[index]
        if quote:
            if char==quote:
                if index+1<len(sql) and sql[index+1]==quote:index+=2;continue
                quote=None
        elif char in ("'",'"'):quote=char
        elif char=='(':depth+=1
        elif char==')':
            if depth==0:return index
            depth-=1
        elif depth==0 and (char==';' or boundary.match(sql,index)):return index
        index+=1
    return len(sql)

def _convert_dateadd(sql):
    total=0
    while True:
        changed=False
        for match in reversed(list(re.finditer(r'\bDATEADD\s*\(',sql,re.I))):
            close=_matching_paren(sql,match.end()-1)
            if close is None:continue
            args=_split_arguments(sql[match.end():close])
            if len(args)!=3:continue
            unit=DATE_UNITS.get(args[0].strip().strip("'\"").lower())
            if not unit:continue
            replacement=f"({args[2].strip()} + ({args[1].strip()}) * INTERVAL '1 {unit}')"
            sql=sql[:match.start()]+replacement+sql[close+1:];total+=1;changed=True
        if not changed:return sql,total

def _matching_paren(sql,open_at):
    depth=0;quote=None;index=open_at
    while index<len(sql):
        char=sql[index]
        if quote:
            if char==quote:
                if index+1<len(sql) and sql[index+1]==quote:index+=2;continue
                quote=None
        elif char in ("'",'"'):quote=char
        elif char=='(':depth+=1
        elif char==')':
            depth-=1
            if depth==0:return index
        index+=1
    return None

def _split_arguments(text):
    parts=[];start=0;depth=0;quote=None;index=0
    while index<len(text):
        char=text[index]
        if quote:
            if char==quote:
                if index+1<len(text) and text[index+1]==quote:index+=2;continue
                quote=None
        elif char in ("'",'"'):quote=char
        elif char=='(':depth+=1
        elif char==')':depth-=1
        elif char==',' and depth==0:parts.append(text[start:index].strip());start=index+1
        index+=1
    parts.append(text[start:].strip());return parts

def _convert_on_existing_skip(sql):
    pattern=re.compile(r'(?P<head>\bINSERT\s+INTO\b.*?)(?:\s+ON\s+EXISTING\s+SKIP)(?P<values>\s+VALUES\s*\(.*?\))(?P<end>\s*;)',re.I|re.S)
    return pattern.subn(lambda m:f"{m.group('head')}{m.group('values')}\nON CONFLICT DO NOTHING{m.group('end')}",sql)

def _move_function_transactions_to_caller(sql):
    guarded=re.compile(r"\bIF\s+VAREXISTS\s*\(\s*'(?P<name>gi_[^']+)'\s*\)\s*=\s*1\s+THEN\s+IF\s+(?P=name)\s*=\s*1\s+THEN\s+COMMIT\s*;?\s+END\s+IF\s*;?\s+END\s+IF\s*;?",re.I|re.S)
    sql,a=guarded.subn('NULL; /* transaction managed by caller */',sql)
    sql,b=re.subn(r'\bCOMMIT\s*;','NULL; /* COMMIT moved to caller */',sql,flags=re.I)
    sql,c=re.subn(r'\bROLLBACK\s*;',"RAISE EXCEPTION 'ASA rollback requested';",sql,flags=re.I)
    return sql,a+b+c


def _convert_list_aggregate(sql):
    total = 0
    while True:
        changed = False
        for match in reversed(list(re.finditer(r'\bLIST\s*\(', sql, re.I))):
            close = _matching_paren(sql, match.end() - 1)
            if close is None:
                continue
            content = sql[match.end():close]
            order_at = _top_level_keyword(content, 'ORDER BY')
            arguments_text = content[:order_at].strip() if order_at is not None else content.strip()
            ordering = content[order_at + len('ORDER BY'):].strip() if order_at is not None else ''
            arguments = _split_arguments(arguments_text)
            if not arguments or len(arguments) > 2:
                continue
            expression = arguments[0].strip()
            distinct = bool(re.match(r'^DISTINCT\b', expression, re.I))
            expression = re.sub(r'^(?:ALL|DISTINCT)\s+', '', expression, flags=re.I)
            delimiter = arguments[1].strip() if len(arguments) == 2 else "','"
            value = f"NULLIF(CAST({expression} AS TEXT), '')"
            prefix = 'DISTINCT ' if distinct else ''
            order_sql = f" ORDER BY {ordering}" if ordering else ''
            replacement = (
                f"COALESCE(string_agg({prefix}{value}, "
                f"COALESCE(CAST({delimiter} AS TEXT), ''){order_sql}), '')"
            )
            sql = sql[:match.start()] + replacement + sql[close + 1:]
            total += 1
            changed = True
        if not changed:
            return sql, total


def _convert_locate(sql):
    total = 0
    for match in reversed(list(re.finditer(r'\bLOCATE\s*\(', sql, re.I))):
        close = _matching_paren(sql, match.end() - 1)
        if close is None:
            continue
        arguments = _split_arguments(sql[match.end():close])
        if len(arguments) != 2:
            continue
        replacement = f"strpos(CAST({arguments[0]} AS TEXT), CAST({arguments[1]} AS TEXT))"
        sql = sql[:match.start()] + replacement + sql[close + 1:]
        total += 1
    return sql, total


def _convert_ifnull(sql):
    """Render ASA IFNULL according to arity instead of renaming the token."""
    total = 0
    while True:
        matches = list(re.finditer(r'\bIFNULL\s*\(', sql, re.I))
        if not matches:
            return sql, total
        changed = False
        # The rightmost call is innermost when calls are nested, so offsets stay valid.
        for match in reversed(matches):
            close = _matching_paren(sql, match.end() - 1)
            if close is None:
                continue
            arguments = _split_arguments(sql[match.end():close])
            if len(arguments) == 2:
                replacement = f"COALESCE({arguments[0]}, {arguments[1]})"
            elif len(arguments) == 3:
                replacement = (
                    f"(CASE WHEN {arguments[0]} IS NULL THEN {arguments[1]} "
                    f"ELSE {arguments[2]} END)"
                )
            else:
                continue
            sql = sql[:match.start()] + replacement + sql[close + 1:]
            total += 1
            changed = True
            break
        if not changed:
            return sql, total


def _convert_character_plus(sql, source_catalog=None):
    """Convert provably-character ASA + chains to PostgreSQL ||."""
    declared_types = _routine_symbol_types(sql)
    character_names = {
        name for name, data_type in declared_types.items()
        if _is_character_type(data_type)
    }
    column_types = _column_type_lookup(sql, source_catalog)
    identifier = r'(?:(?:"[^"]+"|[A-Za-z_]\w*)\.)?(?:"[^"]+"|@?[A-Za-z_]\w*)'
    operand = rf"(?:'(?:''|[^'])*'|{identifier}|__ASA_STRING_CONCAT__\s*\([^()]*\)|CAST\s*\([^()]+\s+AS\s+(?:VAR)?CHAR(?:\s*\(\s*\d+\s*\))?\s*\))"
    pattern = re.compile(rf'(?P<left>{operand})\s*\+\s*(?P<right>{operand})', re.I)
    total = 0
    while True:
        replacement = None
        for match in pattern.finditer(sql):
            left, right = match.group('left'), match.group('right')
            left_character = _known_character_operand(left, character_names, column_types)
            right_character = _known_character_operand(right, character_names, column_types)
            # A character literal/operand makes an ASA + chain a character
            # expression even when offline column metadata is absent. The
            # temporary marker lets later passes consume an entire + chain.
            inferred_chain = (
                left_character and _is_identifier_operand(right)
            ) or (
                right_character and _is_identifier_operand(left)
            )
            if (left_character and right_character) or inferred_chain:
                replacement = (match, f"__ASA_STRING_CONCAT__({left}, {right})")
                break
        if replacement is None:
            return _render_string_concat_markers(sql), total
        match, value = replacement
        sql = sql[:match.start()] + value + sql[match.end():]
        total += 1


def _known_character_operand(value, names, column_types=None):
    value = value.strip()
    return (value.startswith("'") or value.lower() in names
            or bool(re.match(r'^(?:CAST|__ASA_STRING_CONCAT__)\b', value, re.I))
            or (column_types is not None and _is_character_type(column_types(value))))


def _render_string_concat_markers(sql):
    marker = '__ASA_STRING_CONCAT__'
    while marker in sql:
        start = sql.find(marker)
        open_at = sql.find('(', start + len(marker))
        close = _matching_paren(sql, open_at)
        if open_at < 0 or close is None:
            break
        expression = sql[start:close + 1]
        parts = _string_concat_parts(expression)
        if len(parts) < 2:
            break
        sql = sql[:start] + '(' + ' || '.join(parts) + ')' + sql[close + 1:]
    return sql


def _string_concat_parts(expression):
    marker = '__ASA_STRING_CONCAT__'
    value = expression.strip()
    match = re.match(rf'^{marker}\s*\(', value, re.I)
    if not match:
        return [value]
    close = _matching_paren(value, match.end() - 1)
    if close != len(value) - 1:
        return [value]
    arguments = _split_arguments(value[match.end():close])
    if len(arguments) != 2:
        return [value]
    return _string_concat_parts(arguments[0]) + _string_concat_parts(arguments[1])


def _is_identifier_operand(value):
    return bool(re.fullmatch(
        r'(?:(?:"[^"]+"|[A-Za-z_]\w*)\.)?(?:"[^"]+"|@?[A-Za-z_]\w*)',
        value.strip(), re.I,
    ))


def _is_character_type(data_type):
    return bool(data_type and re.search(
        r'\b(?:CHAR|CHARACTER|VARCHAR|NCHAR|NVARCHAR|TEXT|CLOB|LONG\s+VARCHAR)\b',
        str(data_type), re.I,
    ))


def _routine_symbol_types(sql):
    """Build a lightweight ASA parameter/local-variable datatype table."""
    symbols = {}
    declaration = re.search(
        r'\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:PROC(?:EDURE)?|FUNCTION)\s+'
        r'[\w.$"\[\]]+', sql, re.I,
    )
    if declaration:
        opening = sql.find('(', declaration.end())
        if opening >= 0:
            closing = _matching_paren(sql, opening)
            if closing is not None:
                for parameter in _split_arguments(sql[opening + 1:closing]):
                    match = re.match(
                        r'\s*(?:(?:INOUT|IN|OUT)\s+)?'
                        r'(?P<name>@?(?:"[^"]+"|[A-Za-z_]\w*))\s+'
                        r'(?P<type>.+?)\s*$', parameter, re.I | re.S,
                    )
                    if match:
                        symbols[_symbol_name(match.group('name'))] = _declared_type(match.group('type'))

    for match in re.finditer(r'\bDECLARE\s+(?P<clause>.*?);', sql, re.I | re.S):
        clause = match.group('clause').strip()
        if re.match(r'^(?:LOCAL\s+TEMPORARY\s+TABLE|(?:DYNAMIC\s+)?(?:SCROLL\s+)?CURSOR)\b', clause, re.I):
            continue
        pending_names = []
        for item in _split_arguments(clause):
            item_match = re.match(
                r'\s*(?P<name>@?(?:"[^"]+"|[A-Za-z_]\w*))'
                r'(?:\s+(?P<type>.+?))?\s*$', item, re.I | re.S,
            )
            if not item_match:
                pending_names = []
                continue
            name = _symbol_name(item_match.group('name'))
            raw_type = item_match.group('type')
            if not raw_type:
                pending_names.append(name)
                continue
            data_type = _declared_type(raw_type)
            for declared_name in pending_names + [name]:
                symbols[declared_name] = data_type
            pending_names = []
    return symbols


def _symbol_name(value):
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1].replace('""', '"')
    return value.lower()


def _declared_type(value):
    value = re.split(r'\bDEFAULT\b|:=|(?<![<>!])=(?!=)', value, maxsplit=1, flags=re.I)[0]
    return re.sub(r'/\*.*?\*/', '', value, flags=re.S).strip()


def _column_type_lookup(sql, source_catalog):
    """Resolve qualified source columns through a captured catalogue when available."""
    if source_catalog is None or not hasattr(source_catalog, 'column_type'):
        return None
    relations = {}
    relation_pattern = re.compile(
        r'\b(?:FROM|JOIN)\s+(?:(?P<schema>"?[A-Za-z_]\w*"?)\.)?'
        r'(?P<table>"?[A-Za-z_]\w*"?)'
        r'(?:\s+(?:AS\s+)?(?P<alias>"?[A-Za-z_]\w*"?))?', re.I,
    )
    stop_words = {'where', 'join', 'left', 'right', 'full', 'inner', 'outer', 'cross',
                  'on', 'group', 'order', 'having', 'union', 'limit'}
    for match in relation_pattern.finditer(sql):
        schema = (match.group('schema') or 'dba').strip('"')
        table = match.group('table').strip('"')
        alias = (match.group('alias') or '').strip('"')
        relations[table.lower()] = (schema, table)
        if alias and alias.lower() not in stop_words:
            relations[alias.lower()] = (schema, table)

    def lookup(expression):
        match = re.fullmatch(
            r'(?:(?P<qualifier>"?[A-Za-z_]\w*"?)\.)'
            r'(?P<column>"?[A-Za-z_]\w*"?)', expression.strip(), re.I,
        )
        if not match:
            return None
        qualifier = match.group('qualifier').strip('"')
        column = match.group('column').strip('"')
        schema, table = relations.get(qualifier.lower(), ('dba', qualifier))
        try:
            return source_catalog.column_type(schema, table, column)
        except (KeyError, LookupError, TypeError, ValueError):
            return None

    return lookup


def _top_level_keyword(text, keyword):
    keyword_pattern = keyword.replace(' ', r'\s+')
    pattern = re.compile(rf'\b{keyword_pattern}\b', re.I)
    depth = 0
    quote = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif depth == 0:
            match = pattern.match(text, index)
            if match:
                return index
        index += 1
    return None
