import math
import struct
from tests.fixtures import BusinessCase, record, long, blob, text, collection, container
from labavro.schemas.types import UnionValue
from labavro.decoding.cursor import DecodeError
from labavro.processing.execution import execute
from labavro.processing.tasks import get_task

class DecodingTests(BusinessCase):
    def test_union_and_block_skip(self):
        skipped = record([('groups', {'type': 'array', 'items': {'type': 'map', 'values': ['null', 'string']}})], 'Discarded')
        writer = record([('discard', skipped), ('reading', ['int', 'long'])])
        reader = record([('signal', ['int', 'double'])])
        reader['fields'][0]['aliases'] = ['reading']
        mapping = collection([(True, [('z', long(1) + text('sensor')), ('a', long(0))])], mapping=True)
        nested = collection([(True, [mapping]), (False, [b'\x00'])])
        payloads = [nested + long(0) + long(5), nested + long(1) + long(5)]
        values = self.convert(writer, reader, payloads)
        self.assertEqual(values, [{'signal': UnionValue(0, 'int', 5)},
                                  {'signal': UnionValue(1, 'double', 5.0)}])
        accepted = self.convert(writer, writer, payloads)
        self.assertEqual(accepted[0]['discard']['groups'][0],
                         {'a': UnionValue(0, 'null', None), 'z': UnionValue(1, 'string', 'sensor')})
        self.assertEqual(list(accepted[0]['discard']['groups'][0]), ['a', 'z'])

    def test_collection_blocks_and_precision(self):
        schema = record([('a', {'type': 'array', 'items': 'int'}),
                         ('m', {'type': 'map', 'values': 'long'}), ('last', 'boolean')])
        array = collection([(False, [long(1)]), (True, [long(2), long(3)]), (False, [long(4)])])
        mapping = collection([(True, [('z', long(9))]), (False, [('a', long(-1))])], mapping=True)
        self.assertEqual(self.convert(schema, schema, [array + mapping + b'\x01']),
                         [{'a': [1, 2, 3, 4], 'm': {'a': -1, 'z': 9}, 'last': True}])
        self.assertEqual(self.convert(schema, schema, [b'\x00\x00\x00']),
                         [{'a': [], 'm': {}, 'last': False}])
        for value in (-(1 << 31), (1 << 31) - 1):
            self.assertEqual(self.convert('int', 'long', [long(value)]), [value])
        for value in (-(1 << 63), (1 << 63) - 1):
            self.assertEqual(self.convert('long', 'long', [long(value)]), [value])
        self.assertEqual(self.convert('int', 'float', [long(16777217)]), [16777216.0])
        self.assertEqual(self.convert('long', 'float', [long((1 << 62) + (1 << 38) + 1)]), [float((1 << 62) + (1 << 39))])
        self.assertEqual(self.convert('long', 'double', [long(9007199254740993)]), [9007199254740992.0])
        self.assertEqual(self.convert('float', 'double', [bytes.fromhex('cdcccc3d')]), [0.10000000149011612])
        self.assertEqual(self.convert('double', 'double', [struct.pack('<d', 1.25)]), [1.25])
        negative = self.convert('float', 'double', [bytes.fromhex('00000080')])[0]
        self.assertEqual(math.copysign(1, negative), -1)

    def test_corrupt_block(self):
        good = self.bind('int', 'int', [long(2)])
        execute(self.db, good)
        schema = record([('skip', {'type': 'array', 'items': 'int'}), ('tail', 'int')])
        retained = record([('tail', 'int')])
        for size in (1, 3):
            payload = long(-2) + long(size) + long(1) + long(2) + b'\x00' + long(8)
            for reader in (schema, retained):
                task = self.bind(schema, reader, [payload])
                with self.subTest(size=size, reader=reader), self.assertRaises(DecodeError):
                    execute(self.db, task)
                self.assert_unpublished(task)
        map_schema = record([('skip', {'type': 'map', 'values': 'int'}), ('tail', 'int')])
        for size in (2, 4):
            payload = long(-1) + long(size) + text('a') + long(3) + b'\x00' + long(8)
            for reader in (map_schema, retained):
                task = self.bind(map_schema, reader, [payload])
                with self.assertRaises(DecodeError):
                    execute(self.db, task)
                self.assert_unpublished(task)
        payloads = [(container('int', [b'']), 'int', 'int'),
                    (container('int', [long(1) + long(2)], [1]), 'int', 'int'),
                    (container('int', [long(1)], [2]), 'int', 'int'),
                    (container(['null', 'int'], [long(2)]), ['null', 'int'], ['null', 'int']),
                    (container('int', [long(1)])[:-1] + b'X', 'int', 'int'),
                    (container('int', [long(1)])[:-10], 'int', 'int')]
        for raw, writer, reader in payloads:
            task = self.bind(writer, reader, [], raw=raw)
            with self.assertRaises(DecodeError):
                execute(self.db, task)
            self.assertEqual(get_task(self.db, task).state, 'rejected')
            self.assert_unpublished(task)
        self.assertEqual(execute(self.db, good).row_count, 1)

    def test_primitive_bounds_and_fixed(self):
        enum = {'type': 'enum', 'name': 'Grade', 'symbols': ['OK']}
        invalid = [('int', long(1 << 31)), ('long', b'\x80' * 9 + b'\x02'),
                   ('boolean', b'\x02'), ('string', b'\x02\xff'),
                   ('bytes', long(-1)), (enum, long(1)), (['null', 'int'], long(-1))]
        for schema, data in invalid:
            task = self.bind(schema, schema, [data])
            with self.subTest(schema=schema), self.assertRaises(DecodeError):
                execute(self.db, task)
            self.assert_unpublished(task)
        fixed = {'type': 'fixed', 'name': 'LabCode', 'size': 2}
        renamed = {'type': 'fixed', 'name': 'Code', 'aliases': ['LabCode'], 'size': 2}
        self.assertEqual(self.convert(fixed, renamed, [b'\x00\xff']), [b'\x00\xff'])
        self.assertEqual(self.convert('bytes', 'bytes', [blob(b'\x00\xfe')]), [b'\x00\xfe'])
        self.assertEqual(self.convert('string', 'string', [text('河口')]), ['河口'])
        mapping = {'type': 'map', 'values': 'int'}
        duplicate = collection([(False, [('a', long(1)), ('a', long(2))])], mapping=True)
        task = self.bind(mapping, mapping, [duplicate])
        with self.assertRaises(DecodeError):
            execute(self.db, task)
        self.assert_unpublished(task)
