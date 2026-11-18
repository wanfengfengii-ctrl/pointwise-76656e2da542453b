import io
import math
import sqlite3
import struct
import tempfile
from pathlib import Path
from tests.fixtures import BusinessCase, record, long, blob, collection
from labavro.archive.database import ArchiveDatabase
from labavro.archive.reading import read_result
from labavro.archive.export import export_result
from labavro.archive.representation import encode_row, decode_row
from labavro.schemas.types import UnionValue
from labavro.schemas.catalog import register_reader
from labavro.processing.execution import execute

class ExportTests(BusinessCase):
    def test_reopen_fixed_result(self):
        self.db.close()
        with tempfile.TemporaryDirectory(prefix='labavro-') as directory:
            path = str(Path(directory) / 'archive.sqlite')
            self.db = ArchiveDatabase(path)
            schema = record([('choice', ['int', 'long']), ('raw', 'bytes'),
                             ('map', {'type': 'map', 'values': 'double'})])
            payloads = [long(i) + long(5) + blob(b'\x00\xff') +
                        collection([(True, [('z', struct.pack('<d', -0.0)), ('a', struct.pack('<d', 1.25))])], mapping=True)
                        for i in (0, 1)]
            task = self.bind(schema, schema, payloads)
            receipt = execute(self.db, task)
            original = read_result(self.db, receipt.result_id)
            output = io.StringIO()
            export_result(self.db, receipt.result_id, output)
            exported = output.getvalue()
            self.assertIn('"branch_index":1,"branch_type":"long","value":5', exported)
            self.assertIn('"type":"bytes","hex":"00ff"', exported)
            self.assertIn('"map":{"a":1.25,"z":-0.0}', exported)
            changed = read_result(self.db, receipt.result_id)
            changed['rows'][0]['value']['map']['a'] = 999
            changed['rows'][0]['value']['choice'] = None
            changed['rows'][0]['source']['ordinal'] = 999
            changed['binding']['reader_id'] = 'changed'
            register_reader(self.db, 'later', 'string')
            self.db.close()
            self.db = ArchiveDatabase(path)
            self.db.initialize()
            self.assertEqual(read_result(self.db, receipt.result_id), original)
            def deny_inputs(action, table, *rest):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table in ('packages', 'readers') else sqlite3.SQLITE_OK
            self.db.connection.set_authorizer(deny_inputs)
            try:
                self.assertEqual(execute(self.db, task), receipt)
                again = io.StringIO()
                export_result(self.db, receipt.result_id, again)
                self.assertEqual(again.getvalue(), exported)
            finally:
                self.db.connection.set_authorizer(None)

    def test_output_failure_and_typed_values(self):
        schema = record([('reading', 'double'), ('tag', ['int', 'long'])])
        payloads = [struct.pack('<d', value) + long(index % 2) + long(5)
                    for index, value in enumerate((float('nan'), float('inf'), -float('inf'), -0.0))]
        receipt = execute(self.db, self.bind(schema, schema, payloads))
        expected = io.StringIO()
        self.assertEqual(export_result(self.db, receipt.result_id, expected), 4)
        self.assertIn('"type":"float64","value":"NaN"', expected.getvalue())
        self.assertIn('"value":"Infinity"', expected.getvalue())
        self.assertIn('"value":"-Infinity"', expected.getvalue())
        class BrokenSink:
            def __init__(self):
                self.calls = 0
            def write(self, value):
                self.calls += 1
                if self.calls == 2:
                    raise OSError('output device failed')
        with self.assertRaisesRegex(OSError, 'output device failed'):
            export_result(self.db, receipt.result_id, BrokenSink())
        again = io.StringIO()
        export_result(self.db, receipt.result_id, again)
        self.assertEqual(again.getvalue(), expected.getvalue())
        result = read_result(self.db, receipt.result_id)
        self.assertTrue(math.isnan(result['rows'][0]['value']['reading']))
        self.assertEqual(result['rows'][1]['value']['tag'], UnionValue(1, 'long', 5))
        self.assertEqual(math.copysign(1, result['rows'][3]['value']['reading']), -1)
        ordinary = {'union': ['bytes', '00'], 'branch_index': 42,
                    'value': {'type': 'float64', 'value': 'NaN'}}
        self.assertEqual(decode_row(encode_row(ordinary)), ordinary)
        with self.assertRaises(KeyError):
            read_result(self.db, 'unknown')
