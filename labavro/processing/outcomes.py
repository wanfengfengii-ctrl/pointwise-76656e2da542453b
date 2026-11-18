from dataclasses import dataclass
from labavro.schemas.resolution import ResolutionError
from labavro.decoding.cursor import DecodeError

@dataclass(frozen=True)
class Rejection:
    kind: str
    message: str

def record_rejection(db, task_id, error):
    kind = type(error).__name__
    if kind not in ('ResolutionError', 'DecodeError'):
        raise TypeError('not a business rejection')
    with db.transaction() as c:
        cursor = c.execute("UPDATE tasks SET state='rejected',error_kind=?,error_message=? WHERE id=? AND state='pending'",
                           (kind, str(error), task_id))
        if cursor.rowcount != 1:
            raise RuntimeError('task is no longer pending')
    return Rejection(kind, str(error))

def mark_published(connection, task_id, result_id):
    cursor = connection.execute("UPDATE tasks SET state='published',result_id=? WHERE id=? AND state='pending'",
                                (result_id, task_id))
    if cursor.rowcount != 1:
        raise RuntimeError('task is no longer pending')

def raise_rejection(task):
    errors = {'ResolutionError': ResolutionError, 'DecodeError': DecodeError}
    if task.error_kind not in errors:
        raise RuntimeError('invalid stored rejection')
    raise errors[task.error_kind](task.error_message)
