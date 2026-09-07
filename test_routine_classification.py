import unittest

from migration_engine import classify_postgresql_implementation, render_postgresql_routine


class RoutineClassificationTests(unittest.TestCase):
    def test_result_select_is_sql_function(self):
        source = "CREATE PROCEDURE dba.report(IN p_id INTEGER) BEGIN SELECT p_id AS id; END;"
        result = classify_postgresql_implementation(source)
        self.assertEqual(result["object_type"], "function")
        self.assertEqual(result["language"], "sql")
        self.assertEqual(result["language_rule"], "single-select-sql-language")

    def test_branching_result_routine_is_plpgsql_function(self):
        source = """CREATE PROCEDURE dba.report(IN p_id INTEGER)
        RESULT (id INTEGER)
        BEGIN IF p_id > 0 THEN SELECT p_id; ELSE SELECT 0; END IF; END;"""
        result = classify_postgresql_implementation(source)
        self.assertEqual(result["object_type"], "function")
        self.assertEqual(result["language"], "plpgsql")

    def test_transaction_routine_is_plpgsql_procedure(self):
        source = "CREATE PROCEDURE dba.save_it() BEGIN UPDATE items SET active = 1; COMMIT; END;"
        result = classify_postgresql_implementation(source)
        self.assertEqual(result["object_type"], "procedure")
        self.assertEqual(result["language"], "plpgsql")
        self.assertEqual(result["object_rule"], "transaction-control-procedure")

    def test_renderer_honors_plpgsql_decision_for_simple_select(self):
        source = "CREATE PROCEDURE dba.p() BEGIN SELECT 1; END;"
        rendered, _, language = render_postgresql_routine(
            source, "function", "value integer", preferred_language="plpgsql"
        )
        self.assertEqual(language, "plpgsql")
        self.assertIn("LANGUAGE plpgsql", rendered)


if __name__ == "__main__":
    unittest.main()
