import unittest

from postgresql_vocabulary import is_postgresql_builtin, is_postgresql_keyword
from result_metadata import _source_builtin_return_type, infer_returns_table


class ResultMetadataTests(unittest.TestCase):
    def test_shared_vocabulary_covers_keywords_types_and_underscore_builtins(self):
        self.assertTrue(is_postgresql_keyword('lateral'))
        self.assertTrue(is_postgresql_builtin('timestamp'))
        self.assertTrue(is_postgresql_builtin('date_trunc'))
        self.assertTrue(is_postgresql_builtin('jsonb_build_object'))

    def test_schema_qualification_preserves_underscore_builtins(self):
        from migration_engine import _qualify_schema_references

        sql = (
            "SELECT date_trunc('day', now()), current_setting('TimeZone'), "
            "jsonb_build_object('id', id), wa_someone_get_it(id) FROM reports"
        )
        qualified, _ = _qualify_schema_references(sql, 'dba')
        self.assertIn("date_trunc('day', now())", qualified)
        self.assertIn("current_setting('TimeZone')", qualified)
        self.assertIn("jsonb_build_object('id', id)", qualified)
        self.assertIn('dba.wa_someone_get_it(id)', qualified)
        self.assertIn('FROM dba.reports', qualified)

    def test_standard_aggregate_return_types_are_resolved_as_builtins(self):
        self.assertEqual(_source_builtin_return_type('min', ['date']), 'date')
        self.assertEqual(_source_builtin_return_type('max', ['character varying(22)']),
                         'character varying(22)')
        self.assertEqual(_source_builtin_return_type('count', ['integer']), 'bigint')
        self.assertEqual(_source_builtin_return_type('sum', ['integer']), 'bigint')
        self.assertEqual(_source_builtin_return_type('avg', ['integer']), 'numeric')

    def test_min_does_not_query_for_dba_function_metadata(self):
        class Cursor:
            def __init__(self):
                self.params = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, query, params):
                if 'pg_proc' in query:
                    raise AssertionError('min must not be resolved as dba.min')
                self.params = params

            def fetchone(self):
                if self.params == ('dba', 'staff', 'member_id'):
                    return ('integer',)
                return None

        class Connection:
            def cursor(self):
                return Cursor()

        sql = 'SELECT min(staff.member_id) AS op_member_id FROM dba.staff AS staff;'
        self.assertEqual(infer_returns_table(sql, Connection()), 'op_member_id integer')


if __name__ == '__main__':
    unittest.main()
