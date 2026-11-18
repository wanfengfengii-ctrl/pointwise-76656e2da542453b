import hashlib
from labavro.sources.catalog import store_package
from labavro.sources.container import read_header, PackageError
from labavro.schemas.names import build_schema
from labavro.schemas.types import SchemaError
from labavro.schemas.catalog import canonical_schema, schema_digest

def package_digest(raw):
    return hashlib.sha256(raw).hexdigest()

def register_package(db, package_id, raw):
    if not isinstance(package_id, str) or not package_id:
        raise PackageError('empty package id')
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise PackageError('package must be bytes')
    sealed = bytes(raw)
    header = read_header(sealed)
    try:
        build_schema(header.writer_schema)
        text = canonical_schema(header.writer_schema)
        schema_hash = schema_digest(header.writer_schema)
    except SchemaError as exc:
        raise PackageError('invalid writer schema: ' + str(exc)) from exc
    return store_package(db, package_id, sealed, package_digest(sealed), text, schema_hash)
