import unittest


class RoutineTestPlannerTests(unittest.TestCase):
    def test_extracts_branch_values_and_column_based_candidates(self):
        from routine_test_planner import build_routine_test_plan
        plan = build_routine_test_plan('''
        CREATE PROCEDURE dba.report(IN p_mode INTEGER, IN p_provider INTEGER)
        BEGIN
          IF p_mode = 999 THEN
            SELECT t.id FROM dba.treat AS t WHERE t.provider_id = p_provider;
          END IF;
        END;''')
        self.assertEqual(plan['routine_name'], 'dba.report')
        mode = next(item for item in plan['suggestions'] if item['parameter'] == 'p_mode')
        self.assertEqual(mode['candidates'], [999, 1000])
        provider = next(item for item in plan['suggestions'] if item['parameter'] == 'p_provider')
        self.assertEqual(provider['table'], 'dba.treat')
        self.assertEqual(provider['column'], 'provider_id')

    def test_data_findings_report_empty_tables_and_add_samples(self):
        from routine_test_planner import apply_data_findings, build_routine_test_plan
        plan = build_routine_test_plan('''CREATE PROCEDURE dba.p(IN p_id INTEGER) BEGIN
        SELECT t.id FROM dba.treat AS t WHERE t.id=p_id; END;''')
        apply_data_findings(plan, 'target', {'dba.treat': 0}, {('dba.treat', 'id'): []})
        table = next(item for item in plan['tables'] if item['name'] == 'dba.treat')
        self.assertEqual(table['status'], 'empty')
        self.assertEqual(table['target_rows'], 0)

    def test_target_dependencies_are_merged_and_local_temp_table_is_excluded(self):
        from routine_test_planner import build_routine_test_plan
        source = '''CREATE PROCEDURE dba.p(IN p_id INTEGER) BEGIN
        DECLARE LOCAL TEMPORARY TABLE tmp_rows(id INTEGER);
        SELECT t.id FROM treat AS t WHERE t.id=p_id; END;'''
        target = '''CREATE FUNCTION dba.p(IN p_id INTEGER) RETURNS SETOF INTEGER AS $$ BEGIN
        CREATE TEMPORARY TABLE pg_temp.tmp_rows(id INTEGER);
        RETURN QUERY SELECT t.id FROM dba.treat AS t JOIN dba.staff AS s ON s.member_id=t.provider_id;
        END $$ LANGUAGE plpgsql;'''
        plan = build_routine_test_plan(source, target)
        names = {item['name'] for item in plan['tables']}
        self.assertIn('dba.treat', names)
        self.assertIn('dba.staff', names)
        self.assertNotIn('tmp_rows', names)
        self.assertNotIn('pg_temp.tmp_rows', names)

    def test_generates_table_function_scalar_function_and_procedure_calls(self):
        from routine_test_planner import build_routine_test_plan, generate_invocation_sql
        source = '''CREATE PROCEDURE dba.p(IN p_id INTEGER) BEGIN IF p_id = 7 THEN SELECT 1; END IF; END;'''
        table_plan = build_routine_test_plan(source, 'CREATE FUNCTION dba.p(IN p_id INTEGER) RETURNS TABLE(id INTEGER) LANGUAGE sql AS $$ SELECT 1 $$;')
        self.assertEqual(generate_invocation_sql(table_plan)[0], 'SELECT * FROM dba.p(7);')
        scalar_plan = build_routine_test_plan(source, 'CREATE FUNCTION dba.p(IN p_id INTEGER) RETURNS INTEGER LANGUAGE sql AS $$ SELECT 1 $$;')
        self.assertEqual(generate_invocation_sql(scalar_plan)[0], 'SELECT dba.p(7);')
        procedure_plan = build_routine_test_plan(source, 'CREATE PROCEDURE dba.p(IN p_id INTEGER) LANGUAGE plpgsql AS $$ BEGIN END $$;')
        self.assertEqual(generate_invocation_sql(procedure_plan)[0], 'CALL dba.p(7);')

    def test_invocation_formats_date_and_escaped_text_values(self):
        from routine_test_planner import _sql_literal
        self.assertEqual(_sql_literal('2026-08-24', 'DATE'), "'2026-08-24'::date")
        self.assertEqual(_sql_literal("O'Brien", 'VARCHAR(30)'), "'O''Brien'")


if __name__ == '__main__':
    unittest.main()
