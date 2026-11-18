from dataclasses import dataclass, field

class SchemaError(ValueError):
    pass

MISSING = object()
PRIMITIVES = frozenset(('null', 'boolean', 'int', 'long', 'float', 'double', 'bytes', 'string'))
KINDS = PRIMITIVES | {'record', 'enum', 'fixed', 'array', 'map', 'union'}

@dataclass(eq=False)
class TypeNode:
    kind: str
    name: str = ''
    aliases: tuple = ()
    fields: tuple = ()
    branches: tuple = ()
    items: object = None
    symbols: tuple = ()
    size: int = 0
    default: object = MISSING

    @property
    def identity(self):
        return self.name or self.kind

@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: TypeNode
    aliases: tuple = ()
    default: object = MISSING

@dataclass(frozen=True)
class SchemaGraph:
    root: TypeNode
    names: dict

@dataclass(frozen=True)
class UnionValue:
    branch_index: int
    branch_type: str
    value: object

def validate_type_shape(spec):
    if not isinstance(spec, (str, list, dict)):
        raise SchemaError('schema must be a name, object or union')
    if isinstance(spec, list) and not spec:
        raise SchemaError('empty union')
    if isinstance(spec, dict):
        if 'logicalType' in spec:
            raise SchemaError('logical types are unsupported')
        if 'type' not in spec:
            raise SchemaError('missing type')
