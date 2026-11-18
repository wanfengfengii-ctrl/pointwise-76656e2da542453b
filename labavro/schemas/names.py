import re
from labavro.schemas.types import (PRIMITIVES, SchemaError, TypeNode, FieldSpec,
                                  SchemaGraph, MISSING, validate_type_shape)

_PART = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

def qualify_name(name, namespace=''):
    if not isinstance(name, str) or not name or not all(_PART.fullmatch(p) for p in name.split('.')):
        raise SchemaError('invalid name')
    if namespace and not all(_PART.fullmatch(p) for p in namespace.split('.')):
        raise SchemaError('invalid namespace')
    return name if '.' in name or not namespace else namespace + '.' + name

def _aliases(spec, namespace='', field=False):
    values = spec.get('aliases', [])
    if not isinstance(values, list):
        raise SchemaError('aliases must be a list')
    result = []
    for value in values:
        if field and (not isinstance(value, str) or not _PART.fullmatch(value)):
            raise SchemaError('invalid field alias')
        result.append(qualify_name(value, namespace))
    if len(set(result)) != len(result):
        raise SchemaError('duplicate alias')
    return tuple(result)

class NameScope:
    def __init__(self):
        self.names = {}

    def resolve(self, name, namespace=''):
        full = qualify_name(name, namespace)
        if full not in self.names:
            raise SchemaError('unresolved name: ' + full)
        return self.names[full]

    def parse(self, spec, namespace=''):
        validate_type_shape(spec)
        if isinstance(spec, str):
            return TypeNode(spec) if spec in PRIMITIVES else self.resolve(spec, namespace)
        if isinstance(spec, list):
            branches = tuple(self.parse(s, namespace) for s in spec)
            identities = [b.identity for b in branches]
            if any(b.kind == 'union' for b in branches) or len(set(identities)) != len(identities):
                raise SchemaError('nested or duplicate union branch')
            return TypeNode('union', branches=branches)
        kind = spec['type']
        if isinstance(kind, (list, dict)):
            return self.parse(kind, namespace)
        if not isinstance(kind, str):
            raise SchemaError('invalid type')
        if kind in PRIMITIVES:
            return TypeNode(kind)
        if kind in ('record', 'enum', 'fixed'):
            declared_ns = spec.get('namespace', namespace)
            if not isinstance(declared_ns, str):
                raise SchemaError('invalid namespace')
            full = qualify_name(spec.get('name'), declared_ns)
            ns = full.rpartition('.')[0]
            if full in self.names or full in PRIMITIVES:
                raise SchemaError('duplicate or reserved name: ' + full)
            node = TypeNode(kind, full, _aliases(spec, ns))
            self.names[full] = node
            if kind == 'record':
                fields = spec.get('fields')
                if not isinstance(fields, list):
                    raise SchemaError('record fields must be a list')
                found, result = set(), []
                for item in fields:
                    if not isinstance(item, dict) or 'type' not in item:
                        raise SchemaError('invalid record field')
                    name = item.get('name')
                    if not isinstance(name, str) or not _PART.fullmatch(name) or name in found:
                        raise SchemaError('invalid or duplicate field')
                    found.add(name)
                    result.append(FieldSpec(name, self.parse(item['type'], ns),
                                            _aliases(item, field=True), item.get('default', MISSING)))
                node.fields = tuple(result)
            elif kind == 'enum':
                symbols = spec.get('symbols')
                if not isinstance(symbols, list) or not symbols:
                    raise SchemaError('empty enum')
                if any(not isinstance(s, str) or not _PART.fullmatch(s) for s in symbols):
                    raise SchemaError('invalid enum symbol')
                if len(set(symbols)) != len(symbols):
                    raise SchemaError('duplicate enum symbol')
                node.symbols = tuple(symbols)
                node.default = spec.get('default', MISSING)
                if node.default is not MISSING and node.default not in node.symbols:
                    raise SchemaError('invalid enum default')
            else:
                size = spec.get('size')
                if type(size) is not int or size <= 0:
                    raise SchemaError('invalid fixed size')
                node.size = size
            return node
        if kind in ('array', 'map'):
            key = 'items' if kind == 'array' else 'values'
            if key not in spec:
                raise SchemaError('missing collection element type')
            return TypeNode(kind, items=self.parse(spec[key], namespace))
        return self.resolve(kind, namespace)

def build_schema(spec):
    scope = NameScope()
    root = scope.parse(spec)
    graph = SchemaGraph(root, dict(scope.names))
    from labavro.schemas.defaults import validate_defaults
    validate_defaults(graph)
    return graph
