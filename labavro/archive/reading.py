import json
from labavro.archive.representation import decode_row
from labavro.archive.publication import PublicationReceipt

def get_publication_receipt(db, result_id):
    row = db.connection.execute('SELECT r.* FROM receipts r JOIN tasks t ON r.task_id=t.id WHERE r.batch_id=? AND t.state=? AND t.result_id=r.batch_id',
                                (result_id, 'published')).fetchone()
    if row is None:
        raise KeyError(result_id)
    return PublicationReceipt(row['batch_id'], row['task_id'], row['row_count'])

def read_result(db, result_id):
    receipt = get_publication_receipt(db, result_id)
    batch = db.connection.execute('SELECT binding FROM batches WHERE id=?', (result_id,)).fetchone()
    rows = db.connection.execute('SELECT source,value FROM result_rows WHERE batch_id=? ORDER BY ordinal',
                                 (result_id,)).fetchall()
    if len(rows) != receipt.row_count:
        raise RuntimeError('archive row count corruption')
    return {'result_id': result_id, 'binding': json.loads(batch['binding']),
            'receipt': {'task_id': receipt.task_id, 'row_count': receipt.row_count},
            'rows': [{'source': json.loads(row['source']), 'value': decode_row(row['value'])} for row in rows]}
