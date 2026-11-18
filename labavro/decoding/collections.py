from labavro.decoding.cursor import DecodeError
from labavro.decoding.primitives import read_long, read_primitive

def read_collection(cursor, kind, consume, keep=True):
    result = {} if kind == 'map' else []
    seen = set()
    while True:
        count = read_long(cursor)
        if count == 0:
            break
        end = None
        if count < 0:
            count = -count
            size = read_long(cursor)
            if size < 0 or size > cursor.remaining:
                raise DecodeError('invalid collection block size')
            end = cursor.tell() + size
        for _ in range(count):
            key = read_primitive(cursor, 'string') if kind == 'map' else None
            if kind == 'map':
                if key in seen:
                    raise DecodeError('duplicate map key')
                seen.add(key)
            value = consume(cursor)
            if end is not None and cursor.tell() > end:
                raise DecodeError('collection crosses declared block boundary')
            if keep:
                if kind == 'map':
                    result[key] = value
                else:
                    result.append(value)
        if end is not None and cursor.tell() != end:
            raise DecodeError('collection block byte count mismatch')
    if kind == 'map' and keep:
        return {key: result[key] for key in sorted(result)}
    return result

def skip_collection(cursor, kind, consume):
    read_collection(cursor, kind, consume, keep=False)
