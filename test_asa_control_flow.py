import unittest

from asa_control_flow import control_flow_diagnostics, convert_asa_control_flow


class AsaControlFlowTests(unittest.TestCase):
    def test_labeled_while_leave_and_closing_label_convert(self):
        source = '''outer_loop:
        WHILE counter < 10 LOOP
            IF counter = 5 THEN LEAVE outer_loop; END IF;
            CONTINUE outer_loop;
        END LOOP outer_loop;'''
        converted = convert_asa_control_flow(source, lambda value: value)
        self.assertIn('<<outer_loop>>', converted)
        self.assertIn('WHILE counter < 10 LOOP', converted)
        self.assertIn('EXIT outer_loop;', converted)
        self.assertIn('CONTINUE outer_loop;', converted)
        self.assertIn('END LOOP;', converted)
        self.assertNotIn('LEAVE', converted.upper())

    def test_unconditional_loop_and_unlabeled_leave(self):
        converted = convert_asa_control_flow('LOOP\n LEAVE;\nEND LOOP;', lambda value: value)
        self.assertEqual(converted, 'LOOP\n EXIT;\nEND LOOP;')

    def test_sql_psm_while_do_converts(self):
        converted = convert_asa_control_flow(
            'WHILE amount > 0 DO\n SET amount = amount - 1;\nEND WHILE;', lambda value: value
        )
        self.assertIn('WHILE amount > 0 LOOP', converted)
        self.assertIn('END LOOP;', converted)

    def test_tsql_break_converts_to_exit(self):
        converted = convert_asa_control_flow('WHILE ready LOOP\nBREAK\nEND LOOP;', lambda value: value)
        self.assertIn('EXIT;', converted)

    def test_unresolved_dynamic_and_numeric_loops_are_diagnosed(self):
        source = 'FOR r AS c CURSOR USING sql_text DO x = 1; END FOR; FOR i = 1 TO 5 DO x = i; END FOR;'
        codes = {item['code'] for item in control_flow_diagnostics(source, source)}
        self.assertIn('ASA_DYNAMIC_CURSOR_USING', codes)
        self.assertIn('ASA_NUMERIC_FOR_UNSUPPORTED', codes)
        self.assertIn('ASA_FOR_LOOP_UNRESOLVED', codes)

    def test_missing_exit_label_is_diagnosed(self):
        diagnostics = control_flow_diagnostics('LOOP LEAVE missing; END LOOP;', 'LOOP EXIT missing; END LOOP;')
        self.assertIn('LOOP_LABEL_NOT_FOUND', {item['code'] for item in diagnostics})


if __name__ == '__main__':
    unittest.main()
