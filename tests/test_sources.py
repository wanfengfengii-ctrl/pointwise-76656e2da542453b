import copy
import json
from pathlib import Path
from tests.fixtures import BusinessCase, container, record, long, WRITER, READER, PAYLOAD
from labavro.sources.packages import register_package, package_digest
from labavro.sources.catalog import get_package
from labavro.sources.container import PackageError
from labavro.schemas.catalog import register_reader, get_reader
from labavro.schemas.names import build_schema
from labavro.schemas.types import SchemaError
from labavro.processing.tasks import create_task, get_task
from labavro.processing.execution import execute
from labavro.archive.database import ConflictError
from labavro.archive.reading import read_result

class SourceTests(BusinessCase):
    def test_freeze_and_repeat(self):
        schema = copy.deepcopy(READER)
        raw = bytearray(container(WRITER, [PAYLOAD], [2]))
        original = bytes(raw)
        package = register_package(self.db, 'p', raw)
        revision = register_reader(self.db, 'r1', schema)
        task = create_task(self.db, 't', 'p', 'r1')
        raw[-1] ^= 1
        schema['fields'].clear()
        register_reader(self.db, 'r2', record([('reading', ['long', 'double'])], 'Node', 'lab'))
        self.assertEqual(register_package(self.db, 'p', original).digest, package_digest(original))
        self.assertEqual(register_reader(self.db, 'r1', READER).digest, revision.digest)
        self.assertEqual(create_task(self.db, 't', 'p', 'r1'), task)
        self.assertEqual(get_reader(self.db, 'r1').schema, READER)
        self.assertEqual(get_task(self.db, 't').binding['reader_digest'], revision.digest)
        self.db.initialize()
        result = read_result(self.db, execute(self.db, 't').result_id)
        self.assertEqual(result['rows'][0]['value']['note'], 'checked')
        self.assertEqual(result['binding'], task.binding)
        with self.assertRaises(ConflictError):
            create_task(self.db, 't', 'p', 'r2')
        with self.assertRaises(KeyError):
            create_task(self.db, 'missing', 'unknown', 'r1')
        self.assertEqual(get_task(self.db, 't').state, 'published')

    def test_conflicting_package(self):
        schema = record([('reading', 'int')])
        raw = container(schema, [long(17)])
        package = register_package(self.db, 'p', raw)
        with self.assertRaises(ConflictError):
            register_package(self.db, 'p', container(schema, [long(18)]))
        self.assertEqual(get_package(self.db, 'p').raw, raw)
        self.assertEqual(get_package(self.db, 'p').digest, package.digest)
        register_reader(self.db, 'r', schema)
        create_task(self.db, 't', 'p', 'r')
        self.assertEqual(read_result(self.db, execute(self.db, 't').result_id)['rows'][0]['value'], {'reading': 17})

    def test_header_errors_and_reader_validation(self):
        invalid = [b'wrong', container('int', [], codec=b'deflate'),
                   container('Missing', []), container({'type': 'int', 'logicalType': 'date'}, [])]
        for raw in invalid:
            with self.subTest(raw=raw[:10]), self.assertRaises(PackageError):
                register_package(self.db, 'bad', raw)
        self.assertEqual(self.db.connection.execute('SELECT count(*) FROM packages').fetchone()[0], 0)
        invalid_schemas = [
            'Missing', {'type': 'fixed', 'name': 'F', 'size': 0},
            {'type': 'record', 'name': 'Bad', 'fields': [{'name': 'x', 'type': 'int', 'default': None}]},
            {'type': 'record', 'name': 'Bad', 'fields': [{'name': 'x', 'type': ['int', 'null'], 'default': None}]},
            {'type': 'enum', 'name': 'E', 'symbols': ['A'], 'default': 'B'},
            ['int', 'int'], [['int'], 'null'],
            record([('x', {'type': 'fixed', 'name': 'F', 'size': 1}),
                    ('y', {'type': 'fixed', 'name': 'F', 'size': 2})]),
        ]
        for schema in invalid_schemas:
            with self.subTest(schema=schema), self.assertRaises(SchemaError):
                register_reader(self.db, 'bad', schema)
        self.assertEqual(self.db.connection.execute('SELECT count(*) FROM readers').fetchone()[0], 0)
        register_reader(self.db, 'r', 'int')
        with self.assertRaises(SchemaError):
            register_reader(self.db, 'r', 'long')
        self.assertEqual(get_reader(self.db, 'r').schema, 'int')
        graph = build_schema(record([('next', ['null', 'tree.Node'])], 'Node', 'tree'))
        self.assertIs(graph.root.fields[0].type.branches[1], graph.root)

    def test_sample_and_initialize_repeat(self):
        data = json.loads((Path(__file__).resolve().parents[1] / 'samples' / 'day.json').read_text())
        task = self.bind(data['writer_schema'], data['reader_r1'], [], raw=bytes.fromhex(data['container_hex']))
        from labavro.archive.representation import to_export_value
        actual = [to_export_value(r['value']) for r in read_result(self.db, execute(self.db, task).result_id)['rows']]
        self.assertEqual(actual, data['expected_r1'])
        self.db.initialize()
        self.assertEqual(get_task(self.db, task).state, 'published')
        other = self.bind('int', 'long', [long(29)])
        self.assertEqual(execute(self.db, other).row_count, 1)

    def test_unknown_database_version(self):
        self.db.connection.execute('PRAGMA user_version=99')
        with self.assertRaises(RuntimeError):
            self.db.initialize()
        self.db.connection.execute('PRAGMA user_version=1')
        self.db.initialize()
