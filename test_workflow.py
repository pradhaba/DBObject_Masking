import unittest
import tarfile
import zipfile

from workflow import Project, dialect_for, load_projects, safe_extract_sql_archive, save_projects


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        from pathlib import Path
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_projects_round_trip(self):
        project = Project('p1', 'Demo', 'PostgreSQL', 'Oracle', 'localhost', 5432, 'db', 'user', 'now')
        path = self.root / 'projects.sqlite3'
        save_projects([project], path)
        self.assertEqual(load_projects(path), [project])
        import sqlite3
        connection = sqlite3.connect(path)
        try:
            columns = [row[1] for row in connection.execute('PRAGMA table_info(projects)')]
        finally:
            connection.close()
        self.assertNotIn('password', columns)

    def test_extracts_only_supported_files(self):
        archive = self.root / 'objects.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('schema/table.sql', 'create table x(id int);')
            bundle.writestr('notes.md', 'ignore')
        files = safe_extract_sql_archive(archive, self.root / 'out')
        self.assertEqual([p.name for p in files], ['table.sql'])

    def test_rejects_zip_slip(self):
        archive = self.root / 'unsafe.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('../escape.sql', 'bad')
        with self.assertRaisesRegex(ValueError, 'Unsafe path'):
            safe_extract_sql_archive(archive, self.root / 'out')

    def test_target_dialects(self):
        self.assertEqual(dialect_for('PostgreSQL'), 'postgresql')
        self.assertEqual(dialect_for('Oracle'), 'oracle')
        self.assertEqual(dialect_for('SAP ASE'), 'sybase_ase')
        self.assertEqual(dialect_for('SAP ASA'), 'sybase_asa')

    def test_global_integer_variable_rule_is_reviewable(self):
        from database import get_skill_version_rules, list_skill_versions
        path = self.root / 'global-rule.sqlite3'
        candidate = next(v for v in list_skill_versions(path) if v['source_dialect']=='sybase_asa' and v['target_dialect']=='postgresql' and v['status']=='awaiting_approval')
        rule = next(r for r in get_skill_version_rules(candidate['id'], path) if r['rule_code']=='asa-pg-global-int-variable')
        self.assertEqual(rule['review_status'], 'awaiting_approval')
        self.assertEqual(rule['category'], 'parameters_variables')

    def test_schema_qualification_rule_is_reviewable(self):
        from database import get_skill_version_rules, list_skill_versions
        path = self.root / 'schema-rule.sqlite3'
        candidate = next(v for v in list_skill_versions(path) if v['source_dialect']=='sybase_asa' and v['target_dialect']=='postgresql' and v['status']=='awaiting_approval')
        rule = next(r for r in get_skill_version_rules(candidate['id'], path) if r['rule_code']=='asa-pg-schema-qualification')
        self.assertEqual(rule['replacement'], 'dba')
        self.assertEqual(rule['review_status'], 'awaiting_approval')

    def test_extracts_tar_archive(self):
        source = self.root / 'procedure.sql'
        source.write_text('create procedure p as select 1')
        archive = self.root / 'objects.tar.gz'
        with tarfile.open(archive, 'w:gz') as bundle:
            bundle.add(source, arcname='procedures/procedure.sql')
        files = safe_extract_sql_archive(archive, self.root / 'tar-out')
        self.assertEqual([item.name for item in files], ['procedure.sql'])

    def test_mapping_can_be_reused_from_database(self):
        from database import latest_mapping, record_processing
        path = self.root / 'mapping.sqlite3'
        project = Project('p2', 'Mappings', 'SAP ASE', 'PostgreSQL', 'host', 5000, 'db', 'user', 'now')
        save_projects([project], path)
        mapping = {'tables': {'Customer': 'TBL_1'}}
        record_processing(project.id, 'customer.sql', 'mask', 'sybase_ase', 'input', 'output', mapping, path=path)
        self.assertEqual(latest_mapping(project.id, 'customer.sql', path), mapping)

    def test_skill_correction_requires_test_then_creates_new_version(self):
        from database import (
            approve_change_proposal, approve_skill_version, create_change_proposal,
            get_active_skill_version, get_skill_version_rules, list_skill_versions, record_deployment_attempt,
            review_skill_rule,
            update_proposal_rule,
        )
        path = self.root / 'skills.sqlite3'
        candidate = next(v for v in list_skill_versions(path) if v['source_dialect']=='sybase_asa' and v['target_dialect']=='postgresql' and v['status']=='awaiting_approval')
        for rule in get_skill_version_rules(candidate['id'], path):
            review_skill_rule(rule['id'], 'approved', 'initial test approval', path)
        approve_skill_version(candidate['id'], 'initial approver', path)
        skill = get_active_skill_version('sybase_asa', 'postgresql', path)
        attempt = record_deployment_attempt(None, None, skill['id'], 'SELECT BAD_TOKEN;', 'failed', path=path)
        proposal = create_change_proposal(attempt, skill['id'], 'Replace bad token', 'PostgreSQL error', path)
        with self.assertRaises(ValueError):
            approve_change_proposal(proposal, 'reviewer', path)
        update_proposal_rule(proposal, r'BAD_TOKEN', 'CURRENT_TIMESTAMP', 'tester', path)
        new_version = approve_change_proposal(proposal, 'approver', path)
        active = get_active_skill_version('sybase_asa', 'postgresql', path)
        self.assertEqual(active['id'], new_version)
        self.assertGreater(active['version'], candidate['version'])
