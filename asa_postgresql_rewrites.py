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
    sql,count=_convert_character_plus(sql, source_catalog); _trace(trace,'asa-pg-operator-string-plus',count,'character + character','CONCAT')
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
    """Convert only provably-character ASA + expressions, preserving NULL behavior."""
    character_names = set()
    for match in re.finditer(
        r'\b(?:IN|OUT|INOUT|DECLARE)\s+(@?[A-Za-z_]\w*)\s+'
        r'(?:LONG\s+VARCHAR|VARCHAR|CHAR|NCHAR|NVARCHAR|TEXT)\b', sql, re.I,
    ):
        character_names.add(match.group(1).lower())
    column_types = _column_type_lookup(sql, source_catalog)
    identifier = r'(?:(?:"[^"]+"|[A-Za-z_]\w*)\.)?(?:"[^"]+"|@?[A-Za-z_]\w*)'
    operand = rf"(?:'(?:''|[^'])*'|{identifier}|CONCAT\s*\([^()]*\)|CAST\s*\([^()]+\s+AS\s+(?:VAR)?CHAR(?:\s*\(\s*\d+\s*\))?\s*\))"
    pattern = re.compile(rf'(?P<left>{operand})\s*\+\s*(?P<right>{operand})', re.I)
    total = 0
    while True:
        replacement = None
        for match in pattern.finditer(sql):
            left, right = match.group('left'), match.group('right')
            if (_known_character_operand(left, character_names, column_types)
                    and _known_character_operand(right, character_names, column_types)):
                replacement = (match, f"CONCAT({left}, {right})")
                break
        if replacement is None:
            return sql, total
        match, value = replacement
        sql = sql[:match.start()] + value + sql[match.end():]
        total += 1


def _known_character_operand(value, names, column_types=None):
    value = value.strip()
    return (value.startswith("'") or value.lower() in names
            or bool(re.match(r'^(?:CAST|CONCAT)\b', value, re.I))
            or (column_types is not None and _is_character_type(column_types(value))))


def _is_character_type(data_type):
    return bool(data_type and re.search(
        r'\b(?:CHAR|CHARACTER|VARCHAR|NCHAR|NVARCHAR|TEXT|CLOB|LONG\s+VARCHAR)\b',
        str(data_type), re.I,
    ))


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
