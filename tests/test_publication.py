import sqlite3
from tests.fixtures import BusinessCase, long
from labavro.processing.execution import execute
from labavro.processing.tasks import get_task
from labavro.archive.reading import read_result
from labavro.schemas.resolution import ResolutionError
from labavro.decoding.cursor import DecodeError

class PublicationTests(BusinessCase):
    def test_rollback_and_retry(self):
        existing = self.bind('int', 'long', [long(3)])
        before = read_result(self.db, execute(self.db, existing).result_id)
        task = self.bind('int', 'long', [long(5), long(7)])
        self.db.connection.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON receipts BEGIN SELECT RAISE(ABORT,'receipt failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            execute(self.db, task)
        self.assertEqual(get_task(self.db, task).state, 'pending')
        self.assert_unpublished(task)
        self.assertEqual(read_result(self.db, before['result_id']), before)
        self.db.connection.execute('DROP TRIGGER reject_receipt')
        receipt = execute(self.db, task)
        self.assertEqual(receipt.row_count, 2)
        self.assertEqual([r['value'] for r in read_result(self.db, receipt.result_id)['rows']], [5, 7])
        self.assertEqual(execute(self.db, task), receipt)
        self.assertEqual(self.db.connection.execute('SELECT count(*) FROM receipts WHERE task_id=?', (task,)).fetchone()[0], 1)

    def test_state_callback_failure_and_replay(self):
        task = self.bind('int', 'int', [long(13)])
        self.db.connection.execute("CREATE TRIGGER reject_completion BEFORE UPDATE OF state ON tasks WHEN NEW.state='published' BEGIN SELECT RAISE(ABORT,'completion failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            execute(self.db, task)
        self.assert_unpublished(task)
        self.assertEqual(get_task(self.db, task).state, 'pending')
        self.db.connection.execute('DROP TRIGGER reject_completion')
        receipt = execute(self.db, task)
        def deny_inputs(action, table, *rest):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table in ('packages', 'readers') else sqlite3.SQLITE_OK
        self.db.connection.set_authorizer(deny_inputs)
        try:
            self.assertEqual(execute(self.db, task), receipt)
            self.assertEqual(read_result(self.db, receipt.result_id)['rows'][0]['value'], 13)
        finally:
            self.db.connection.set_authorizer(None)

    def test_rejected_replay_and_empty_batch(self):
        for writer, reader, payload, error in [('string', 'long', b'\x02x', ResolutionError),
                                               ('int', 'int', b'', DecodeError)]:
            task = self.bind(writer, reader, [payload])
            messages = []
            for _ in range(2):
                with self.assertRaises(error) as caught:
                    execute(self.db, task)
                messages.append(str(caught.exception))
            self.assertEqual(messages[0], messages[1])
            self.assertEqual(get_task(self.db, task).state, 'rejected')
            self.assert_unpublished(task)
        task = self.bind('int', 'long', [])
        receipt = execute(self.db, task)
        self.assertEqual(receipt.row_count, 0)
        self.assertEqual(read_result(self.db, receipt.result_id)['rows'], [])
