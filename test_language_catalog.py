import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from database import _migrate_legacy_database, connect, get_language_catalog_elements
from language_catalog import (
    apply_language_catalog, canonical_catalog_hash, catalog_coverage,
)


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

    def test_canonical_hash_ignores_formatting_bom_and_line_endings(self):
        payload = {"target": "postgresql", "elements": [{"code": "x"}]}
        lf = json.dumps(payload, indent=2).encode("utf-8") + b"\n"
        crlf = b"\xef\xbb\xbf" + lf.replace(b"\n", b"\r\n")
        self.assertEqual(
            canonical_catalog_hash(json.loads(lf.decode("utf-8"))),
            canonical_catalog_hash(json.loads(crlf.decode("utf-8-sig"))),
        )

    def test_legacy_line_ending_hash_is_upgraded_automatically(self):
        with closing(connect(self.database_path)) as db, db:
            catalog = Path(__file__).parent / "skills" / "catalogs" / "sybase_asa_to_postgresql.v1.json"
            lf = catalog.read_bytes().replace(b"\r\n", b"\n")
            legacy_hash = hashlib.sha256(lf).hexdigest()
            db.execute("UPDATE language_catalog_releases SET content_hash=? WHERE catalog_version=1", (legacy_hash,))
            db.commit()
        with closing(connect(self.database_path)) as db:
            stored = db.execute("SELECT content_hash FROM language_catalog_releases WHERE catalog_version=1").fetchone()[0]
            payload = json.loads(catalog.read_text(encoding="utf-8-sig"))
            self.assertEqual(stored, canonical_catalog_hash(payload))

    def test_unknown_legacy_hash_is_repaired_when_stored_rows_match(self):
        with closing(connect(self.database_path)) as db, db:
            db.execute(
                "UPDATE language_catalog_releases SET content_hash='legacy-unknown' WHERE catalog_version=1"
            )
        with closing(connect(self.database_path)) as db:
            stored = db.execute(
                "SELECT content_hash FROM language_catalog_releases WHERE catalog_version=1"
            ).fetchone()[0]
        catalog = Path(__file__).parent / "skills" / "catalogs" / "sybase_asa_to_postgresql.v1.json"
        payload = json.loads(catalog.read_text(encoding="utf-8-sig"))
        self.assertEqual(stored, canonical_catalog_hash(payload))

    def test_real_same_version_change_is_rejected(self):
        get_language_catalog_elements("sybase_asa", "postgresql", self.database_path)
        catalog_dir = Path(self.temp.name) / "changed_catalog"
        catalog_dir.mkdir()
        source = Path(__file__).parent / "skills" / "catalogs" / "sybase_asa_to_postgresql.v1.json"
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
        payload["elements"][0]["target_template"] = "CHANGED_WITHOUT_VERSION"
        (catalog_dir / source.name).write_text(json.dumps(payload), encoding="utf-8")
        with patch("language_catalog.CATALOG_DIR", catalog_dir):
            with self.assertRaisesRegex(ValueError, "immutable SQLite release"):
                connect(self.database_path)

    def test_legacy_database_is_copied_backed_up_and_migrated_once(self):
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        target = Path(self.temp.name) / "local" / "ddl_masker.sqlite3"
        with closing(sqlite3.connect(legacy)) as db, db:
            db.execute("CREATE TABLE marker(value TEXT)")
            db.execute("INSERT INTO marker VALUES ('preserved')")
        self.assertTrue(_migrate_legacy_database(target, legacy))
        self.assertFalse(_migrate_legacy_database(target, legacy))
        backup = target.with_name("ddl_masker.legacy-backup.sqlite3")
        self.assertTrue(backup.exists())
        with closing(sqlite3.connect(target)) as db:
            self.assertEqual(db.execute("SELECT value FROM marker").fetchone()[0], "preserved")


if __name__ == "__main__":
    unittest.main()
