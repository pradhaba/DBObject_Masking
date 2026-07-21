import unittest

from masker import mask_text, suggest_mapping_filename, unmask_text


class MaskerTests(unittest.TestCase):
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
