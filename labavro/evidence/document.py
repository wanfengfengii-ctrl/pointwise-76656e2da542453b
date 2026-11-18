"""Evidence document assembly and joint publication.

The evidence document is materialized once, at publish time, inside the same
transaction as the rows, the receipt and the task state. Afterwards reads and
exports only touch the persisted document; inputs are never re-parsed and
evidence is never regenerated.
"""
import json

from labavro.archive.publication import PublicationReceipt
from labavro.archive.representation import encode_row


def build_document(task_id, candidate, writer_schema, reader_schema):
    return {
        'result_id': 'result:' + task_id,
        'binding': candidate.binding,
        'receipt': {'task_id': task_id, 'row_count': len(candidate.rows)},
        'writer_schema': writer_schema,
        'reader_schema': reader_schema,
        'rows': [{'source': row['source'], 'entries': row['entries'], 'proof': row['proof']}
                 for row in candidate.rows]}


def serialize_document(document):
    return json.dumps(document, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def publish_evidence(db, task_id, candidate, document_text, complete):
    result_id = 'result:' + task_id
    binding = json.dumps(candidate.binding, sort_keys=True, separators=(',', ':'))
    with db.transaction() as c:
        c.execute('INSERT INTO batches VALUES(?,?,?)', (result_id, task_id, binding))
        for ordinal, row in enumerate(candidate.rows):
            if row['source']['ordinal'] != ordinal:
                raise ValueError('non-contiguous publication order')
            c.execute('INSERT INTO result_rows VALUES(?,?,?,?)',
                      (result_id, ordinal, json.dumps(row['source'], sort_keys=True),
                       encode_row(row['value'])))
        c.execute('INSERT INTO receipts VALUES(?,?,?)', (result_id, task_id, len(candidate.rows)))
        c.execute('INSERT INTO evidence VALUES(?,?)', (result_id, document_text))
        complete(c, task_id, result_id)
    return PublicationReceipt(result_id, task_id, len(candidate.rows))
