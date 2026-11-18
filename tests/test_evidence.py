import copy
import io
import json
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path

import evidence_replay
from tests.fixtures import BusinessCase, record, long, blob, text, collection, container
from labavro.archive.database import ArchiveDatabase, ConflictError
from labavro.archive.reading import read_result, read_evidence
from labavro.archive.export import export_result, export_evidence
from labavro.archive.representation import to_export_value
from labavro.processing.execution import execute, execute_evidenced
from labavro.processing.tasks import get_task
from labavro.schemas.types import UnionValue


def rich_schemas():
    writer = record([
        ('skip_me', 'string'),
        ('code', {'type': 'fixed', 'name': 'LabCode', 'size': 2}),
        ('blob', 'bytes'),
        ('reading', 'int'),
        ('ratio', 'float'),
        ('choice', ['int', 'string']),
        ('samples', {'type': 'array', 'items': 'long'}),
        ('metrics', {'type': 'map', 'values': 'double'}),
        ('grade', {'type': 'enum', 'name': 'Grade', 'symbols': ['OK', 'NEW']}),
        ('spike', 'double'),
        ('nested', record([('keep', 'int'), ('drop', 'string')], 'Inner'))], 'Rich')
    reader = record([
        ('code', {'type': 'fixed', 'name': 'Code', 'size': 2, 'aliases': ['LabCode']}),
        ('blob', 'bytes'),
        ('reading', 'long'),
        ('ratio', 'double'),
        ('choice', ['string', 'int']),
        ('samples', {'type': 'array', 'items': 'long'}),
        ('metrics', {'type': 'map', 'values': 'double'}),
        ('grade', {'type': 'enum', 'name': 'Grade', 'symbols': ['OK', 'OTHER'], 'default': 'OTHER'}),
        ('spike', 'double'),
        ('nested', record([('keep', 'long'), ('extra', 'string')], 'Inner')),
        ('note', 'string'),
        ('tag', ['int', 'string'])], 'Rich')
    reader['fields'][9]['type']['fields'][1]['default'] = 'n/a'
    reader['fields'][10]['default'] = 'checked'
    reader['fields'][11]['default'] = 7
    return writer, reader


def rich_payload(spike):
    parts = [('skip_me', text('DISCARDED_SECRET')),
             ('code', b'\x00\xff'),
             ('blob', blob(b'\x01\x02')),
             ('reading', long(123456)),
             ('ratio', struct.pack('<f', 0.1)),
             ('choice', long(0) + long(42)),
             ('samples', collection([(False, [long(1), long(2)]), (True, [long(3)])])),
             ('metrics', collection([(True, [('z', struct.pack('<d', 2.5)),
                                             ('a', struct.pack('<d', -0.0))])], mapping=True)),
             ('grade', long(1)),
             ('spike', struct.pack('<d', spike)),
             ('nested', long(7) + text('INNER_SECRET'))]
    offsets = {}
    position = 0
    for name, data in parts:
        offsets[name] = (position, position + len(data))
        position += len(data)
    return b''.join(data for _, data in parts), offsets


class EvidenceConsistencyTests(BusinessCase):
    def test_evidenced_matches_plain_execute(self):
        writer, reader = rich_schemas()
        payload_one, offsets = rich_payload(float('nan'))
        payload_two, _ = rich_payload(-float('inf'))
        raw = container(writer, [payload_one, payload_two])
        task = self.bind(writer, reader, [], raw=raw)
        receipt = execute_evidenced(self.db, task)
        evidenced = read_result(self.db, receipt.result_id)
        other = ArchiveDatabase()
        try:
            from labavro.sources.packages import register_package
            from labavro.schemas.catalog import register_reader
            from labavro.processing.tasks import create_task
            register_package(other, 'p1', raw)
            register_reader(other, 'r1', reader)
            create_task(other, 't1', 'p1', 'r1')
            plain_receipt = execute(other, 't1')
            plain = read_result(other, plain_receipt.result_id)
            plain_export = io.StringIO()
            export_result(other, plain_receipt.result_id, plain_export)
        finally:
            other.close()
        # Rows and the original JSONL export are identical to plain execute.
        normalize = lambda rows: json.dumps([to_export_value(row['value']) for row in rows],
                                            ensure_ascii=True, separators=(',', ':'), allow_nan=False)
        self.assertEqual(normalize(evidenced['rows']), normalize(plain['rows']))
        evidenced_export = io.StringIO()
        self.assertEqual(export_result(self.db, receipt.result_id, evidenced_export), 2)
        self.assertEqual(evidenced_export.getvalue(), plain_export.getvalue())
        self.assertIn('"type":"float64","value":"NaN"', evidenced_export.getvalue())
        self.assertIn('"value":"-Infinity"', evidenced_export.getvalue())
        self.assertEqual(evidenced['rows'][0]['value']['choice'], UnionValue(1, 'int', 42))
        self.assertEqual(evidenced['rows'][0]['value']['nested'], {'keep': 7, 'extra': 'n/a'})
        self.assertEqual(evidenced['rows'][0]['value']['tag'], UnionValue(0, 'int', 7))
        # Independent replay reconstructs the same rows from the document alone.
        document = read_evidence(self.db, receipt.result_id)
        wanted = [dict(source=row['source'], record=to_export_value(row['value']))
                  for row in evidenced['rows']]
        self.assertEqual(evidence_replay.replay(copy.deepcopy(document)), wanted)
        # Discarded writer payloads never leave the archive inside evidence.
        serialized = json.dumps(document, ensure_ascii=True)
        for secret in ('DISCARDED_SECRET', 'INNER_SECRET'):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret.encode().hex(), serialized)
        entries = {json.dumps(entry['path']): entry for entry in document['rows'][0]['entries']}

        def path(*segments):
            return json.dumps([list(segment) for segment in segments])

        code = entries[path(['field', 'code'])]
        self.assertEqual((code['reader_type'], code['writer_type']), ('Code', 'LabCode'))
        self.assertEqual(code['span'], list(offsets['code']))
        self.assertEqual(code['raw_hex'], '00ff')
        blob_entry = entries[path(['field', 'blob'])]
        self.assertEqual(blob_entry['span'], list(offsets['blob']))
        self.assertEqual(blob_entry['raw_hex'], blob(b'\x01\x02').hex())
        ratio = entries[path(['field', 'ratio'])]
        self.assertEqual((ratio['reader_type'], ratio['writer_type']), ('double', 'float'))
        self.assertEqual(ratio['raw_hex'], struct.pack('<f', 0.1).hex())
        reading = entries[path(['field', 'reading'])]
        self.assertEqual((reading['reader_type'], reading['writer_type']), ('long', 'int'))
        choice = entries[path(['field', 'choice'], ['branch', 1])]
        self.assertEqual(choice['writer_path'], [['field', 'choice'], ['branch', 0]])
        self.assertEqual(choice['reader_type'], 'int')
        grade = entries[path(['field', 'grade'])]
        self.assertEqual((grade['origin'], grade['raw_hex']), ('wire', long(1).hex()))
        tag = entries[path(['field', 'tag'], ['branch', 0])]
        self.assertEqual(tag['origin'], 'default')
        self.assertIsNone(tag['span'])
        note = entries[path(['field', 'note'])]
        self.assertEqual((note['origin'], note['reader_type']), ('default', 'string'))
        extra = entries[path(['field', 'nested'], ['field', 'extra'])]
        self.assertEqual(extra['origin'], 'default')
        keep = entries[path(['field', 'nested'], ['field', 'keep'])]
        self.assertEqual(keep['span'], [offsets['nested'][0], offsets['nested'][0] + 1])
        # Proof keeps collection block structure and writer-side map order.
        proof_fields = dict(document['rows'][0]['proof']['fields'])
        self.assertEqual(proof_fields['samples']['blocks'], [{'count': 2}, {'count': 1, 'size': 1}])
        metrics = proof_fields['metrics']
        self.assertEqual(metrics['blocks'], [{'count': 2, 'size': 20}])
        self.assertEqual([key for key, _ in metrics['entries']], ['z', 'a'])
        self.assertEqual(list(evidenced['rows'][0]['value']['metrics']), ['a', 'z'])
        # Reopening the database keeps the persisted document identical.
        output_one = io.StringIO()
        export_evidence(self.db, receipt.result_id, output_one)
        output_two = io.StringIO()
        export_evidence(self.db, receipt.result_id, output_two)
        self.assertEqual(output_one.getvalue(), output_two.getvalue())
        self.assertEqual(json.loads(output_one.getvalue()), document)


class EvidenceBoundaryTests(BusinessCase):
    def test_evidence_insert_failure_rolls_back(self):
        task = self.bind('int', 'long', [long(1), long(2)])
        self.db.connection.execute(
            "CREATE TRIGGER fail_evidence BEFORE INSERT ON evidence "
            "BEGIN SELECT RAISE(ABORT,'evidence failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            execute_evidenced(self.db, task)
        self.assertEqual(get_task(self.db, task).state, 'pending')
        self.assert_unpublished(task)
        self.assertEqual(self.db.connection.execute('SELECT count(*) FROM evidence').fetchone()[0], 0)
        self.db.connection.execute('DROP TRIGGER fail_evidence')
        receipt = execute_evidenced(self.db, task)
        self.assertEqual(receipt.row_count, 2)
        self.assertEqual(execute_evidenced(self.db, task), receipt)
        self.assertEqual(len(read_evidence(self.db, receipt.result_id)['rows']), 2)

    def test_pre_evidence_archive_migration(self):
        task = self.bind('int', 'long', [long(3)])
        receipt = execute(self.db, task)
        self.db.connection.execute('DROP TABLE evidence')
        self.db.initialize()
        self.assertEqual(read_result(self.db, receipt.result_id)['rows'][0]['value'], 3)
        with self.assertRaises(ConflictError):
            read_evidence(self.db, receipt.result_id)
        with self.assertRaises(ConflictError):
            export_evidence(self.db, receipt.result_id, io.StringIO())
        with self.assertRaises(ConflictError):
            execute_evidenced(self.db, task)
        self.assertEqual(execute(self.db, task), receipt)

    def test_unknown_and_pending_results(self):
        with self.assertRaises(KeyError):
            read_evidence(self.db, 'result:missing')
        with self.assertRaises(KeyError):
            export_evidence(self.db, 'result:missing', io.StringIO())
        task = self.bind('int', 'int', [long(1)])
        with self.assertRaises(KeyError):
            read_evidence(self.db, 'result:' + task)
        with self.assertRaises(KeyError):
            export_evidence(self.db, 'result:' + task, io.StringIO())


class EvidenceReplayTests(BusinessCase):
    def make_document(self):
        writer = record([('u', ['int', 'string']), ('x', 'int')])
        reader = record([('u', ['string', 'int']), ('x', 'long'), ('note', 'string')])
        reader['fields'][2]['default'] = 'ok'
        task = self.bind(writer, reader, [long(0) + long(5) + long(9)])
        receipt = execute_evidenced(self.db, task)
        return read_evidence(self.db, receipt.result_id)

    def test_replay_rejects_tampered_documents(self):
        document = self.make_document()
        wanted = evidence_replay.replay(copy.deepcopy(document))
        self.assertEqual(len(wanted), 1)
        self.assertEqual(wanted[0]['record'],
                         {'u': {'branch_index': 1, 'branch_type': 'int', 'value': 5},
                          'x': 9, 'note': 'ok'})
        proof_fields = dict(document['rows'][0]['proof']['fields'])

        def tampered():
            return copy.deepcopy(document)

        variants = []
        bad = tampered()
        bad['rows'][0]['proof']['fields'][0][1]['child']['child']['raw'] = '0e'
        variants.append(bad)
        bad = tampered()
        bad['rows'][0]['entries'][0]['raw_hex'] = '00'
        variants.append(bad)
        bad = tampered()
        del bad['rows'][0]['entries'][0]
        variants.append(bad)
        bad = tampered()
        bad['rows'][0]['entries'].append(bad['rows'][0]['entries'][0])
        variants.append(bad)
        bad = tampered()
        bad['rows'][0]['proof']['fields'][0][1]['child']['branch'] = 0
        variants.append(bad)
        bad = tampered()
        bad['rows'][0]['proof']['fields'][2][1]['literal'] = 'tampered'
        variants.append(bad)
        bad = tampered()
        bad['binding']['writer_schema_digest'] = '00' * 32
        variants.append(bad)
        bad = tampered()
        bad['receipt']['row_count'] = 2
        variants.append(bad)
        bad = tampered()
        bad['rows'][0]['entries'][1]['span'] = [0, 99]
        variants.append(bad)
        bad = tampered()
        bad['rows'][0]['source']['ordinal'] = 7
        variants.append(bad)
        bad = tampered()
        bad['rows'][0]['proof']['kind'] = 'map'
        variants.append(bad)
        variants.append({'rows': []})
        variants.append('not a document')
        for index, variant in enumerate(variants):
            with self.subTest(variant=index), self.assertRaises(ValueError):
                evidence_replay.replay(variant)
        # The untouched document still replays after all mutations.
        self.assertEqual(evidence_replay.replay(document), wanted)

    def test_replay_cli_detached(self):
        document = self.make_document()
        output = io.StringIO()
        result_id = document['result_id']
        export_evidence(self.db, result_id, output)
        with tempfile.TemporaryDirectory(prefix='labavro-replay-') as directory:
            replay_file = Path(directory) / 'evidence_replay.py'
            replay_file.write_bytes((Path(__file__).resolve().parents[1] / 'evidence_replay.py').read_bytes())
            input_file = Path(directory) / 'evidence.json'
            input_file.write_text(output.getvalue(), encoding='utf-8')
            import subprocess
            import sys
            done = subprocess.run([sys.executable, '-I', '-B', str(replay_file), str(input_file)],
                                  cwd=directory, text=True, capture_output=True, check=True)
            self.assertEqual(json.loads(done.stdout), evidence_replay.replay(document))
            input_file.write_text('{corrupted', encoding='utf-8')
            failed = subprocess.run([sys.executable, '-I', '-B', str(replay_file), str(input_file)],
                                    cwd=directory, text=True, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)


if __name__ == '__main__':
    unittest.main()
