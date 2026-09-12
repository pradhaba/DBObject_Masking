import tempfile
import unittest
from pathlib import Path

from migration_brain import export_masked_lesson, relevant_brain_lessons
from migration_references import extract_reference_corrections


class MigrationBrainTests(unittest.TestCase):
    def test_masked_yaml_is_discoverable_without_private_identifiers_or_literals(self):
        with tempfile.TemporaryDirectory() as temp:
            brain = Path(temp)
            source = """CREATE PROCEDURE dba.customer_report(IN @customer_id INTEGER)
            BEGIN SELECT customers.secret_name FROM dba.customers
            WHERE customers.customer_id = @customer_id
            AND customers.status = 'PRIVATE_STATUS'; END;"""
            generated = """CREATE FUNCTION dba.customer_report(IN p_customer_id INTEGER)
            RETURNS SETOF RECORD LANGUAGE sql AS $$
            SELECT customers.secret_name FROM dba.customers
            WHERE customers.customer_id = p_customer_id;
            $$;"""
            corrected = generated.replace('customers.secret_name', 'COALESCE(customers.secret_name, \'UNKNOWN\')')
            run = {
                'input_ddl': source, 'output_ddl': generated,
                'source_dialect': 'sybase_asa', 'target_dialect': 'postgresql',
            }
            path = export_masked_lesson(
                run, corrected, extract_reference_corrections(generated, corrected), brain
            )
            yaml_text = path.read_text(encoding='utf-8')
            self.assertNotIn('customer_report', yaml_text)
            self.assertNotIn('secret_name', yaml_text)
            self.assertNotIn('PRIVATE_STATUS', yaml_text)
            self.assertNotIn("'UNKNOWN'", yaml_text)
            self.assertIn('TEXT_LITERAL_', yaml_text)
            matches = relevant_brain_lessons(
                source, 'sybase_asa', 'postgresql', brain
            )
            self.assertEqual(matches[0]['lesson_id'], path.stem.removeprefix('lesson-'))


if __name__ == '__main__':
    unittest.main()
