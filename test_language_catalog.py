import tempfile
import unittest
from pathlib import Path

from database import connect, get_language_catalog_elements
from language_catalog import apply_language_catalog, catalog_coverage


class LanguageCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "catalog.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_bundled_catalog_is_loaded_by_kind(self):
        elements = get_language_catalog_elements(
            "sybase_asa", "postgresql", self.database_path
        )
        kinds = {entry["element_kind"] for entry in elements}
        self.assertTrue({"datatype", "keyword", "function", "operator", "statement", "structural"} <= kinds)
        self.assertGreater(len(elements), 30)
        self.assertTrue(any(entry["disposition"] == "type_dependent" for entry in elements))
        self.assertTrue(any(entry["element_code"] == "asa-pg-function-list" for entry in elements))

    def test_section_scoped_conversion_protects_literals(self):
        elements = get_language_catalog_elements(
            "sybase_asa", "postgresql", self.database_path
        )
        source = """CREATE FUNCTION dba.f(IN p LONG BINARY) RETURNS UNIQUEIDENTIFIER
        BEGIN
        DECLARE note LONG VARCHAR;
        SET note = 'LONG VARCHAR GETDATE()';
        RETURN ISNULL(GETDATE(), CURRENT TIMESTAMP);
        END;"""
        converted, trace = apply_language_catalog(source, elements)
        self.assertIn("p BYTEA", converted)
        self.assertIn("RETURNS UUID", converted)
        self.assertIn("DECLARE note TEXT", converted)
        self.assertIn("'LONG VARCHAR GETDATE()'", converted)
        self.assertIn("COALESCE(CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)", converted)
        self.assertTrue(trace)

    def test_context_sensitive_construct_is_reported_not_blindly_replaced(self):
        elements = get_language_catalog_elements(
            "sybase_asa", "postgresql", self.database_path
        )
        source = "CREATE PROCEDURE p() BEGIN SELECT * FROM a,b WHERE a.id *= b.id; END;"
        converted, _ = apply_language_catalog(source, elements)
        self.assertIn("*=", converted)
        review = catalog_coverage(converted, elements)
        self.assertEqual(review[0]["code"], "asa-pg-struct-outer-join")
        self.assertEqual(review[0]["handling"], "diagnostic")


if __name__ == "__main__":
    unittest.main()
