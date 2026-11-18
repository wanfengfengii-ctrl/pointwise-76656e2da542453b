from tests.fixtures import BusinessCase, record, long, WRITER, READER, PAYLOAD
from labavro.schemas.types import UnionValue
from labavro.schemas.resolution import ResolutionError, compile_resolution
from labavro.schemas.names import build_schema
from labavro.processing.execution import execute
from labavro.processing.tasks import get_task

class ResolutionTests(BusinessCase):
    def test_recursive_alias_and_defaults(self):
        values = self.convert(WRITER, READER, [PAYLOAD], [2])
        child = {'next': UnionValue(0, 'null', None),
                 'signal': UnionValue(1, 'double', 5.0), 'note': 'checked'}
        expected = [{'next': UnionValue(1, 'center.Record', child),
                     'signal': UnionValue(0, 'int', 5), 'note': 'checked'},
                    {'next': UnionValue(0, 'null', None),
                     'signal': UnionValue(1, 'double', 9.0), 'note': 'checked'}]
        self.assertEqual(values, expected)
        self.assertEqual(list(values[0]), ['next', 'signal', 'note'])
        compact = record([('reading', 'double')], 'Node', 'lab')
        for _ in range(2):
            self.assertEqual(self.convert(WRITER, compact, [PAYLOAD], [2]), [{'reading': 5.0}, {'reading': 9.0}])
            self.assertEqual(self.convert(WRITER, READER, [PAYLOAD], [2]), expected)
        import json
        from labavro.archive.representation import to_export_value
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'V3', 'input': PAYLOAD.hex(), 'expected': to_export_value(expected),
            'actual': to_export_value(values), 'downstream_expected': [5, 9],
            'downstream_actual': [v['signal'].value for v in values],
            'code_paths': ['labavro/schemas/resolution.py', 'labavro/decoding/records.py'],
            'oracle': 'hand-decoded zigzag integers and explicit named union tags'}))

    def test_scopes_aliases_and_isolated_defaults(self):
        nested = record([('a', record([('x', 'int')], 'Thing', 'left')),
                         ('b', record([('x', 'string')], 'Thing', 'right'))])
        self.assertEqual(self.convert(nested, nested, [long(4) + b'\x02z']),
                         [{'a': {'x': 4}, 'b': {'x': 'z'}}])
        writer = record([('old', 'int'), ('value', 'int')])
        reader = record([('value', 'long')])
        reader['fields'][0]['aliases'] = ['old']
        self.assertEqual(self.convert(writer, reader, [long(10) + long(11)]), [{'value': 11}])
        reader = record([('fresh', 'int')])
        reader['fields'][0]['aliases'] = ['old', 'value']
        with self.assertRaises(ResolutionError):
            compile_resolution(build_schema(writer), build_schema(reader))
        empty = record([])
        default = {'type': 'array', 'items': {'type': 'map', 'values': 'int'}}
        with_default = record([('items', default)])
        with_default['fields'][0]['default'] = [{'a': 1}]
        values = self.convert(empty, with_default, [b'', b''])
        values[0]['items'][0]['a'] = 7
        self.assertEqual(values[1], {'items': [{'a': 1}]})
        self.assertEqual(self.convert(empty, with_default, [b'']), [{'items': [{'a': 1}]}])

    def test_missing_required_and_enum(self):
        task = self.bind(record([]), record([('value', 'int')]), [b''])
        with self.assertRaises(ResolutionError):
            execute(self.db, task)
        self.assertEqual(get_task(self.db, task).state, 'rejected')
        self.assert_unpublished(task)
        ew = {'type': 'enum', 'name': 'Grade', 'symbols': ['OK', 'NEW'], 'default': 'OK'}
        er = {'type': 'enum', 'name': 'Grade', 'symbols': ['OK', 'OTHER'], 'default': 'OTHER'}
        self.assertEqual(self.convert(ew, er, [long(0), long(1)]), ['OK', 'OTHER'])
        del er['default']
        task = self.bind(ew, er, [long(0), long(1)])
        with self.assertRaises(ResolutionError):
            execute(self.db, task)
        self.assert_unpublished(task)
        task = self.bind('null', 'int', [b''])
        with self.assertRaises(ResolutionError):
            execute(self.db, task)
        self.assert_unpublished(task)

    def test_explicit_null_and_union_runtime_compatibility(self):
        reader = record([('value', 'int')])
        reader['fields'][0]['default'] = 7
        self.assertEqual(self.convert(record([]), reader, [b'']), [{'value': 7}])
        task = self.bind(record([('value', 'null')]), reader, [b''])
        with self.assertRaises(ResolutionError):
            execute(self.db, task)
        self.assert_unpublished(task)
        nullable = record([('value', ['null', 'int'])])
        nullable['fields'][0]['default'] = None
        self.assertEqual(self.convert(record([('value', 'null')]), nullable, [b'']),
                         [{'value': UnionValue(0, 'null', None)}])
        self.assertEqual(self.convert(['string', 'int'], ['long', 'string'], [long(1) + long(5)]),
                         [UnionValue(0, 'long', 5)])
        self.assertEqual(self.convert(['string', 'int'], 'long', [long(1) + long(5)]), [5])
        task = self.bind(['string', 'int'], 'long', [long(0) + b'\x02x'])
        with self.assertRaises(ResolutionError):
            execute(self.db, task)
        self.assert_unpublished(task)
        reader = record([('choice', ['int', 'string'])])
        reader['fields'][0]['default'] = 12
        self.assertEqual(self.convert(record([]), reader, [b'']),
                         [{'choice': UnionValue(0, 'int', 12)}])

    def test_recursive_pair_contexts(self):
        node = record([('reading', 'int'), ('next', ['null', 'Node'])], 'Node')
        writer = record([('a', node), ('b', 'Node')], 'Envelope')
        left = {'type': 'record', 'name': 'Left', 'aliases': ['Node'], 'fields': [
            {'name': 'value', 'aliases': ['reading'], 'type': 'long'},
            {'name': 'next', 'type': ['null', 'Left']}]}
        right = {'type': 'record', 'name': 'Right', 'aliases': ['Node'], 'fields': [
            {'name': 'next', 'type': ['null', 'Right']},
            {'name': 'reading', 'type': 'double'},
            {'name': 'flag', 'type': 'string', 'default': 'right'}]}
        reader = record([('a', left), ('b', right)], 'Envelope')
        payload = long(2) + long(1) + long(3) + long(0) + long(4) + long(0)
        expected = {'a': {'value': 2, 'next': UnionValue(1, 'Left',
                          {'value': 3, 'next': UnionValue(0, 'null', None)})},
                    'b': {'next': UnionValue(0, 'null', None), 'reading': 4.0, 'flag': 'right'}}
        self.assertEqual(self.convert(writer, reader, [payload]), [expected])
