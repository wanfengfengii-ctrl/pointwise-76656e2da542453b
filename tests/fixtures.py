import json
import unittest
from labavro.archive.database import ArchiveDatabase
from labavro.sources.packages import register_package
from labavro.schemas.catalog import register_reader
from labavro.processing.tasks import create_task
from labavro.processing.execution import execute
from labavro.archive.reading import read_result

SYNC = bytes(range(16))

def long(value):
    number = value * 2 if value >= 0 else -value * 2 - 1
    result = bytearray()
    while number >= 128:
        result.append((number & 127) | 128)
        number >>= 7
    result.append(number)
    return bytes(result)

def blob(value):
    return long(len(value)) + value

def text(value):
    return blob(value.encode('utf-8'))

def collection(parts, mapping=False):
    result = bytearray()
    for negative, entries in parts:
        body = b''.join(text(key) + value for key, value in entries) if mapping else b''.join(entries)
        if not entries:
            continue
        result += long(-len(entries) if negative else len(entries))
        if negative:
            result += long(len(body))
        result += body
    return bytes(result) + b'\x00'

def container(schema, payloads, counts=None, codec=b'null'):
    metadata = (long(2) + text('avro.schema') +
                blob(json.dumps(schema, separators=(',', ':')).encode()) +
                text('avro.codec') + blob(codec) + b'\x00')
    result = b'Obj\x01' + metadata + SYNC
    counts = [1] * len(payloads) if counts is None else counts
    for count, payload in zip(counts, payloads):
        result += long(count) + blob(payload) + SYNC
    return result

def record(fields, name='Sample', namespace=None, aliases=None):
    result = {'type': 'record', 'name': name,
              'fields': [{'name': n, 'type': t} for n, t in fields]}
    if namespace is not None:
        result['namespace'] = namespace
    if aliases is not None:
        result['aliases'] = aliases
    return result

WRITER = record([('discard', {'type': 'array', 'items': 'int'}),
                 ('reading', ['int', 'long']), ('next', ['null', 'lab.Node'])],
                'Node', 'lab')
READER = {'type': 'record', 'name': 'Record', 'namespace': 'center', 'aliases': ['lab.Node'],
          'fields': [{'name': 'next', 'type': ['null', 'center.Record'], 'default': None},
                     {'name': 'signal', 'aliases': ['reading'], 'type': ['int', 'double']},
                     {'name': 'note', 'type': 'string', 'default': 'checked'}]}
PAYLOAD = bytes.fromhex('03040e1000000a0200020a00020100021200')

class BusinessCase(unittest.TestCase):
    def setUp(self):
        self.db = ArchiveDatabase()
        self.serial = 0

    def tearDown(self):
        self.db.close()

    def bind(self, writer, reader, payloads, counts=None, raw=None):
        self.serial += 1
        ident = str(self.serial)
        register_package(self.db, 'p' + ident, raw if raw is not None else container(writer, payloads, counts))
        register_reader(self.db, 'r' + ident, reader)
        create_task(self.db, 't' + ident, 'p' + ident, 'r' + ident)
        return 't' + ident

    def convert(self, writer, reader, payloads, counts=None):
        task = self.bind(writer, reader, payloads, counts)
        receipt = execute(self.db, task)
        return [r['value'] for r in read_result(self.db, receipt.result_id)['rows']]

    def assert_unpublished(self, task):
        for table in ('batches', 'receipts'):
            self.assertEqual(self.db.connection.execute('SELECT count(*) FROM ' + table + ' WHERE task_id=?', (task,)).fetchone()[0], 0)
        self.assertEqual(self.db.connection.execute('SELECT count(*) FROM result_rows WHERE batch_id=?', ('result:' + task,)).fetchone()[0], 0)
