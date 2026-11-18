import argparse
import json
import sys
import unittest
from pathlib import Path
from labavro.archive.database import ArchiveDatabase
from labavro.sources.packages import register_package
from labavro.sources.catalog import get_package
from labavro.sources.blocks import iter_blocks
from labavro.schemas.catalog import register_reader, get_reader
from labavro.schemas.names import build_schema
from labavro.schemas.resolution import compile_resolution, ResolutionError
from labavro.decoding.cursor import DecodeError
from labavro.decoding.reader import decode_block
from labavro.processing.tasks import create_task, get_task
from labavro.processing.validation import BatchValidator
from labavro.processing.outcomes import record_rejection, mark_published, raise_rejection
from labavro.archive.publication import publish_batch
from labavro.archive.reading import read_result, get_publication_receipt, get_evidence_text
from labavro.archive.export import export_result
from labavro.archive.representation import to_export_value
from labavro.evidence.tracked import EvidenceValidator, decode_block_tracked
from labavro.evidence.document import build_document, serialize_document, publish_evidence

def execute(db, task_id):
    task = get_task(db, task_id)
    if task.state == 'published':
        return get_publication_receipt(db, task.result_id)
    if task.state == 'rejected':
        raise_rejection(task)
    package = get_package(db, task.binding['package_id'])
    reader = get_reader(db, task.binding['reader_id'])
    if (package.digest != task.binding['package_digest'] or
        package.schema_digest != task.binding['writer_schema_digest'] or
        reader.digest != task.binding['reader_digest']):
        raise RuntimeError('frozen input corruption')
    try:
        plan = compile_resolution(build_schema(package.writer_schema), build_schema(reader.schema))
        validator = BatchValidator(task.binding)
        for block in iter_blocks(package.raw, package.header):
            validator.begin_block(block)
            for record in decode_block(block, plan):
                validator.add_record(record)
        candidate = validator.finish()
    except (ResolutionError, DecodeError) as exc:
        record_rejection(db, task_id, exc)
        raise
    return publish_batch(db, task_id, candidate, mark_published)

def execute_evidenced(db, task_id):
    task = get_task(db, task_id)
    if task.state == 'published':
        return get_evidence_text(db, task.result_id)[0]
    if task.state == 'rejected':
        raise_rejection(task)
    package = get_package(db, task.binding['package_id'])
    reader = get_reader(db, task.binding['reader_id'])
    if (package.digest != task.binding['package_digest'] or
        package.schema_digest != task.binding['writer_schema_digest'] or
        reader.digest != task.binding['reader_digest']):
        raise RuntimeError('frozen input corruption')
    try:
        plan = compile_resolution(build_schema(package.writer_schema), build_schema(reader.schema))
        validator = EvidenceValidator(task.binding)
        for block in iter_blocks(package.raw, package.header):
            validator.begin_block(block)
            for record in decode_block_tracked(block, plan):
                validator.add_record(record)
        candidate = validator.finish()
    except (ResolutionError, DecodeError) as exc:
        record_rejection(db, task_id, exc)
        raise
    document = build_document(task_id, candidate, package.writer_schema, reader.schema)
    return publish_evidence(db, task_id, candidate, serialize_document(document), mark_published)

def main(argv=None):
    parser = argparse.ArgumentParser(description='Northbank laboratory exchange')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('verify')
    demo = sub.add_parser('demo')
    demo.add_argument('--db', default=':memory:')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    if args.command == 'verify':
        suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    sample = json.loads((root / 'samples' / 'day.json').read_text(encoding='utf-8'))
    db = ArchiveDatabase(args.db)
    try:
        register_package(db, 'north-day', bytes.fromhex(sample['container_hex']))
        register_reader(db, 'center-r1', sample['reader_r1'])
        create_task(db, 'day-r1', 'north-day', 'center-r1')
        register_reader(db, 'center-r2', sample['reader_r2'])
        receipt = execute(db, 'day-r1')
        actual = [to_export_value(row['value']) for row in read_result(db, receipt.result_id)['rows']]
        if actual != sample['expected_r1']:
            raise AssertionError({'expected': sample['expected_r1'], 'actual': actual})
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'V3.writer_layout', 'input': sample['payload_hex'],
            'expected': sample['expected_r1'], 'actual': actual,
            'downstream_expected': 2, 'downstream_actual': receipt.row_count,
            'code_paths': ['labavro/schemas/resolution.py', 'labavro/decoding/records.py',
                           'labavro/archive/publication.py'],
            'oracle': 'hand zigzag bytes; reader aliases and first-compatible union rules'}))
        export_result(db, receipt.result_id, sys.stdout)
    finally:
        db.close()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
