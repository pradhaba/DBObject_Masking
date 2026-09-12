import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import sqlite3

import yaml

from database import (
    create_migration_reference, list_migration_references, record_processing,
)
from migration_references import (
    apply_relevant_references, extract_reference_corrections, source_features,
)


class MigrationReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'references.sqlite3'
        self.brain_dir = Path(self.temp.name) / 'brain'
        self.source = 'CREATE PROCEDURE dba.p() BEGIN SELECT OLD_VALUE FROM items; END;'
        self.generated = '''CREATE FUNCTION dba.p() RETURNS SETOF RECORD LANGUAGE sql AS $$
SELECT OLD_VALUE FROM dba.items;
$$;'''

    def tearDown(self):
        self.temp.cleanup()

    def _run(self):
        return record_processing(
            None, 'p.sql', 'migrate', 'postgresql', self.source, self.generated,
            source_dialect='sybase_asa', target_dialect='postgresql', path=self.path,
        )

    def test_reference_stores_source_generated_corrected_and_exact_edits(self):
        run_id = self._run()
        corrected = self.generated.replace('SELECT OLD_VALUE', 'SELECT NEW_VALUE')
        reference_id = create_migration_reference(
            run_id, corrected, 'reviewer', 'Corrected the result expression.', self.path,
            self.brain_dir,
        )
        references = list_migration_references(self.path)
        self.assertEqual(references[0]['id'], reference_id)
        self.assertEqual(references[0]['corrected_ddl'], corrected)
        self.assertEqual(references[0]['correction_count'], 1)
        lesson_path = Path(references[0]['knowledge_path'])
        self.assertTrue(lesson_path.exists())
        lesson = yaml.safe_load(lesson_path.read_text(encoding='utf-8'))
        self.assertEqual(lesson['verification']['status'], 'approved_human_reference')
        serialized = lesson_path.read_text(encoding='utf-8')
        self.assertNotIn('items', serialized.lower())
        self.assertNotIn('reviewer', serialized.lower())
        self.assertIn('TBL_', serialized)

    def test_relevant_reference_applies_only_an_exact_generated_fragment(self):
        run_id = self._run()
        corrected = self.generated.replace('SELECT OLD_VALUE', 'SELECT NEW_VALUE')
        create_migration_reference(
            run_id, corrected, 'reviewer', path=self.path, brain_dir=self.brain_dir
        )
        output, trace, references = apply_relevant_references(
            self.source, self.generated, 'sybase_asa', 'postgresql', self.path
        )
        self.assertIn('SELECT NEW_VALUE', output)
        self.assertEqual(references[0]['applied_corrections'], 1)
        self.assertEqual(trace[0]['rules'][0]['rule_code'], 'approved-migration-reference')

        different = self.generated.replace('OLD_VALUE', 'OTHER_VALUE')
        output, trace, references = apply_relevant_references(
            self.source, different, 'sybase_asa', 'postgresql', self.path
        )
        self.assertEqual(output, different)
        self.assertFalse(trace)
        self.assertEqual(references[0]['applied_corrections'], 0)

    def test_insert_only_changes_are_reference_only(self):
        corrected = self.generated.replace('$$;', '-- reviewed\n$$;')
        self.assertEqual(extract_reference_corrections(self.generated, corrected), [])

    def test_features_are_structural_not_object_specific(self):
        features = source_features(self.source)
        self.assertIn('CREATE_PROCEDURE', features)
        self.assertIn('SELECT', features)
        self.assertNotIn('ITEMS', features)

    def test_brain_export_does_not_run_inside_reference_write_transaction(self):
        run_id = self._run()
        corrected = self.generated.replace('SELECT OLD_VALUE', 'SELECT NEW_VALUE')
        lesson_path = self.brain_dir / 'lesson-test.yaml'

        def export_while_touching_same_database(*_args, **_kwargs):
            # Catalogue-backed masking may initialize/update the same runtime DB.
            # This write reproduces the lock seen when export ran inside the
            # migration-reference transaction.
            with closing(sqlite3.connect(self.path, timeout=0.05)) as second_connection, second_connection:
                second_connection.execute(
                    "UPDATE processing_runs SET review_notes=review_notes WHERE id=?",
                    (run_id,),
                )
            return lesson_path

        with patch('migration_brain.export_masked_lesson', export_while_touching_same_database):
            reference_id = create_migration_reference(
                run_id, corrected, 'reviewer', path=self.path, brain_dir=self.brain_dir
            )

        self.assertGreater(reference_id, 0)
        self.assertEqual(list_migration_references(self.path)[0]['knowledge_path'], str(lesson_path))


if __name__ == '__main__':
    unittest.main()
