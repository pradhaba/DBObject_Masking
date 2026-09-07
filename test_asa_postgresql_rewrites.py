import unittest
from asa_postgresql_rewrites import convert_asa_postgresql_constructs
from migration_engine import _apply_table_alias_policy, _unqualified_masked_column_diagnostics

class AsaPostgresqlRewriteTests(unittest.TestCase):
    def test_nested_top_and_first_move_to_query_end(self):
        source="SELECT (SELECT TOP 1 started FROM periods ORDER BY started), (SELECT FIRST id FROM items);"
        converted,_=convert_asa_postgresql_constructs(source,'function')
        self.assertIn('SELECT started FROM periods ORDER BY started\nLIMIT 1)',converted)
        self.assertIn('SELECT id FROM items\nLIMIT 1)',converted)
        self.assertNotRegex(converted,r'(?i)SELECT\s+(?:TOP|FIRST)\b')

    def test_dateadd_units_and_nested_arguments(self):
        converted,_=convert_asa_postgresql_constructs("SELECT DATEADD(dd,-1,period_start), DATEADD(month,amount,COALESCE(d1,d2));",'function')
        self.assertIn("period_start + (-1) * INTERVAL '1 day'",converted)
        self.assertIn("COALESCE(d1,d2) + (amount) * INTERVAL '1 month'",converted)

    def test_on_existing_skip_moves_after_values(self):
        converted,_=convert_asa_postgresql_constructs('INSERT INTO t(id,value) ON EXISTING SKIP VALUES (1,COALESCE(x,0));','function')
        self.assertIn('VALUES (1,COALESCE(x,0))\nON CONFLICT DO NOTHING;',converted)
        self.assertNotIn('ON EXISTING',converted.upper())

    def test_function_commit_guard_becomes_caller_managed(self):
        source="IF varexists('gi_allow_commits') = 1 THEN IF gi_allow_commits = 1 THEN commit; END IF; END IF;"
        converted,trace=convert_asa_postgresql_constructs(source,'function')
        self.assertEqual(converted,'NULL; /* transaction managed by caller */')
        self.assertTrue(any(item['rules'][0]['rule_code']=='asa-function-caller-transaction' for item in trace))

    def test_procedure_commit_is_preserved(self):
        converted,_=convert_asa_postgresql_constructs('COMMIT;','procedure')
        self.assertEqual(converted,'COMMIT;')

    def test_offline_column_review_is_warning(self):
        mapping = {'columns': {'COL_1': 'existing_column'}}
        diagnostics = _unqualified_masked_column_diagnostics(
            'SELECT * FROM TAB_1 WHERE COL_1 = 1;', mapping, 'sybase_asa',
            source_available=False,
        )
        self.assertEqual(diagnostics[0]['severity'], 'warning')
        self.assertIn('marked unavailable', diagnostics[0]['suggestion'])

    def test_list_aggregate_maps_to_string_agg_with_asa_empty_semantics(self):
        source = "SELECT LIST(DISTINCT name, ';' ORDER BY name) FROM people;"
        converted, trace = convert_asa_postgresql_constructs(source, 'function')
        self.assertIn("COALESCE(string_agg(DISTINCT NULLIF(CAST(name AS TEXT), ''),", converted)
        self.assertIn("COALESCE(CAST(';' AS TEXT), '') ORDER BY name), '')", converted)
        self.assertTrue(any(item['rules'][0]['rule_code']=='asa-pg-function-list' for item in trace))

    def test_locate_maps_to_strpos_with_reordered_semantics(self):
        converted, _ = convert_asa_postgresql_constructs(
            "SELECT LOCATE(description, 'needle') FROM items;", 'function'
        )
        self.assertIn("strpos(CAST(description AS TEXT), CAST('needle' AS TEXT))", converted)

    def test_plus_only_converts_provably_character_operands(self):
        source = """CREATE FUNCTION f(IN first_name VARCHAR(20), IN last_name VARCHAR(20), IN n INTEGER)
        BEGIN SELECT first_name + ' ' + last_name, n + 1; END;"""
        converted, _ = convert_asa_postgresql_constructs(source, 'function')
        self.assertIn("CONCAT(CONCAT(first_name, ' '), last_name)", converted)
        self.assertIn("n + 1", converted)

    def test_nested_join_conversion_preserves_outer_where_predicates(self):
        source = '''CREATE PROCEDURE dba.PROC_1(IN PARAM_1 INTEGER)
BEGIN
SELECT TBL_1.COL_1
FROM dba.TBL_1, dba.TBL_2, dba.TBL_3
WHERE TBL_1.COL_2 = PARAM_1
AND TBL_2.COL_3 = TBL_1.COL_3
AND EXISTS (
  SELECT * FROM dba.TBL_4, dba.TBL_5 "inner_user"
  WHERE "inner_user".COL_4 = TBL_4.COL_4
  AND TBL_4.COL_5 = TBL_1.COL_5
)
AND TBL_3.COL_6 IS NULL
END;'''
        mapping = {'tables': {
            'users': 'TBL_1', 'staff': 'TBL_2', 'parameters': 'TBL_3',
            'rights': 'TBL_4', 'members': 'TBL_5',
        }}
        converted, _ = _apply_table_alias_policy(source, '{"alias_length":3}', mapping)
        self.assertIn('use.COL_2 = PARAM_1', converted)
        self.assertIn('par.COL_6 IS NULL', converted)
        self.assertIn('EXISTS (', converted)
        self.assertIn('JOIN dba.TBL_5 "inner_user" ON "inner_user".COL_4 = rig.COL_4', converted)
        self.assertNotRegex(converted, r'(?is)\bEND\b\s*\bWHERE\b')

    def test_masked_table_token_after_relation_is_restored_as_missing_comma(self):
        source = '''SELECT * FROM dba.TBL_1
TBL_2,
TBL_3 "users2"
WHERE TBL_1.COL_1 = TBL_3.COL_1 AND TBL_2.COL_2 = TBL_3.COL_2;'''
        mapping = {'tables': {
            'users_rights': 'TBL_1', 'users_membership': 'TBL_2', 'users': 'TBL_3'
        }}
        converted, _ = _apply_table_alias_policy(source, '{"alias_length":3}', mapping)
        self.assertIn('FROM dba.TBL_1 AS usr', converted)
        self.assertIn('CROSS JOIN TBL_2 AS usm', converted)
        self.assertIn('JOIN TBL_3 "users2" ON', converted)

if __name__=='__main__': unittest.main()
