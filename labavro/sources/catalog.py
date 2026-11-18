import json
from dataclasses import dataclass
from labavro.archive.database import ConflictError
from labavro.sources.container import read_header

@dataclass(frozen=True)
class SourcePackage:
    id: str
    raw: bytes
    digest: str
    writer_schema: object
    schema_digest: str
    header: object

def get_package(db, package_id):
    row = db.connection.execute('SELECT * FROM packages WHERE id=?', (package_id,)).fetchone()
    if row is None:
        raise KeyError(package_id)
    raw = bytes(row['raw'])
    return SourcePackage(row['id'], raw, row['digest'], json.loads(row['schema_json']),
                         row['schema_digest'], read_header(raw))

def store_package(db, package_id, raw, digest, schema_text, schema_hash):
    with db.transaction() as c:
        row = c.execute('SELECT raw,digest FROM packages WHERE id=?', (package_id,)).fetchone()
        if row is not None:
            if row['digest'] != digest or bytes(row['raw']) != raw:
                raise ConflictError('package id already seals different bytes')
        else:
            c.execute('INSERT INTO packages VALUES(?,?,?,?,?)',
                      (package_id, raw, digest, schema_text, schema_hash))
    return get_package(db, package_id)
