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

    def test_quoted_alias_column_parameter_link_resolves_real_table(self):
        from routine_test_planner import build_routine_test_plan
        source = '''CREATE PROCEDURE dba.p(IN category_id INTEGER) BEGIN
        SELECT "mai"."mailmerge_category_id"
        FROM "dba"."mailmerge_items" AS "mai"
        WHERE "mai"."mailmerge_category_id" = category_id;
        END;'''
        target = source.replace('CREATE PROCEDURE', 'CREATE FUNCTION').replace('category_id;', 'p_category_id;')
        plan = build_routine_test_plan(source, target)
        finding = next(item for item in plan['suggestions'] if item['parameter'] == 'category_id')
        self.assertEqual(finding['table'], 'dba.mailmerge_items')
        self.assertEqual(finding['column'], 'mailmerge_category_id')

    def test_postgresql_data_lookup_uses_distinct_values_with_limit_five(self):
        from routine_test_planner import build_routine_test_plan, collect_data_findings
        plan = build_routine_test_plan('''CREATE PROCEDURE dba.p(IN category_id INTEGER) BEGIN
        SELECT "mai"."mailmerge_category_id" FROM "dba"."mailmerge_items" AS "mai"
        WHERE "mai"."mailmerge_category_id" = category_id; END;''')
        class Cursor:
            def __init__(self): self.queries=[]; self.rows=[]
            def __enter__(self): return self
            def __exit__(self,*_): return False
            def execute(self,query):
                self.queries.append(query)
                self.rows=[(10,),(20,),(30,)] if 'DISTINCT' in query else [(1,)]
            def fetchone(self): return self.rows[0] if self.rows else None
            def fetchmany(self,size): return self.rows[:size]
        class Connection:
            def __init__(self): self.current=Cursor()
            def cursor(self): return self.current
            def rollback(self): pass
        connection=Connection()
        collect_data_findings(connection,plan,'target','PostgreSQL')
        lookup=next(query for query in connection.current.queries if 'DISTINCT' in query)
        self.assertIn('SELECT DISTINCT "mailmerge_category_id"',lookup)
        self.assertIn('FROM "dba"."mailmerge_items"',lookup)
        self.assertTrue(lookup.endswith('LIMIT 5'))
        finding=next(item for item in plan['suggestions'] if item['parameter']=='category_id')
        self.assertEqual(finding['candidates'],[10,20,30])

    def test_source_data_check_never_derives_parameter_values(self):
        from routine_test_planner import build_routine_test_plan, collect_data_findings
        plan = build_routine_test_plan('''CREATE PROCEDURE dba.p(IN category_id INTEGER) BEGIN
        SELECT m.category_id FROM dba.mailmerge AS m WHERE m.category_id=category_id; END;''')
        class Cursor:
            def __init__(self): self.queries=[]; self.rows=[]
            def __enter__(self): return self
            def __exit__(self,*_): return False
            def execute(self,query): self.queries.append(query);self.rows=[(55,)]
            def fetchone(self): return self.rows[0]
            def fetchmany(self,size): return self.rows[:size]
        class Connection:
            def __init__(self): self.current=Cursor()
            def cursor(self): return self.current
            def rollback(self): pass
        connection=Connection()
        collect_data_findings(connection,plan,'source','SQL Anywhere ASA',derive_parameter_values=False)
        finding=next(item for item in plan['suggestions'] if item['parameter']=='category_id')
        self.assertEqual(finding['candidates'],[])
        self.assertFalse(any('DISTINCT' in query for query in connection.current.queries))


if __name__ == '__main__':
    unittest.main()
