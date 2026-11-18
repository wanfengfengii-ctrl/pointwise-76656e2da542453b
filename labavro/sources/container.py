import json
from dataclasses import dataclass
from labavro.decoding.cursor import ByteCursor, DecodeError
from labavro.decoding.primitives import read_primitive
from labavro.decoding.collections import read_collection

class PackageError(ValueError):
    pass

@dataclass(frozen=True)
class ContainerHeader:
    writer_schema: object
    sync: bytes
    data_start: int
    metadata: dict

def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result

def read_header(raw):
    try:
        cursor = ByteCursor(raw)
        if cursor.read_exact(4) != b'Obj\x01':
            raise PackageError('invalid object container magic')
        metadata = read_collection(cursor, 'map', lambda c: read_primitive(c, 'bytes'))
        if metadata.get('avro.codec', b'null') != b'null':
            raise PackageError('only null codec is supported')
        if 'avro.schema' not in metadata:
            raise PackageError('missing writer schema')
        schema = json.loads(metadata['avro.schema'].decode('utf-8'),
                            object_pairs_hook=_unique_object,
                            parse_constant=lambda s: (_ for _ in ()).throw(ValueError('nonfinite schema JSON')))
        sync = cursor.read_exact(16)
        return ContainerHeader(schema, sync, cursor.tell(), metadata)
    except (DecodeError, UnicodeError, ValueError, TypeError) as exc:
        if isinstance(exc, PackageError):
            raise
        raise PackageError('invalid container header: ' + str(exc)) from exc
