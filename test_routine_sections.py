import unittest

from routine_sections import split_asa_routine


class RoutineSectionTests(unittest.TestCase):
    def test_lossless_function_sections_ignore_keywords_in_literals(self):
        source = """-- generated\nCREATE FUNCTION dba.f_total(\n  IN p_id INT\n) RETURNS MONEY\nBEGIN\n  DECLARE v_note LONG VARCHAR;\n  SET v_note = 'BEGIN RESULT EXCEPTION';\n  RETURN 1;\nEND;"""
        document = split_asa_routine(source)
        self.assertEqual(document.render(), source)
        kinds = [section.kind for section in document.sections]
        self.assertIn("parameters", kinds)
        self.assertIn("return_contract", kinds)
        self.assertIn("declarations", kinds)
        self.assertEqual(kinds[-1], "body")

    def test_result_contract_and_exception_handler(self):
        source = """CREATE PROCEDURE p(IN n INTEGER)\nRESULT (id INTEGER, label VARCHAR(40))\nBEGIN\nDECLARE x INTEGER;\nSELECT n, 'ok';\nEXCEPTION WHEN OTHERS THEN\n  RESIGNAL;\nEND;"""
        document = split_asa_routine(source)
        self.assertEqual(document.render(), source)
        kinds = [section.kind for section in document.sections]
        self.assertIn("return_contract", kinds)
        self.assertIn("exception_handlers", kinds)


if __name__ == "__main__":
    unittest.main()
