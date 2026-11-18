from labavro.schemas.types import PRIMITIVES
from labavro.schemas.defaults import materialize_default
from labavro.schemas.resolution import ResolutionError
from labavro.decoding.cursor import DecodeError
from labavro.decoding.primitives import read_primitive, skip_primitive, read_long, promote_number
from labavro.decoding.collections import read_collection, skip_collection
from labavro.decoding.unions import read_union, skip_union, wrap_reader_union

def skip_value(cursor, writer):
    k = writer.kind
    if k in PRIMITIVES:
        skip_primitive(cursor, k)
    elif k == 'record':
        for field in writer.fields:
            skip_value(cursor, field.type)
    elif k in ('array', 'map'):
        skip_collection(cursor, k, lambda c: skip_value(c, writer.items))
    elif k == 'union':
        skip_union(cursor, writer, skip_value)
    elif k == 'fixed':
        cursor.skip_exact(writer.size)
    elif k == 'enum':
        index = read_long(cursor)
        if not 0 <= index < len(writer.symbols):
            raise DecodeError('invalid enum index')
    else:
        raise DecodeError('unknown writer kind')

def read_record(cursor, plan):
    values = {}
    for action in plan.actions:
        if action.reader_field is None:
            skip_value(cursor, action.writer_field.type)
        else:
            values[action.reader_field.name] = read_value(cursor, action.plan)
    for field in plan.defaults:
        values[field.name] = materialize_default(field.type, field.default)
    return {field.name: values[field.name] for field in plan.reader.fields}

def read_value(cursor, plan):
    op = plan.operation
    if op == 'error':
        raise ResolutionError(plan.reason)
    if op == 'writer_union':
        return read_union(cursor, plan, read_value)
    if op == 'reader_union':
        return wrap_reader_union(plan, read_value(cursor, plan.child))
    if op == 'record':
        return read_record(cursor, plan)
    if op in ('array', 'map'):
        return read_collection(cursor, op, lambda c: read_value(c, plan.child))
    if op == 'fixed':
        return cursor.read_exact(plan.writer.size)
    if op == 'enum':
        index = read_long(cursor)
        if not 0 <= index < len(plan.writer.symbols):
            raise DecodeError('invalid enum index')
        symbol = plan.writer.symbols[index]
        if symbol in plan.reader.symbols:
            return symbol
        from labavro.schemas.types import MISSING
        if plan.reader.default is not MISSING:
            return plan.reader.default
        raise ResolutionError('unknown reader enum symbol: ' + symbol)
    value = read_primitive(cursor, plan.writer.kind)
    if plan.writer.kind in ('int', 'long', 'float', 'double'):
        return promote_number(value, plan.reader.kind)
    return value
