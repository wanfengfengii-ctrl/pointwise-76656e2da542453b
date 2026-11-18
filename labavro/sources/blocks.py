from dataclasses import dataclass
from labavro.decoding.cursor import ByteCursor, DecodeError
from labavro.decoding.primitives import read_long

@dataclass(frozen=True)
class ContainerBlock:
    index: int
    count: int
    payload: bytes

def iter_blocks(raw, header):
    cursor = ByteCursor(raw, header.data_start)
    index = 0
    while cursor.remaining:
        count = read_long(cursor)
        size = read_long(cursor)
        if count <= 0 or size < 0:
            raise DecodeError('invalid container block count or length')
        payload = cursor.read_exact(size)
        if cursor.read_exact(16) != header.sync:
            raise DecodeError('container sync mismatch')
        yield ContainerBlock(index, count, payload)
        index += 1
