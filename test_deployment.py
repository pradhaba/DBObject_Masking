import types
import unittest
from unittest.mock import patch

from deployment import deploy_postgresql


class DeploymentTests(unittest.TestCase):
    def test_approved_deployment_executes_and_commits(self):
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql):
                connection.executed = sql

        class Connection:
            def __init__(self):
                self.executed = None
                self.committed = False
                self.closed = False

            def cursor(self):
                return Cursor()

            def commit(self):
                self.committed = True

            def rollback(self):
                raise AssertionError('successful deployment must not roll back')

            def close(self):
                self.closed = True

        connection = Connection()
        psycopg = types.SimpleNamespace(connect=lambda **_kwargs: connection)
        project = types.SimpleNamespace(
            id='p1', target_host='localhost', target_port=5432,
            target_database_name='target', target_username='user',
        )
        with patch.dict('sys.modules', {'psycopg': psycopg}), patch(
            'deployment.record_deployment_attempt', return_value=17
        ) as record:
            result = deploy_postgresql(project, 'CREATE TABLE tested(id int);', 3, 5, 'secret')

        self.assertTrue(result['deployed'])
        self.assertEqual(connection.executed, 'CREATE TABLE tested(id int);')
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)
        record.assert_called_once_with(3, 'p1', 5, 'CREATE TABLE tested(id int);', 'deployed')


if __name__ == '__main__':
    unittest.main()
