import unittest

from masker import mask_text, suggest_mapping_filename, unmask_text


class MaskerTests(unittest.TestCase):
    def test_masking_prefixes_are_loaded_from_database(self):
        from database import get_masking_rules
        rules = {rule['object_type']: rule['token_prefix'] for rule in get_masking_rules()}
        masked, mapping = mask_text('CREATE TABLE Customer (id int);', embed_mapping=False)
        self.assertEqual(mapping['tables']['Customer'].split('_')[0], rules['table'])

    def test_database_backed_migration_skill_preserves_names(self):
        import tempfile
        from pathlib import Path
        from database import approve_skill_version, get_skill_version_rules, list_skill_versions, review_skill_rule
        from migration_engine import migrate_text
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'skills.sqlite3'
            candidate = next(v for v in list_skill_versions(path) if v['source_dialect']=='sybase_asa' and v['target_dialect']=='postgresql' and v['status']=='awaiting_approval')
            for rule in get_skill_version_rules(candidate['id'], path):
                review_skill_rule(rule['id'], 'approved', 'test approval', path)
            approve_skill_version(candidate['id'], 'test approver', path)
            migrated, mapping, skill = migrate_text(
                'CREATE PROCEDURE Customer() BEGIN SELECT GETDATE(); END',
                'sybase_asa', 'postgresql', path,
            )
        self.assertIn('Customer', migrated)
        self.assertIn('CURRENT_TIMESTAMP', migrated)
        self.assertIn('CREATE OR REPLACE FUNCTION', migrated)
        self.assertEqual(skill['source_dialect'], 'sybase_asa')
        self.assertIn('Customer', mapping['procedures'])

    def test_asa_skill_rejects_non_procedure_objects(self):
        from migration_engine import migrate_text
        with self.assertRaisesRegex(ValueError, 'procedures only'):
            migrate_text('CREATE TABLE Customer (id int);', 'sybase_asa', 'postgresql')

    def test_postgresql_routine_classification(self):
        from migration_engine import classify_postgresql_routine
        self.assertEqual(classify_postgresql_routine('CREATE PROCEDURE p() BEGIN COMMIT; END')[0], 'procedure')
        self.assertEqual(classify_postgresql_routine('CREATE PROCEDURE p(OUT result integer) BEGIN END')[0], 'procedure')
        self.assertEqual(classify_postgresql_routine('CREATE PROCEDURE p() BEGIN SELECT value FROM t; END')[0], 'function')

    def test_result_clause_keeps_nested_datatype_parentheses(self):
        import tempfile
        from pathlib import Path
        from database import approve_skill_version, get_skill_version_rules, list_skill_versions, review_skill_rule
        from migration_engine import migrate_text
        sql = '''CREATE PROCEDURE dba.p()
RESULT (
    mailmerge_set_id int,
    mailmerge_set_name varchar(50),
    mailmerge_category_id int,
    date_column_name varchar(50)
)
BEGIN
    SELECT 1, 'a', 2, 'b';
END;'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'skills.sqlite3'
            candidate = next(v for v in list_skill_versions(path) if v['source_dialect']=='sybase_asa' and v['target_dialect']=='postgresql' and v['status']=='awaiting_approval')
            for rule in get_skill_version_rules(candidate['id'], path):
                review_skill_rule(rule['id'], 'approved', 'test', path)
            approve_skill_version(candidate['id'], 'tester', path)
            migrated, _, _ = migrate_text(sql, 'sybase_asa', 'postgresql', path)
        self.assertIn('RETURNS TABLE (\n    mailmerge_set_id int', migrated)
        self.assertIn('\n    , mailmerge_set_name TEXT', migrated)
        self.assertIn('\n    , mailmerge_category_id int', migrated)
        self.assertIn('\n    , date_column_name TEXT\n)', migrated)
        self.assertIn('LANGUAGE sql', migrated)
        self.assertNotIn('RETURN QUERY SELECT 1', migrated)

    def test_nested_select_in_where_is_not_return_query(self):
        from migration_engine import _convert_top_level_result_selects
        body = '''BEGIN
    SELECT id FROM parent
    WHERE 0 < (
        SELECT count(*)
        FROM child
        WHERE child.parent_id = parent.id
    );
END'''
        converted = _convert_top_level_result_selects(body)
        self.assertIn('RETURN QUERY SELECT id FROM parent', converted)
        self.assertIn('\n        SELECT count(*)', converted)
        self.assertNotIn('RETURN QUERY SELECT count(*)', converted)

    def test_language_sql_requires_one_simple_select(self):
        from migration_engine import _simple_select_body
        self.assertIsNotNone(_simple_select_body('BEGIN\nSELECT id FROM customer;\nEND;'))
        self.assertIsNone(_simple_select_body('BEGIN\nIF x = 1 THEN\nSELECT id FROM customer;\nEND IF;\nEND;'))
        self.assertIsNone(_simple_select_body('BEGIN\nSELECT id INTO target FROM customer;\nUPDATE audit SET used = 1;\nEND;'))

    def test_plpgsql_declarations_and_set_assignments_are_normalized(self):
        from migration_engine import _normalize_plpgsql_body
        declarations, body = _normalize_plpgsql_body('''BEGIN
    DECLARE _li_user_id INTEGER;
    SET
    _li_user_id = dba.get_int_var('gi_user_id');
END''')
        self.assertEqual(declarations.strip(), '_li_user_id INTEGER;')
        self.assertNotIn('DECLARE _li_user_id', body)
        self.assertNotIn('\n    SET\n', body)
        self.assertIn("_li_user_id := dba.get_int_var('gi_user_id');", body)

    def test_schema_qualification_handles_tables_and_internal_calls(self):
        from migration_engine import _qualify_schema_references
        source = 'SELECT wa_someone_get_it(report_code) FROM tablename1 AS a JOIN tablename2 AS b ON a.id=b.id'
        migrated, count = _qualify_schema_references(source, 'dba')
        self.assertIn('dba.wa_someone_get_it(report_code)', migrated)
        self.assertIn('FROM dba.tablename1 AS a', migrated)
        self.assertIn('JOIN dba.tablename2 AS b', migrated)
        self.assertEqual(count, 3)
        unchanged, count = _qualify_schema_references('SELECT count(*) FROM dba.tablename1', 'dba')
        self.assertEqual(unchanged, 'SELECT count(*) FROM dba.tablename1')
        self.assertEqual(count, 0)
        keywords = "RETURNS TABLE (x int) WHEN 'PRICE_LIST' THEN (CASE WHEN STRING('x') = 'x' AND (LEN(x) > 0) THEN 1 END)"
        unchanged, count = _qualify_schema_references(keywords, 'dba')
        self.assertEqual(unchanged, keywords)
        self.assertEqual(count, 0)

    def test_table_alias_policy_rewrites_table_qualifiers(self):
        from migration_engine import _apply_table_alias_policy
        masked = "SELECT TBL_1.COL_1, TBL_2.COL_2 FROM dba.TBL_1 JOIN dba.TBL_2 ON TBL_1.COL_3 = TBL_2.COL_3"
        mapping = {"tables": {"report_items": "TBL_1", "reports": "TBL_2"}}
        policy = '{"alias_length":3,"aliases":{"report_items":"ret"}}'
        migrated, count = _apply_table_alias_policy(masked, policy, mapping)
        self.assertIn("ret.COL_1", migrated)
        self.assertIn("rep.COL_2", migrated)
        self.assertIn("FROM dba.TBL_1 AS ret", migrated)
        self.assertIn("JOIN dba.TBL_2 AS rep", migrated)
        self.assertIn("ON ret.COL_3 = rep.COL_3", migrated)
        self.assertEqual(count, 6)

    def test_table_alias_policy_preserves_existing_alias_and_avoids_collisions(self):
        from migration_engine import _apply_table_alias_policy
        masked = "SELECT TBL_1.COL_1, TBL_2.COL_2 FROM TBL_1 AS x JOIN TBL_2 ON TBL_1.COL_3=TBL_2.COL_3"
        mapping = {"tables": {"report_one": "TBL_1", "report_two": "TBL_2"}}
        migrated, _ = _apply_table_alias_policy(masked, '{"alias_length":3,"aliases":{}}', mapping)
        self.assertIn("FROM TBL_1 AS x", migrated)
        self.assertIn("JOIN TBL_2 AS ret", migrated)
        self.assertIn("x.COL_1", migrated)
        self.assertIn("ret.COL_2", migrated)

    def test_table_alias_policy_handles_comma_separated_tables(self):
        from migration_engine import _apply_table_alias_policy
        masked = (
            "SELECT TBL_1.COL_1, TBL_2.COL_2 "
            "FROM dba.TBL_1, dba.TBL_2 "
            "WHERE TBL_1.COL_3 = TBL_2.COL_3"
        )
        mapping = {"tables": {"report_items": "TBL_1", "reports": "TBL_2"}}
        migrated, _ = _apply_table_alias_policy(masked, '{"alias_length":3,"aliases":{}}', mapping)
        self.assertIn("FROM dba.TBL_1 AS rei", migrated)
        self.assertIn("JOIN dba.TBL_2 AS rep ON rei.COL_3 = rep.COL_3", migrated)
        self.assertIn("rei.COL_1", migrated)
        self.assertIn("rep.COL_2", migrated)

    def test_table_aliases_use_word_letters_and_never_numeric_suffixes(self):
        from migration_engine import _apply_table_alias_policy
        masked = (
            "SELECT TBL_1.COL_1, TBL_2.COL_2 FROM dba.TBL_1, dba.TBL_2 "
            "WHERE TBL_1.COL_1=TBL_2.COL_1"
        )
        mapping = {"tables": {"mailmerge_sets": "TBL_1", "mailmerge_types": "TBL_2"}}
        migrated, _ = _apply_table_alias_policy(masked, '{"alias_length":3,"aliases":{}}', mapping)
        self.assertIn("FROM dba.TBL_1 AS mas", migrated)
        self.assertIn("JOIN dba.TBL_2 AS mat ON mas.COL_1=mat.COL_1", migrated)
        self.assertNotRegex(migrated, r'\b[A-Za-z_]+\d+\b(?=\.)')

    def test_true_alias_collision_uses_letters_instead_of_number(self):
        from migration_engine import _apply_table_alias_policy
        masked = (
            "SELECT TBL_1.COL_1, TBL_2.COL_2 FROM TBL_1, TBL_2 "
            "WHERE TBL_1.COL_1=TBL_2.COL_1"
        )
        mapping = {"tables": {"mailmerge_sets": "TBL_1", "main_sets": "TBL_2"}}
        migrated, _ = _apply_table_alias_policy(masked, '{"alias_length":3,"aliases":{}}', mapping)
        self.assertIn("FROM TBL_1 AS mas", migrated)
        self.assertIn("JOIN TBL_2 AS mam", migrated)
        self.assertNotIn("mas2", migrated)

    def test_masks_every_table_in_multiline_comma_from_list(self):
        sql = '''SELECT reports.report_id, actions.action_id
    FROM
    dba."reports",
    dba."actions",
    dba."table_1",
    dba."table_2"
    WHERE reports.report_id = actions.report_id'''
        masked, mapping = mask_text(sql, 'sybase_asa', embed_mapping=False)
        self.assertEqual(
            set(mapping['tables']),
            {'reports', 'actions', 'table_1', 'table_2'},
        )
        self.assertIn('dba.TBL_1', masked)
        self.assertIn('dba.TBL_2', masked)
        self.assertIn('dba.TBL_3', masked)
        self.assertIn('dba.TBL_4', masked)

    def test_comma_tables_become_joins_using_where_relationships(self):
        from migration_engine import _apply_table_alias_policy
        masked = '''SELECT TBL_1.COL_1
FROM
dba.TBL_1,
dba.TBL_2,
dba.TBL_3
WHERE
CONCAT('REPORT_', TBL_1.COL_2) = TBL_2.COL_3
AND TBL_2.COL_4 = TBL_3.COL_4
AND TBL_1.COL_5 = 'Y';'''
        mapping = {'tables': {'reports': 'TBL_1', 'actions': 'TBL_2', 'details': 'TBL_3'}}
        migrated, _ = _apply_table_alias_policy(masked, '{"alias_length":3,"aliases":{}}', mapping)
        self.assertIn('FROM dba.TBL_1 AS rep', migrated)
        self.assertIn("JOIN dba.TBL_2 AS act ON CONCAT('REPORT_', rep.COL_2) = act.COL_3", migrated)
        self.assertIn('JOIN dba.TBL_3 AS det ON act.COL_4 = det.COL_4', migrated)
        self.assertIn("WHERE\nrep.COL_5 = 'Y'", migrated)
        self.assertNotIn('dba.TBL_1 AS rep,', migrated)

    def test_comma_table_without_relationship_becomes_cross_join(self):
        from migration_engine import _apply_table_alias_policy
        masked = "SELECT * FROM TBL_1, TBL_2 WHERE TBL_1.COL_1 = 'Y'"
        mapping = {'tables': {'reports': 'TBL_1', 'actions': 'TBL_2'}}
        migrated, _ = _apply_table_alias_policy(masked, '{"alias_length":3,"aliases":{}}', mapping)
        self.assertIn('CROSS JOIN TBL_2 AS act', migrated)

    def test_parenthesized_where_relationships_become_joins(self):
        from migration_engine import _convert_comma_tables_to_joins
        sql = '''FROM
treat AS tre,
patients AS pat,
staff AS sta,
procedures AS pro,
wtlist_r AS wtl
WHERE
((pro.item_id = tre.item_id)
AND (pat.patient_id = tre.patient_id)
AND (sta.member_id = tre.provider_id)
AND (tre.account_id IS NULL))'''
        migrated, count = _convert_comma_tables_to_joins(sql)
        self.assertEqual(count, 1)
        self.assertIn('JOIN patients AS pat ON pat.patient_id = tre.patient_id', migrated)
        self.assertIn('JOIN staff AS sta ON sta.member_id = tre.provider_id', migrated)
        self.assertIn('JOIN procedures AS pro ON pro.item_id = tre.item_id', migrated)
        self.assertIn('CROSS JOIN wtlist_r AS wtl', migrated)
        self.assertIn('WHERE\ntre.account_id IS NULL', migrated)

    def test_multiple_selects_and_nested_subqueries_convert_independently(self):
        from migration_engine import _convert_comma_tables_to_joins
        sql = '''IF PARAM_1 = 999 THEN
SELECT (SELECT TBL_2.COL_1 FROM TBL_2 WHERE TBL_2.COL_2 = TBL_1.COL_2)
FROM TBL_5 AS t5, TBL_1 AS t1, TBL_4 AS t4
WHERE ((t1.COL_3 = t5.COL_3) AND (t4.COL_4 = t5.COL_4) AND (t5.COL_5 IS NULL))
ELSE
SELECT (SELECT TBL_2.COL_1 FROM TBL_2 WHERE TBL_2.COL_2 = TBL_1.COL_2)
FROM TBL_5 AS t5, TBL_1 AS t1, TBL_3 AS t3
WHERE ((t1.COL_3 = t5.COL_3) AND (t3.COL_6 = t5.COL_6) AND (t5.COL_5 IS NULL));
END IF;'''
        migrated, count = _convert_comma_tables_to_joins(sql)
        self.assertEqual(count, 2)
        self.assertEqual(migrated.count('JOIN TBL_1 AS t1 ON'), 2)
        self.assertIn('JOIN TBL_4 AS t4 ON t4.COL_4 = t5.COL_4', migrated)
        self.assertIn('JOIN TBL_3 AS t3 ON t3.COL_6 = t5.COL_6', migrated)
        self.assertEqual(migrated.count('FROM TBL_2 WHERE'), 2)
        self.assertEqual(migrated.count('WHERE\nt5.COL_5 IS NULL'), 2)

    def test_aliases_are_reused_in_each_select_scope_before_join_conversion(self):
        from migration_engine import _apply_table_alias_policy
        sql = '''IF PARAM_1 = 999 THEN
SELECT (SELECT head.COL_1 FROM TBL_1 AS head WHERE head.COL_2 = head.COL_2)
FROM TBL_5, TBL_1, TBL_4, TBL_3
WHERE ((TBL_3.COL_3 = TBL_5.COL_3) AND (head.COL_4 = TBL_5.COL_4) AND (TBL_4.COL_5 = TBL_5.COL_5))
ELSE
SELECT head.COL_1
FROM TBL_5, TBL_1, TBL_4, TBL_3
WHERE ((TBL_3.COL_3 = TBL_5.COL_3) AND (head.COL_4 = TBL_5.COL_4) AND (TBL_4.COL_5 = TBL_5.COL_5));
END IF;'''
        mapping = {'tables': {
            'treat': 'TBL_5', 'patients': 'TBL_1',
            'staff': 'TBL_4', 'procedures': 'TBL_3',
        }}
        migrated, _ = _apply_table_alias_policy(sql, '{"alias_length":3,"aliases":{}}', mapping)
        self.assertEqual(migrated.count('FROM TBL_5 AS tre'), 2)
        self.assertEqual(migrated.count('JOIN TBL_1 AS head ON'), 2)
        self.assertEqual(migrated.count('JOIN TBL_4 AS sta ON'), 2)
        self.assertEqual(migrated.count('JOIN TBL_3 AS pro ON'), 2)

    def test_already_masked_procedure_is_not_masked_again(self):
        from migration_engine import _identity_mapping, _is_already_masked
        sql = '''CREATE PROCEDURE dba.PROC_7(IN PARAM_35 INTEGER)
        BEGIN SELECT TBL_1.COL_28, TBL_5.COL_32
        FROM TBL_5, TBL_1
        WHERE TBL_1.COL_19 = TBL_5.COL_19; END;'''
        self.assertTrue(_is_already_masked(sql))
        mapping = _identity_mapping(sql)
        self.assertEqual(mapping['tables']['TBL_1'], 'TBL_1')
        self.assertEqual(mapping['columns']['COL_32'], 'COL_32')
        self.assertNotIn('parameters', mapping)

    def test_multiline_asa_if_expression_becomes_case(self):
        from migration_engine import _convert_asa_if_expressions
        source = '''IF head.COL_10 = head.COL_19 THEN
head.COL_28
ELSE
(SELECT head.COL_28 FROM TBL_1 AS head WHERE head.COL_10 = head.COL_19)
ENDIF AS head_surname'''
        converted = _convert_asa_if_expressions(source)
        self.assertIn('CASE WHEN head.COL_10 = head.COL_19 THEN head.COL_28 ELSE', converted)
        self.assertIn('END AS head_surname', converted)
        self.assertNotRegex(converted.lower(), r'\bendif\b')

    def test_result_select_branches_are_aligned_by_output_name(self):
        from result_metadata import align_result_selects
        source = '''BEGIN
        IF x = 1 THEN
          SELECT t.first_name, t.identifier AS item_id FROM TBL_1 t;
        ELSE
          SELECT t.identifier AS item_id, t.first_name FROM TBL_1 t;
        END IF;
        END;'''
        aligned = align_result_selects(source)
        second = aligned.lower().rfind('select')
        self.assertLess(
            aligned.lower().find('t.first_name', second),
            aligned.lower().find('t.identifier as item_id', second),
        )

    def test_renderer_uses_inferred_returns_table_contract(self):
        from migration_engine import render_postgresql_routine
        source = 'CREATE PROCEDURE dba.PROC_1() BEGIN SELECT TBL_1.COL_1 FROM TBL_1; END;'
        rendered, _, _ = render_postgresql_routine(source, 'function', 'COL_1 integer')
        self.assertIn('RETURNS TABLE (\n    COL_1 integer\n)', rendered)
        self.assertNotIn('RETURNS SETOF RECORD', rendered)

    def test_returns_table_character_types_are_normalized_to_text(self):
        from migration_engine import _normalize_returns_table_types
        contract = 'a varchar(50), b CHAR(*), c character varying(120), d character varying, e integer, f CHARACTER(40), g CHARACTER'
        self.assertEqual(
            _normalize_returns_table_types(contract),
            'a TEXT, b TEXT, c TEXT, d character varying, e integer, f TEXT, g CHARACTER',
        )

    def test_character_return_columns_are_cast_to_text_in_result_query(self):
        from migration_engine import render_postgresql_routine
        source = '''CREATE PROCEDURE dba.p()
        RESULT (col1 INTEGER, col2 VARCHAR(300))
        BEGIN
        SELECT tab.col1 AS col1, tab.col2 AS col2 FROM TBL_1 AS tab;
        END'''
        rendered, _, _ = render_postgresql_routine(source, 'function')
        self.assertIn('col2 TEXT', rendered)
        self.assertIn('(tab.col2)::TEXT AS col2', rendered)

    def test_pre_normalized_text_contract_still_casts_result_expression(self):
        from migration_engine import render_postgresql_routine
        source = '''CREATE PROCEDURE dba.p()
        RESULT (col1 INTEGER, col2 TEXT)
        BEGIN SELECT tab.col1 AS col1, tab.col2 AS col2 FROM TBL_1 AS tab; END'''
        rendered, _, _ = render_postgresql_routine(source, 'function')
        self.assertIn('(tab.col2)::TEXT AS col2', rendered)

    def test_metadata_mismatch_casts_expression_to_declared_result_type(self):
        from migration_engine import render_postgresql_routine
        source = '''CREATE PROCEDURE dba.p()
        RESULT (item_id INTEGER, amount NUMERIC(20,4))
        BEGIN SELECT tab.item_id AS item_id, tab.amount AS amount FROM TBL_1 AS tab; END'''
        rendered, _, _ = render_postgresql_routine(
            source, 'function', 'item_id smallint, amount numeric(12,2)'
        )
        self.assertIn('(tab.item_id)::INTEGER AS item_id', rendered)
        self.assertIn('(tab.amount)::NUMERIC(20,4) AS amount', rendered)

    def test_text_return_cast_is_not_duplicated(self):
        from migration_engine import _cast_returns_table_text_outputs
        source = '''CREATE FUNCTION dba.p() RETURNS TABLE (value TEXT) LANGUAGE sql AS $$
        SELECT tab.value::TEXT AS value FROM dba.tab AS tab;
        $$;'''
        rendered = _cast_returns_table_text_outputs(source)
        self.assertEqual(rendered.upper().count('::TEXT'), 1)

    def test_return_query_is_terminated_before_else_and_end_if(self):
        from migration_engine import render_postgresql_routine
        source = '''CREATE PROCEDURE dba.p(IN p_mode INTEGER)
        RESULT (value INTEGER)
        BEGIN
        IF p_mode = 1 THEN
          SELECT t.value FROM TBL_1 AS t
        ELSE
          SELECT t.value FROM TBL_1 AS t
        END IF;
        END;'''
        rendered, _, _ = render_postgresql_routine(source, 'function')
        self.assertRegex(rendered, r't\.value\s+FROM TBL_1 AS t;\s*\n\s*ELSE')
        self.assertRegex(rendered, r't\.value\s+FROM TBL_1 AS t;\s*\n\s*END IF')

    def test_sql_case_else_does_not_end_return_query(self):
        from migration_engine import _terminate_return_queries
        body = '''BEGIN
RETURN QUERY SELECT CASE
WHEN true THEN 1
ELSE 2
END AS value
FROM dba.t
END;'''
        terminated = _terminate_return_queries(body)
        self.assertIn('ELSE 2', terminated)
        self.assertIn('FROM dba.t;', terminated)

    def test_postgresql_formatter_uses_spaces_and_structured_joins(self):
        from postgres_formatter import format_postgresql_routine
        source = '''CREATE OR REPLACE FUNCTION dba.f(
IN p_id INTEGER
)
RETURNS TABLE (
x integer
)
LANGUAGE plpgsql
AS $$
BEGIN
IF p_id = 1 THEN
RETURN QUERY select
t.x
FROM dba.t AS t
JOIN dba.u AS u ON u.id = t.id
WHERE t.active = true
AND u.active = true;
ELSE
RETURN QUERY SELECT t.x FROM dba.t AS t;
END IF;
END;
$$;'''
        formatted = format_postgresql_routine(source)
        self.assertNotIn('\t', formatted)
        self.assertIn('    IF p_id = 1 THEN', formatted)
        self.assertIn('        RETURN QUERY\n        SELECT', formatted)
        self.assertIn('        JOIN dba.u AS u', formatted)
        self.assertIn('            ON u.id = t.id', formatted)
        self.assertIn('            AND u.active = true;', formatted)

    def test_postgresql_formatter_supports_customer_indentation_styles(self):
        from postgres_formatter import format_postgresql_routine
        source = '''CREATE OR REPLACE PROCEDURE dba.p()
LANGUAGE plpgsql
AS $$
BEGIN
IF true THEN
END IF;
END;
$$;'''
        two_spaces = format_postgresql_routine(source, '2 spaces')
        tabs = format_postgresql_routine(source, 'Tabs')
        self.assertIn('\n  IF true THEN\n', two_spaces)
        self.assertIn('\n\tIF true THEN\n', tabs)

    def test_postgresql_formatter_uses_leading_select_list_commas(self):
        from postgres_formatter import format_postgresql_routine
        source = '''CREATE OR REPLACE FUNCTION dba.f()
RETURNS TABLE (
column1 INTEGER,
column2 TEXT,
column3 TEXT
)
LANGUAGE plpgsql
AS $$
BEGIN
RETURN QUERY
SELECT
t.column1,
(t.first_name || t.last_name),
t.column3
FROM dba.people AS t;
END;
$$;'''
        formatted = format_postgresql_routine(source, '4 spaces')
        self.assertIn(
            'SELECT\n'
            '        t.column1\n'
            '        , (t.first_name || t.last_name)\n'
            '        , t.column3\n'
            '    FROM',
            formatted,
        )

    def test_postgresql_formatter_uses_leading_parameter_and_return_commas(self):
        from postgres_formatter import format_postgresql_routine
        source = '''CREATE OR REPLACE FUNCTION dba.f(
IN p_id INTEGER,
IN p_amount NUMERIC(20,4),
IN p_name TEXT
)
RETURNS TABLE (
item_id INTEGER,
amount NUMERIC(20,4),
item_name TEXT
)
LANGUAGE sql
AS $$
SELECT
p_id,
p_amount,
p_name;
$$;'''
        formatted = format_postgresql_routine(source)
        self.assertIn(
            'CREATE OR REPLACE FUNCTION dba.f(\n'
            '    IN p_id INTEGER\n'
            '    , IN p_amount NUMERIC(20,4)\n'
            '    , IN p_name TEXT\n'
            ')',
            formatted,
        )
        self.assertIn(
            'RETURNS TABLE (\n'
            '    item_id INTEGER\n'
            '    , amount NUMERIC(20,4)\n'
            '    , item_name TEXT\n'
            ')',
            formatted,
        )

    def test_postgresql_formatter_normalizes_dba_schema_without_changing_literals(self):
        from postgres_formatter import format_postgresql_routine
        source = '''CREATE OR REPLACE FUNCTION "dba".f()
RETURNS TABLE (
value TEXT
)
LANGUAGE sql
AS $$
SELECT
t.value
FROM dba.items AS t
WHERE t.note = 'keep "dba".items and dba.items literal';
$$;'''
        formatted = format_postgresql_routine(source)
        self.assertIn('FUNCTION dba.f()', formatted)
        self.assertIn('FROM dba.items AS t', formatted)
        self.assertIn("'keep \"dba\".items and dba.items literal'", formatted)

    def test_dynamic_temp_report_uses_static_parameterized_branches(self):
        from dynamic_temp_renderer import render_dynamic_temp_report, supports_dynamic_temp_report
        rendered = render_dynamic_temp_report()
        self.assertIn('DROP TABLE IF EXISTS pg_temp.tmp_records', rendered)
        self.assertIn('CREATE TEMPORARY TABLE pg_temp.tmp_records', rendered)
        self.assertNotIn('CREATE TEMPORARY TABLE IF NOT EXISTS', rendered)
        self.assertEqual(rendered.count('INSERT INTO tmp_records'), 3)
        self.assertIn('p_a1_session_id', rendered)
        self.assertIn('NULLIF(tmp.acc_tot, 0)', rendered)
        self.assertNotIn('EXECUTE', rendered)
        self.assertNotIn('||', rendered)
        fixture = '''create procedure dba.any_name() begin
        declare local temporary table tmp_records(acc_category integer);
        select patients_accounts_provider, account_payment_plan, acc_category;
        execute immediate @sql; end'''
        self.assertTrue(supports_dynamic_temp_report(fixture))
        self.assertIn('FUNCTION dba.any_name(', render_dynamic_temp_report(fixture))

    def test_simple_dynamic_sql_is_inlined_without_object_specific_logic(self):
        from dynamic_temp_renderer import inline_simple_dynamic_sql
        source = """create procedure dba.any_proc(in p_id integer) begin
        declare @statement long varchar;
        set @statement = 'delete from audit_rows where row_id = ' || p_id;
        execute immediate @statement;
        end;"""
        converted, count = inline_simple_dynamic_sql(source)
        self.assertEqual(count, 1)
        self.assertIn('delete from audit_rows where row_id = p_id;', converted.lower())
        self.assertNotIn('execute immediate', converted.lower())
        self.assertNotIn('declare @statement', converted.lower())

    def test_dynamic_identifiers_are_not_mistaken_for_static_values(self):
        from dynamic_temp_renderer import inline_simple_dynamic_sql
        source = """create procedure dba.any_proc(in table_name varchar(30)) begin
        declare @statement long varchar;
        set @statement = 'select * from ' || table_name;
        execute immediate @statement;
        end;"""
        converted, count = inline_simple_dynamic_sql(source)
        self.assertEqual(count, 0)
        self.assertIn('execute immediate', converted.lower())

    def test_strict_cte_suitability_requires_one_consumer_and_append_only_writes(self):
        from cte_analyzer import analyze_cte_suitability
        eligible = analyze_cte_suitability('''
        CREATE TEMPORARY TABLE pg_temp.stage(id integer) ON COMMIT DROP;
        INSERT INTO stage (id) SELECT src.id FROM dba.source AS src;
        SELECT s.id FROM stage AS s;''')[0]
        self.assertTrue(eligible['eligible'])
        self.assertEqual(eligible['mode'], 'single_cte')
        rejected = analyze_cte_suitability('''
        CREATE TEMPORARY TABLE pg_temp.stage(id integer) ON COMMIT DROP;
        INSERT INTO stage (id) SELECT src.id FROM dba.source AS src;
        UPDATE stage SET id = 2;
        SELECT s.id FROM stage AS s;
        SELECT COUNT(*) FROM stage;''')[0]
        self.assertFalse(rejected['eligible'])
        self.assertEqual(rejected['mode'], 'keep_temporary_table')

    def test_cte_suitability_detects_complex_derived_query_without_temp_table(self):
        from cte_analyzer import analyze_cte_suitability
        decisions = analyze_cte_suitability('''
        SELECT report.provider_id, report.total
        FROM (
            SELECT provider_id, SUM(fee) AS total
            FROM dba.treat
            GROUP BY provider_id
        ) AS report;''')
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0]['eligible'])
        self.assertEqual(decisions[0]['mode'], 'readability_cte')
        self.assertEqual(decisions[0]['table'], 'report')
        simple = analyze_cte_suitability('SELECT p.id FROM dba.patients AS p;')
        self.assertEqual(simple, [])

    def test_cte_suitability_does_not_promote_scalar_subquery(self):
        from cte_analyzer import analyze_cte_suitability
        decisions = analyze_cte_suitability('''
        SELECT p.id, (SELECT MAX(t.fee) FROM dba.treat AS t WHERE t.patient_id=p.id)
        FROM dba.patients AS p;''')
        self.assertEqual(decisions, [])

    def test_complex_derived_query_is_rewritten_to_readability_cte(self):
        from cte_analyzer import apply_readability_ctes
        source = '''RETURN QUERY
        SELECT report.provider_id, report.total
        FROM (
            SELECT provider_id, SUM(fee) AS total
            FROM dba.treat
            GROUP BY provider_id
        ) AS report;'''
        converted, decisions = apply_readability_ctes(source)
        self.assertIn('WITH report AS (', converted)
        self.assertIn('FROM report;', converted)
        self.assertNotIn('FROM (', converted)
        self.assertEqual(decisions[0]['mode'], 'implemented_readability_cte')

    def test_metadata_resolver_accepts_quoted_table_qualifier(self):
        from result_metadata import _resolve_expression
        class Cursor:
            def __enter__(self):return self
            def __exit__(self,*_):return False
            def execute(self,_query,params):self.params=params
            def fetchone(self):return ('integer',) if self.params == ('dba','treat','provider_id') else None
        class Connection:
            def cursor(self):return Cursor()
        self.assertEqual(
            _resolve_expression('"treat".provider_id', Connection(), {}, 'dba'),
            ('provider_id', 'integer'),
        )

    def test_result_metadata_qualifies_unique_unqualified_result_columns(self):
        from result_metadata import qualify_unqualified_result_columns
        columns = {
            ('dba', 'saved_reports', 'saved_report_web_id'): 'integer',
            ('dba', 'saved_reports', 'file_size'): 'integer',
            ('dba', 'reports', 'report_id'): 'integer',
        }
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, _query, params): self.params = tuple(value.lower() for value in params)
            def fetchone(self):
                value = columns.get(self.params)
                return (value,) if value else None
        class Connection:
            def cursor(self): return Cursor()
        source = '''SELECT saved_report_web_id, file_size
FROM dba.saved_reports, dba.reports
ORDER BY saved_report_web_id ASC;'''
        qualified = qualify_unqualified_result_columns(source, Connection())
        self.assertIn('"saved_reports".saved_report_web_id', qualified)
        self.assertIn('"saved_reports".file_size', qualified)
        self.assertIn('ORDER BY "saved_reports".saved_report_web_id ASC', qualified)

    def test_join_conversion_preserves_newline_before_order_by(self):
        from migration_engine import _convert_comma_tables_to_joins
        source = '''FROM dba.TBL_1 AS one, dba.TBL_2 AS two
WHERE one.COL_1 = two.COL_1
ORDER BY one.COL_1;'''
        converted, count = _convert_comma_tables_to_joins(source)
        self.assertEqual(count, 1)
        self.assertIn('ON one.COL_1 = two.COL_1\nORDER BY', converted)

    def test_migration_comment_removal_preserves_strings_and_lines(self):
        from migration_engine import _strip_sql_comments
        source = "SELECT '--not comment', '// also text', '/* also text */' /* remove\nthis */\n-- remove line\n// remove ASA line\nFROM test;"
        cleaned = _strip_sql_comments(source)
        self.assertIn("'--not comment'", cleaned)
        self.assertIn("'// also text'", cleaned)
        self.assertIn("'/* also text */'", cleaned)
        self.assertNotIn('remove', cleaned)
        self.assertEqual(cleaned.count('\n'), source.count('\n'))

    def test_unterminated_quoted_identifier_reports_source_location(self):
        from migration_engine import migrate_text
        source = '''CREATE PROCEDURE dba.p()
BEGIN
SELECT t."broken
FROM dba.t
END'''
        with self.assertRaisesRegex(ValueError, r'Unterminated double-quoted identifier at line 3, column 10'):
            migrate_text(source, 'sybase_asa', 'postgresql')

    def test_end_keyword_is_not_used_as_an_implicit_table_alias(self):
        from migration_engine import _apply_table_alias_policy
        mapping = {'tables': {'mailmerge_sets': 'TBL_1'}}
        rendered, _ = _apply_table_alias_policy(
            'SELECT TBL_1.COL_1 FROM dba.TBL_1\nEND',
            '{"alias_length": 3}',
            mapping,
        )
        self.assertIn('FROM dba.TBL_1 AS mas\nEND', rendered)
        self.assertNotIn('AS END', rendered)

    def test_grouped_filters_and_scalar_subquery_do_not_become_join_conditions(self):
        from migration_engine import _convert_comma_tables_to_joins
        sql = '''FROM TBL_5 AS tre, TBL_1 AS head, TBL_4 AS sta, TBL_3 AS pro, TBL_6 AS wtl
WHERE ((pro.COL_14 = tre.COL_14)
AND (head.COL_19 = tre.COL_19)
AND (head.COL_26 IS NULL)
AND (sta.COL_16 = tre.COL_25)
AND (tre.COL_8 IS NULL))
AND (PARAM_34 = (SELECT sta.COL_24 FROM TBL_4 AS sta WHERE sta.COL_16 = tre.COL_25) OR PARAM_34 = 0)
AND sta.COL_16 = wtl.COL_12
AND wtl.COL_27 = 0
ELSE'''
        migrated, count = _convert_comma_tables_to_joins(sql)
        self.assertEqual(count, 1)
        self.assertIn('JOIN TBL_1 AS head ON head.COL_19 = tre.COL_19', migrated)
        self.assertIn('JOIN TBL_4 AS sta ON sta.COL_16 = tre.COL_25', migrated)
        self.assertIn('JOIN TBL_3 AS pro ON pro.COL_14 = tre.COL_14', migrated)
        self.assertIn('JOIN TBL_6 AS wtl ON sta.COL_16 = wtl.COL_12', migrated)
        self.assertIn('PARAM_34 = (SELECT sta.COL_24 FROM TBL_4 AS sta WHERE', migrated)
        self.assertIn('head.COL_26 IS NULL', migrated)
        self.assertIn('wtl.COL_27 = 0;\nELSE', migrated)

    def test_masks_qualified_table_and_columns(self):
        sql = 'CREATE TABLE db.dbo.Customer (CustomerId int, "Display Name" varchar(50));'
        masked, mapping = mask_text(sql, embed_mapping=False)
        self.assertNotIn('Customer', masked)
        self.assertNotIn('CustomerId', masked)
        self.assertNotIn('Display Name', masked)
        self.assertEqual('TBL_1', mapping['tables']['Customer'])
        self.assertIn('CustomerId', mapping['columns'])

    def test_masks_proc_abbreviation_and_altered_function(self):
        sql = '''
        CREATE PROC db.dbo.GetCustomer AS SELECT 1;
        ALTER FUNCTION reporting.CustomerName() RETURNS varchar(50) AS BEGIN RETURN 'x'; END;
        '''
        masked, mapping = mask_text(sql, embed_mapping=False)
        self.assertNotIn('GetCustomer', masked)
        self.assertNotIn('CustomerName', masked)
        self.assertIn('GetCustomer', mapping['procedures'])
        self.assertIn('CustomerName', mapping['functions'])

    def test_round_trip(self):
        sql = 'CREATE TABLE dbo.Orders (OrderId int, Amount decimal(10, 2));'
        masked, mapping = mask_text(sql, embed_mapping=False)
        self.assertEqual(sql, unmask_text(masked, mapping))

    def test_masks_tables_and_qualified_columns_inside_procedure(self):
        sql = '''create procedure dba."w3_get_fee_levels"()
        BEGIN
            select "pract_fee_levels"."id", "pract_fee_levels"."description"
            from "pract_fee_levels"
        END;'''
        masked, mapping = mask_text(sql, dialect='sybase_asa', embed_mapping=False)
        self.assertNotIn('w3_get_fee_levels', masked)
        self.assertNotIn('pract_fee_levels', masked)
        self.assertNotIn('"id"', masked)
        self.assertNotIn('description', masked)
        self.assertIn('pract_fee_levels', mapping['tables'])
        self.assertEqual({'description', 'id'}, set(mapping['columns']))

    def test_mapping_filename_uses_first_procedure_or_table(self):
        procedure_first = 'CREATE VIEW v AS SELECT 1; CREATE PROC dbo.GetFees AS SELECT 1; CREATE TABLE Fees(id int);'
        table_first = 'CREATE TABLE "Fee Levels" (id int); CREATE PROCEDURE get_fees() BEGIN END;'
        self.assertEqual('GetFees_mapping.json', suggest_mapping_filename(procedure_first))
        self.assertEqual('Fee Levels_mapping.json', suggest_mapping_filename(table_first))

    def test_keeps_routine_parameters_and_declared_variables_separate(self):
        sql = '''create procedure dba.we_are_procs(
            IN @mail_merge_id integer,
            @effectiveness numeric(3,2))
        BEGIN
            DECLARE @result integer;
            SET @result = @mail_merge_id;
        END;'''
        masked, mapping = mask_text(sql, dialect='sybase_asa', embed_mapping=False)
        self.assertNotIn('@mail_merge_id', masked)
        self.assertNotIn('@effectiveness', masked)
        self.assertNotIn('@result', masked)
        self.assertIn('@PARAM_', masked)
        self.assertIn('@VAR_', masked)
        self.assertEqual(
            {'@mail_merge_id', '@effectiveness'},
            set(mapping['parameters']),
        )
        self.assertEqual({'@result'}, set(mapping['variables']))
        self.assertEqual(sql, unmask_text(masked, mapping))

    def test_masks_sybase_select_into_and_multi_name_declare_as_variables(self):
        sql = '''CREATE PROCEDURE dba.sp_asa_migration_test
        (
            IN @member_id INTEGER,
            IN @ai_right INTEGER,
            IN @user_id INTEGER,
        )
        BEGIN
        DECLARE @res log varchar;
        DECLARE @alias_id, @member_type integer;
        select min("users_aliases"."alias_id")
        into @alias_id
        from "dba"."users_aliases"
        where "users_aliases"."user_id" = @user_id;
        END;'''
        masked, mapping = mask_text(sql, dialect='sybase_asa', embed_mapping=False)

        self.assertEqual(
            {'@alias_id', '@member_type', '@res'},
            set(mapping['variables']),
        )
        self.assertNotIn('@alias_id', mapping['tables'])
        self.assertNotIn('@member_type', masked)
        self.assertRegex(masked, r'into\s+@VAR_\d+')
        restored = unmask_text(masked, mapping)
        self.assertIn('into @alias_id', restored)
        self.assertIn('DECLARE @alias_id, @member_type integer', restored)

    def test_masks_unqualified_update_columns(self):
        sql = '''update "DNA".fee_levels
        set effectiveness = @PARAM_3
        where mail_merge_id = @PARAM_02;'''
        masked, mapping = mask_text(sql, dialect='sybase_asa', embed_mapping=False)
        self.assertNotIn('effectiveness', masked)
        self.assertNotIn('mail_merge_id', masked)
        self.assertEqual(
            {'effectiveness', 'mail_merge_id'},
            set(mapping['columns']),
        )
        self.assertIn('set COL_', masked)
        self.assertIn('where COL_', masked)
        self.assertEqual(sql, unmask_text(masked, mapping))

    def test_unmasks_parameter_after_target_dialect_removes_at_sign(self):
        mapping = {
            'procedures': {'we_are_procs': 'PROC_1'},
            'parameters': {
                '@mail_merge_id': '@PARAM_3',
                '@effectiveness': '@PARAM_2',
            },
        }
        translated = '''create procedure PROC_1(
            IN PARAM_3 integer, PARAM_2 numeric(3,2))
        BEGIN SET PARAM_2 = 1; END;'''
        restored = unmask_text(translated, mapping, dialect='postgresql')
        self.assertIn('p_mail_merge_id integer', restored)
        self.assertIn('p_effectiveness numeric', restored)
        self.assertIn('SET p_effectiveness = 1', restored)
        self.assertNotIn('@', restored)
        self.assertNotIn('PARAM_', restored)

    def test_postgresql_does_not_duplicate_existing_p_prefix(self):
        mapping = {'parameters': {'p_customer_id': 'PARAM_1'}}
        self.assertEqual(
            'SELECT p_customer_id;',
            unmask_text('SELECT PARAM_1;', mapping, dialect='postgresql'),
        )

    def test_unmasks_bare_sybase_variables_in_postgresql_translation(self):
        mapping = {
            'variables': {
                '@alias_id': '@VAR_12',
                '@member_type': '@VAR_13',
                '@res': '@VAR_14',
            },
        }
        translated = '''DECLARE
            var_14 VARCHAR;
            var_12 INTEGER;
            var_13 INTEGER;
        BEGIN
            SELECT MIN(alias_id) INTO var_12;
        END;'''

        restored = unmask_text(translated, mapping, dialect='postgresql')

        self.assertIn('_res VARCHAR', restored)
        self.assertIn('_alias_id INTEGER', restored)
        self.assertIn('_member_type INTEGER', restored)
        self.assertIn('INTO _alias_id', restored)
        self.assertNotRegex(restored, r'(?i)\bvar_\d+\b')


if __name__ == '__main__':
    unittest.main()
