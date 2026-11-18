import json
from dataclasses import dataclass
from labavro.archive.database import ConflictError
from labavro.sources.catalog import get_package
from labavro.schemas.catalog import get_reader

@dataclass(frozen=True)
class FrozenTask:
    id: str
    binding: dict
    state: str
    error_kind: object
    error_message: object
    result_id: object

def get_task(db, task_id):
    row = db.connection.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        raise KeyError(task_id)
    return FrozenTask(row['id'], json.loads(row['binding']), row['state'],
                      row['error_kind'], row['error_message'], row['result_id'])

def create_task(db, task_id, package_id, revision_id):
    if not isinstance(task_id, str) or not task_id:
        raise ValueError('empty task id')
    package = get_package(db, package_id)
    reader = get_reader(db, revision_id)
    binding = {'package_id': package_id, 'package_digest': package.digest,
               'writer_schema_digest': package.schema_digest,
               'reader_id': revision_id, 'reader_digest': reader.digest}
    text = json.dumps(binding, sort_keys=True, separators=(',', ':'))
    with db.transaction() as c:
        row = c.execute('SELECT binding FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is not None:
            if row[0] != text:
                raise ConflictError('task binding is immutable')
        else:
            c.execute("INSERT INTO tasks(id,binding,state) VALUES(?,?,'pending')", (task_id, text))
    return get_task(db, task_id)
