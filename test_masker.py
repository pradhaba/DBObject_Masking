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
        self.assertIn('RETURNS TABLE (mailmerge_set_id int,', migrated)
        self.assertIn('mailmerge_set_name varchar(50),', migrated)
        self.assertIn('mailmerge_category_id int,', migrated)
        self.assertIn('date_column_name varchar(50))', migrated)
        self.assertIn('LANGUAGE sql', migrated)
        self.assertNotIn('RETURN QUERY SELECT 1', migrated)
        self.assertNotIn('\n,\n    mailmerge_category_id', migrated)

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
