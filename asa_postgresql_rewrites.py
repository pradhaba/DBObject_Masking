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

def convert_asa_postgresql_constructs(sql: str, target_type: str) -> tuple[str, list[dict]]:
    trace=[]
    sql,count=_convert_top_first(sql); _trace(trace,'asa-top-first-limit',count,'SELECT TOP/FIRST','LIMIT')
    sql,count=_convert_dateadd(sql); _trace(trace,'asa-dateadd-interval',count,'DATEADD','PostgreSQL interval arithmetic')
    sql,count=_convert_on_existing_skip(sql); _trace(trace,'asa-on-existing-conflict',count,'ON EXISTING SKIP','ON CONFLICT DO NOTHING')
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
