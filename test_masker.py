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

    def test_masks_routine_parameters_and_declared_variables(self):
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
        self.assertEqual(
            {'@mail_merge_id', '@effectiveness', '@result'},
            set(mapping['parameters']),
        )
        self.assertEqual(sql, unmask_text(masked, mapping))

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
        self.assertIn('mail_merge_id integer', restored)
        self.assertIn('effectiveness numeric', restored)
        self.assertIn('SET effectiveness = 1', restored)
        self.assertNotIn('@', restored)
        self.assertNotIn('PARAM_', restored)


if __name__ == '__main__':
    unittest.main()
