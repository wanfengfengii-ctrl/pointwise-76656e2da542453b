import math
import struct
from labavro.schemas.types import SchemaError, MISSING, UnionValue

def materialize_default(node, value, active=None):
    active = set() if active is None else active
    identity = (id(node), id(value))
    if identity in active:
        raise SchemaError('nonterminating recursive default')
    active = active | {identity}
    k = node.kind
    if k == 'union':
        branch = node.branches[0]
        return UnionValue(0, branch.identity, materialize_default(branch, value, active))
    if k == 'null' and value is None:
        return None
    if k == 'boolean' and type(value) is bool:
        return value
    if k in ('int', 'long') and type(value) is int:
        bits = 32 if k == 'int' else 64
        if -(1 << (bits - 1)) <= value < (1 << (bits - 1)):
            return value
    if k in ('float', 'double') and type(value) in (int, float):
        try:
            result = float(value)
            if k == 'float':
                from labavro.decoding.primitives import promote_number
                result = promote_number(value, 'float')
            if math.isfinite(result):
                return result
        except (OverflowError, ValueError):
            pass
    if k == 'string' and isinstance(value, str):
        try:
            value.encode('utf-8')
            return value
        except UnicodeEncodeError:
            pass
    if k in ('bytes', 'fixed') and isinstance(value, str):
        try:
            result = bytes(ord(c) for c in value)
            if k == 'bytes' or len(result) == node.size:
                return result
        except ValueError:
            pass
    if k == 'enum' and isinstance(value, str) and value in node.symbols:
        return value
    if k == 'array' and isinstance(value, list):
        return [materialize_default(node.items, v, active) for v in value]
    if k == 'map' and isinstance(value, dict) and all(isinstance(key, str) for key in value):
        try:
            for key in value:
                key.encode('utf-8')
        except UnicodeEncodeError as exc:
            raise SchemaError('invalid UTF-8 default map key') from exc
        return {key: materialize_default(node.items, value[key], active) for key in sorted(value)}
    if k == 'record' and isinstance(value, dict):
        if set(value) - {f.name for f in node.fields}:
            raise SchemaError('unknown default record field')
        result = {}
        for f in node.fields:
            item = value.get(f.name, f.default)
            if item is MISSING:
                raise SchemaError('missing default record field: ' + f.name)
            result[f.name] = materialize_default(f.type, item, active)
        return result
    raise SchemaError('default does not match ' + node.identity)

def validate_defaults(graph):
    seen = set()
    def visit(node):
        if id(node) in seen:
            return
        seen.add(id(node))
        for f in node.fields:
            if f.default is not MISSING:
                materialize_default(f.type, f.default)
            visit(f.type)
        for branch in node.branches:
            visit(branch)
        if node.items is not None:
            visit(node.items)
    visit(graph.root)
