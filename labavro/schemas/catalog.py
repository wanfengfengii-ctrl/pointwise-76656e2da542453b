import hashlib
import json
from dataclasses import dataclass
from labavro.schemas.names import build_schema
from labavro.schemas.types import SchemaError

def canonical_schema(schema):
    try:
        return json.dumps(schema, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SchemaError('schema is not finite JSON') from exc

def schema_digest(schema):
    return hashlib.sha256(canonical_schema(schema).encode('utf-8')).hexdigest()

@dataclass(frozen=True)
class ReaderRevision:
    id: str
    schema: object
    digest: str

def get_reader(db, revision_id):
    row = db.connection.execute('SELECT * FROM readers WHERE id=?', (revision_id,)).fetchone()
    if row is None:
        raise KeyError(revision_id)
    return ReaderRevision(row['id'], json.loads(row['schema_json']), row['digest'])

def register_reader(db, revision_id, schema):
    if not isinstance(revision_id, str) or not revision_id:
        raise SchemaError('empty revision id')
    text = canonical_schema(schema)
    frozen = json.loads(text)
    build_schema(frozen)
    digest = schema_digest(frozen)
    with db.transaction() as c:
        row = c.execute('SELECT schema_json FROM readers WHERE id=?', (revision_id,)).fetchone()
        if row is not None:
            if row[0] != text:
                raise SchemaError('reader revision is immutable')
        else:
            c.execute('INSERT INTO readers VALUES(?,?,?)', (revision_id, text, digest))
    return get_reader(db, revision_id)
