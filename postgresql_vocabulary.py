"""Central PostgreSQL vocabulary used by migration and formatting.

The connected server's pg_catalog is authoritative for function overloads and
return types.  This module is the offline vocabulary used while rewriting SQL,
before a catalog query can be made.  Keep PostgreSQL-only additions here rather
than adding private keyword/function lists to individual modules.
"""

from sqlparse import keywords as sqlparse_keywords


POSTGRESQL_KEYWORDS = frozenset(
    set(sqlparse_keywords.KEYWORDS)
    | set(sqlparse_keywords.KEYWORDS_COMMON)
    | set(sqlparse_keywords.KEYWORDS_PLPGSQL)
    | {
        'ASC', 'DESC', 'QUERY', 'ELSIF', 'INOUT', 'VARIADIC', 'SETOF',
        'IMMUTABLE', 'STABLE', 'VOLATILE', 'STRICT', 'PARALLEL', 'COST', 'ROWS',
        'TEMP', 'TEMPORARY', 'UNLOGGED', 'IDENTITY', 'GENERATED', 'OVERRIDING',
        'CONFLICT', 'MATERIALIZED', 'RECURSIVE', 'LATERAL', 'FILTER', 'WINDOW',
        'WITHIN', 'ORDINALITY', 'DO', 'NOTHING', 'INCLUDE', 'STORED',
    }
)

# Names whose parenthesized form is SQL grammar or a type constructor rather
# than a project routine.  Function names without an underscore are already
# left unqualified by the custom-routine heuristic, but are included here so
# callers can ask a direct and complete vocabulary question.
POSTGRESQL_TYPES = frozenset({
    'bigint', 'bigserial', 'bit', 'boolean', 'box', 'bytea', 'char', 'character',
    'cidr', 'circle', 'date', 'decimal', 'double', 'inet', 'int', 'int2', 'int4',
    'int8', 'integer', 'interval', 'json', 'jsonb', 'line', 'lseg', 'macaddr',
    'macaddr8', 'money', 'numeric', 'path', 'pg_lsn', 'point', 'polygon', 'real',
    'serial', 'serial2', 'serial4', 'serial8', 'smallint', 'smallserial', 'text',
    'time', 'timestamp', 'timetz', 'timestamptz', 'tsquery', 'tsvector', 'uuid',
    'varbit', 'varchar', 'xml',
})

POSTGRESQL_SPECIAL_FORMS = frozenset({
    'array', 'cast', 'collation', 'current_date', 'current_role', 'current_schema',
    'current_time', 'current_timestamp', 'current_user', 'extract', 'localtime',
    'localtimestamp', 'normalize', 'overlay', 'position', 'session_user',
    'substring', 'system_user', 'trim', 'user', 'values',
})

# Offline protection for built-ins commonly containing underscores.  The live
# target pg_catalog remains the complete/version-correct function catalog.
POSTGRESQL_BUILTIN_FUNCTIONS = frozenset({
    'array_agg', 'array_append', 'array_cat', 'array_dims', 'array_fill',
    'array_length', 'array_lower', 'array_ndims', 'array_position',
    'array_positions', 'array_prepend', 'array_remove', 'array_replace',
    'array_to_json', 'array_to_string', 'array_upper', 'ascii', 'avg',
    'bit_length', 'bool_and', 'bool_or', 'btrim', 'cardinality', 'ceil',
    'ceiling', 'char_length', 'character_length', 'chr', 'clock_timestamp',
    'coalesce', 'concat', 'concat_ws', 'convert_from', 'convert_to', 'count',
    'current_database', 'current_query', 'current_schema', 'current_schemas',
    'current_setting', 'date_bin', 'date_part', 'date_trunc', 'decode',
    'digest', 'encode', 'enum_first', 'enum_last', 'enum_range', 'every',
    'format', 'gen_random_uuid', 'generate_series', 'generate_subscripts',
    'greatest', 'grouping', 'inet_client_addr', 'inet_client_port',
    'inet_server_addr', 'inet_server_port', 'initcap', 'json_agg',
    'json_array', 'json_array_length', 'json_build_array', 'json_build_object',
    'json_each', 'json_each_text', 'json_extract_path', 'json_extract_path_text',
    'json_object', 'json_object_agg', 'json_object_keys', 'json_populate_record',
    'json_populate_recordset', 'json_strip_nulls', 'json_to_record',
    'json_to_recordset', 'json_typeof', 'jsonb_agg', 'jsonb_array_length',
    'jsonb_build_array', 'jsonb_build_object', 'jsonb_each', 'jsonb_each_text',
    'jsonb_extract_path', 'jsonb_extract_path_text', 'jsonb_insert',
    'jsonb_object', 'jsonb_object_agg', 'jsonb_object_keys',
    'jsonb_path_exists', 'jsonb_path_match', 'jsonb_path_query',
    'jsonb_path_query_array', 'jsonb_path_query_first', 'jsonb_populate_record',
    'jsonb_populate_recordset', 'jsonb_set', 'jsonb_set_lax', 'jsonb_strip_nulls',
    'jsonb_to_record', 'jsonb_to_recordset', 'jsonb_typeof', 'least', 'length',
    'lower', 'lpad', 'ltrim', 'make_date', 'make_interval', 'make_time',
    'make_timestamp', 'make_timestamptz', 'max', 'md5', 'min', 'now',
    'nullif', 'num_nonnulls', 'num_nulls', 'octet_length', 'parse_ident',
    'pg_backend_pid', 'pg_column_size', 'pg_get_expr', 'pg_get_functiondef',
    'pg_get_indexdef', 'pg_get_serial_sequence', 'pg_is_in_recovery',
    'pg_postmaster_start_time', 'pg_relation_size', 'pg_size_pretty',
    'pg_sleep', 'pg_table_size', 'pg_total_relation_size', 'quote_ident',
    'quote_literal', 'quote_nullable', 'regexp_count', 'regexp_instr',
    'regexp_like', 'regexp_match', 'regexp_matches', 'regexp_replace',
    'regexp_split_to_array', 'regexp_split_to_table', 'regexp_substr',
    'repeat', 'replace', 'reverse', 'round', 'row_number', 'rpad', 'rtrim',
    'set_config', 'split_part', 'string_agg', 'string_to_array', 'strpos',
    'sum', 'timeofday', 'to_ascii', 'to_char', 'to_date', 'to_hex', 'to_json',
    'to_jsonb', 'to_number', 'to_regclass', 'to_regnamespace', 'to_regproc',
    'to_regprocedure', 'to_regrole', 'to_regtype', 'to_timestamp', 'translate',
    'trim_scale', 'unnest', 'upper', 'version', 'width_bucket', 'xmlagg',
    'xmlconcat', 'xmlelement', 'xmlexists', 'xmlforest', 'xmlparse',
    'xmlpi', 'xmlroot', 'xmlserialize',
})


def is_postgresql_keyword(name: str) -> bool:
    return name.upper() in POSTGRESQL_KEYWORDS


def is_postgresql_builtin(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in POSTGRESQL_BUILTIN_FUNCTIONS
        or lowered in POSTGRESQL_SPECIAL_FORMS
        or lowered in POSTGRESQL_TYPES
        or is_postgresql_keyword(name)
    )
