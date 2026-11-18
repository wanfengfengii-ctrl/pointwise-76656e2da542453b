import struct
import math
from labavro.decoding.cursor import DecodeError

def read_long(cursor):
    value = 0
    for index in range(10):
        byte = cursor.read_exact(1)[0]
        if index == 9 and byte > 1:
            raise DecodeError('long exceeds 64 bits')
        value |= (byte & 127) << (7 * index)
        if not byte & 128:
            return (value >> 1) ^ -(value & 1)
    raise DecodeError('unterminated long')

def read_primitive(cursor, kind):
    if kind == 'null':
        return None
    if kind == 'boolean':
        value = cursor.read_exact(1)[0]
        if value > 1:
            raise DecodeError('invalid boolean')
        return bool(value)
    if kind in ('int', 'long'):
        value = read_long(cursor)
        if kind == 'int' and not -(1 << 31) <= value < (1 << 31):
            raise DecodeError('int exceeds 32 bits')
        return value
    if kind in ('float', 'double'):
        return struct.unpack('<f' if kind == 'float' else '<d',
                             cursor.read_exact(4 if kind == 'float' else 8))[0]
    if kind in ('bytes', 'string'):
        value = cursor.read_exact(read_long(cursor))
        if kind == 'bytes':
            return value
        try:
            return value.decode('utf-8', errors='strict')
        except UnicodeDecodeError as exc:
            raise DecodeError('invalid UTF-8') from exc
    raise DecodeError('not a primitive: ' + kind)

def skip_primitive(cursor, kind):
    read_primitive(cursor, kind)

def promote_number(value, target):
    if target == 'float':
        if type(value) is int and value:
            sign = -1 if value < 0 else 1
            magnitude = abs(value)
            shift = max(0, magnitude.bit_length() - 24)
            if shift:
                mantissa, remainder = divmod(magnitude, 1 << shift)
                half = 1 << (shift - 1)
                if remainder > half or (remainder == half and mantissa & 1):
                    mantissa += 1
                value = math.ldexp(float(sign * mantissa), shift)
        return struct.unpack('<f', struct.pack('<f', value))[0]
    if target == 'double':
        return float(value)
    return value
