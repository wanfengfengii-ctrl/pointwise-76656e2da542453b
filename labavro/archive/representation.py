import json
import math
import struct
from labavro.schemas.types import UnionValue

def _encode(value):
    if value is None:
        return ['null']
    if type(value) is bool:
        return ['boolean', value]
    if type(value) is int:
        return ['integer', str(value)]
    if type(value) is float:
        return ['float64', struct.pack('>d', value).hex()]
    if isinstance(value, str):
        return ['string', value]
    if isinstance(value, bytes):
        return ['bytes', value.hex()]
    if isinstance(value, UnionValue):
        return ['union', value.branch_index, value.branch_type, _encode(value.value)]
    if isinstance(value, list):
        return ['array', [_encode(v) for v in value]]
    if isinstance(value, dict):
        return ['object', [[k, _encode(v)] for k, v in value.items()]]
    raise TypeError('unsupported archive value')

def _decode(value):
    tag = value[0]
    if tag == 'null':
        return None
    if tag in ('boolean', 'string'):
        return value[1]
    if tag == 'integer':
        return int(value[1])
    if tag == 'float64':
        return struct.unpack('>d', bytes.fromhex(value[1]))[0]
    if tag == 'bytes':
        return bytes.fromhex(value[1])
    if tag == 'union':
        return UnionValue(value[1], value[2], _decode(value[3]))
    if tag == 'array':
        return [_decode(v) for v in value[1]]
    if tag == 'object':
        return {k: _decode(v) for k, v in value[1]}
    raise ValueError('unknown stored value tag')

def encode_row(value):
    return json.dumps(_encode(value), ensure_ascii=True, separators=(',', ':'), allow_nan=False)

def decode_row(text):
    return _decode(json.loads(text))

def to_export_value(value):
    if isinstance(value, UnionValue):
        return {'branch_index': value.branch_index, 'branch_type': value.branch_type,
                'value': to_export_value(value.value)}
    if isinstance(value, bytes):
        return {'type': 'bytes', 'hex': value.hex()}
    if type(value) is float and not math.isfinite(value):
        label = 'NaN' if math.isnan(value) else ('Infinity' if value > 0 else '-Infinity')
        return {'type': 'float64', 'value': label}
    if isinstance(value, dict):
        return {k: to_export_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_export_value(v) for v in value]
    return value
