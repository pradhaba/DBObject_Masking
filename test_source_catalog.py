import sqlite3
import tempfile
import unittest
from pathlib import Path

from source_catalog import capture_source_catalog, referenced_relations


class SourceCatalogTests(unittest.TestCase):
    def test_discovers_persistent_relations_but_not_local_temp_tables(self):
        sql = '''DECLARE LOCAL TEMPORARY TABLE ids(id integer);
        SELECT p.period_id FROM dba.a_periods p JOIN app_books b ON b.id=p.book_id;
        INSERT INTO ids VALUES (1);'''
        self.assertEqual(referenced_relations(sql), [('dba', 'a_periods'), ('dba', 'app_books')])

    def test_captures_asa_columns_in_sqlite_and_resolves_them(self):
        class Cursor:
            def execute(self, query, params):
                self.query, self.params = query, tuple(value.lower() for value in params)
                return self
            def fetchall(self):
                if 'SYS.SYSTABCOL' in self.query and self.params == ('dba', 'a_periods'):
                    return [('period_id', 1, 'integer', None, 32, 0, 'NO'),
                            ('app_book_id', 2, 'integer', None, 32, 0, 'YES')]
                return []
            def fetchone(self):
                if 'SYS.SYSVIEW' in self.query:
                    return ('BASE', None)
                return None
            def close(self): pass
        class Connection:
            def cursor(self): return Cursor()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'catalog.sqlite3'
            catalog = capture_source_catalog(
                Connection(), None, 'SELECT period_id FROM dba.a_periods;', path
            )
            self.assertEqual(catalog.column_type('dba', 'a_periods', 'period_id'), 'integer')
            db = sqlite3.connect(path)
            try:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM source_catalog_objects').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM source_catalog_columns').fetchone()[0], 2)
            finally:
                db.close()

    def test_source_snapshot_qualifies_column_missing_from_postgresql(self):
        from result_metadata import qualify_unqualified_result_columns
        from source_catalog import SourceCatalog

        source = SourceCatalog(1, {('dba', 'a_periods', 'app_book_id'): 'integer'})
        qualified = qualify_unqualified_result_columns(
            'SELECT app_book_id FROM dba.a_periods WHERE app_book_id > 0;',
            object(), source_catalog=source,
        )
        self.assertIn('SELECT "a_periods".app_book_id', qualified)
        self.assertIn('WHERE "a_periods".app_book_id', qualified)


if __name__ == '__main__':
    unittest.main()
