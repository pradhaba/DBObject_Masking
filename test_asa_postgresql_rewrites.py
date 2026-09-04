import unittest
from asa_postgresql_rewrites import convert_asa_postgresql_constructs
from migration_engine import _unqualified_masked_column_diagnostics

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

if __name__=='__main__': unittest.main()
