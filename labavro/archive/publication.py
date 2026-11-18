import json
from dataclasses import dataclass
from labavro.archive.representation import encode_row

@dataclass(frozen=True)
class PublicationReceipt:
    result_id: str
    task_id: str
    row_count: int

def publish_batch(db, task_id, candidate, complete):
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
        complete(c, task_id, result_id)
    return PublicationReceipt(result_id, task_id, len(candidate.rows))
