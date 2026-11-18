"""Independent offline replayer for labavro evidence documents.

This file is deliberately self-contained so it can be copied alone to the
receiving side; it only needs the Python standard library and never imports
labavro. It reconstructs the published experiment rows from the document's
frozen dual schemas, the per-occurrence proof tree and the raw leaf
encodings, checking the standard entries view against the proof on the way.
It never reads the archive database, the original package, pre-decoded rows
or the production decoders, and it does not try to prove that the encodings
really came from the declared package.

replay(document) returns a list of {"source": ..., "record": ...} items in
document row order, where each record uses the archive export shape (unions
carry branch_index/branch_type/value, bytes and fixed carry hex, non-finite
floats carry their float64 label). Invalid documents raise ValueError.

Usage: python3.11 -B evidence_replay.py evidence.json
writes the replayed list as one JSON value to stdout; failures exit non-zero.
"""
import hashlib
import json
import math
import re
import struct
import sys

MISSING = object()
PRIMITIVES = frozenset(('null', 'boolean', 'int', 'long', 'float', 'double', 'bytes', 'string'))
PROMOTIONS = {'int': ('long', 'float', 'double'), 'long': ('float', 'double'), 'float': ('double',)}
ENTRY_KEYS = ('path', 'reader_type', 'origin', 'writer_path', 'writer_type', 'span', 'raw_hex')
SEGMENT_TAGS = ('field', 'index', 'key', 'branch')
_PART = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _fail(message):
    raise ValueError(message)


def _require(condition, message):
    if not condition:
        _fail(message)


def _is_int(value):
    return type(value) is int


def _canon(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False)


# --- schema parsing (mirrors docs/rules.md naming and validation) ---

class _Node:
    __slots__ = ('kind', 'name', 'aliases', 'fields', 'branches', 'items',
                 'symbols', 'size', 'default')

    def __init__(self, kind, name='', aliases=(), branches=(), items=None):
        self.kind = kind
        self.name = name
        self.aliases = aliases
        self.fields = ()
        self.branches = branches
        self.items = items
        self.symbols = ()
        self.size = 0
        self.default = MISSING

    @property
    def identity(self):
        return self.name or self.kind


class _Field:
    __slots__ = ('name', 'type', 'aliases', 'default')

    def __init__(self, name, type_, aliases=(), default=MISSING):
        self.name = name
        self.type = type_
        self.aliases = aliases
        self.default = default


def _qualify(name, namespace=''):
    _require(isinstance(name, str) and name and
             all(_PART.fullmatch(part) for part in name.split('.')), 'invalid name')
    _require(not namespace or all(_PART.fullmatch(part) for part in namespace.split('.')),
             'invalid namespace')
    return name if '.' in name or not namespace else namespace + '.' + name


def _aliases(spec, namespace='', field=False):
    values = spec.get('aliases', [])
    _require(isinstance(values, list), 'aliases must be a list')
    result = []
    for value in values:
        if field:
            _require(isinstance(value, str) and _PART.fullmatch(value), 'invalid field alias')
        result.append(_qualify(value, namespace))
    _require(len(set(result)) == len(result), 'duplicate alias')
    return tuple(result)


class _Scope:
    def __init__(self):
        self.names = {}

    def resolve(self, name, namespace=''):
        full = _qualify(name, namespace)
        _require(full in self.names, 'unresolved name: ' + full)
        return self.names[full]

    def parse(self, spec, namespace=''):
        _require(isinstance(spec, (str, list, dict)), 'schema must be a name, object or union')
        if isinstance(spec, list):
            _require(spec, 'empty union')
            branches = tuple(self.parse(item, namespace) for item in spec)
            identities = [branch.identity for branch in branches]
            _require(not any(branch.kind == 'union' for branch in branches) and
                     len(set(identities)) == len(identities), 'nested or duplicate union branch')
            return _Node('union', branches=branches)
        if isinstance(spec, str):
            return _Node(spec) if spec in PRIMITIVES else self.resolve(spec, namespace)
        _require('logicalType' not in spec, 'logical types are unsupported')
        _require('type' in spec, 'missing type')
        kind = spec['type']
        if isinstance(kind, (list, dict)):
            return self.parse(kind, namespace)
        _require(isinstance(kind, str), 'invalid type')
        if kind in PRIMITIVES:
            return _Node(kind)
        if kind in ('record', 'enum', 'fixed'):
            declared_ns = spec.get('namespace', namespace)
            _require(isinstance(declared_ns, str), 'invalid namespace')
            full = _qualify(spec.get('name'), declared_ns)
            ns = full.rpartition('.')[0]
            _require(full not in self.names and full not in PRIMITIVES,
                     'duplicate or reserved name: ' + full)
            node = _Node(kind, full, _aliases(spec, ns))
            self.names[full] = node
            if kind == 'record':
                fields = spec.get('fields')
                _require(isinstance(fields, list), 'record fields must be a list')
                found = set()
                parsed = []
                for item in fields:
                    _require(isinstance(item, dict) and 'type' in item, 'invalid record field')
                    name = item.get('name')
                    _require(isinstance(name, str) and _PART.fullmatch(name) and name not in found,
                             'invalid or duplicate field')
                    found.add(name)
                    parsed.append(_Field(name, self.parse(item['type'], ns),
                                         _aliases(item, field=True), item.get('default', MISSING)))
                node.fields = tuple(parsed)
            elif kind == 'enum':
                symbols = spec.get('symbols')
                _require(isinstance(symbols, list) and symbols, 'empty enum')
                for symbol in symbols:
                    _require(isinstance(symbol, str) and _PART.fullmatch(symbol),
                             'invalid enum symbol')
                _require(len(set(symbols)) == len(symbols), 'duplicate enum symbol')
                node.symbols = tuple(symbols)
                node.default = spec.get('default', MISSING)
                _require(node.default is MISSING or node.default in node.symbols,
                         'invalid enum default')
            else:
                size = spec.get('size')
                _require(_is_int(size) and size > 0, 'invalid fixed size')
                node.size = size
            return node
        if kind in ('array', 'map'):
            key = 'items' if kind == 'array' else 'values'
            _require(key in spec, 'missing collection element type')
            return _Node(kind, items=self.parse(spec[key], namespace))
        return self.resolve(kind, namespace)


def _build_schema(spec):
    scope = _Scope()
    root = scope.parse(spec)
    seen = set()

    def visit(node):
        if id(node) in seen:
            return
        seen.add(id(node))
        for field in node.fields:
            if field.default is not MISSING:
                _materialize_default(field.type, field.default)
            visit(field.type)
        for branch in node.branches:
            visit(branch)
        if node.items is not None:
            visit(node.items)

    visit(root)
    return root


class _Union:
    __slots__ = ('branch_index', 'branch_type', 'value')

    def __init__(self, branch_index, branch_type, value):
        self.branch_index = branch_index
        self.branch_type = branch_type
        self.value = value


def _materialize_default(node, value, active=None):
    active = set() if active is None else active
    identity = (id(node), id(value))
    _require(identity not in active, 'nonterminating recursive default')
    active = active | {identity}
    kind = node.kind
    if kind == 'union':
        branch = node.branches[0]
        return _Union(0, branch.identity, _materialize_default(branch, value, active))
    if kind == 'null' and value is None:
        return None
    if kind == 'boolean' and type(value) is bool:
        return value
    if kind in ('int', 'long') and type(value) is int:
        bits = 32 if kind == 'int' else 64
        if -(1 << (bits - 1)) <= value < (1 << (bits - 1)):
            return value
    if kind in ('float', 'double') and type(value) in (int, float):
        try:
            result = float(value)
            if kind == 'float':
                result = _promote_number(value, 'float')
            if math.isfinite(result):
                return result
        except (OverflowError, ValueError):
            pass
    if kind == 'string' and isinstance(value, str):
        try:
            value.encode('utf-8')
            return value
        except UnicodeEncodeError:
            pass
    if kind in ('bytes', 'fixed') and isinstance(value, str):
        try:
            result = bytes(ord(char) for char in value)
            if kind == 'bytes' or len(result) == node.size:
                return result
        except ValueError:
            pass
    if kind == 'enum' and isinstance(value, str) and value in node.symbols:
        return value
    if kind == 'array' and isinstance(value, list):
        return [_materialize_default(node.items, item, active) for item in value]
    if kind == 'map' and isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for key in value:
            try:
                key.encode('utf-8')
            except UnicodeEncodeError:
                _fail('invalid UTF-8 default map key')
        return {key: _materialize_default(node.items, value[key], active) for key in sorted(value)}
    if kind == 'record' and isinstance(value, dict):
        _require(not (set(value) - {field.name for field in node.fields}),
                 'unknown default record field')
        result = {}
        for field in node.fields:
            item = value.get(field.name, field.default)
            _require(item is not MISSING, 'missing default record field: ' + field.name)
            result[field.name] = _materialize_default(field.type, item, active)
        return result
    _fail('default does not match ' + node.identity)


# --- resolution (first compatible reader branch, alias and promotion rules) ---

class _Action:
    __slots__ = ('writer_field', 'reader_field', 'plan')

    def __init__(self, writer_field, reader_field, plan):
        self.writer_field = writer_field
        self.reader_field = reader_field
        self.plan = plan


class _Plan:
    __slots__ = ('writer', 'reader', 'operation', 'actions', 'defaults',
                 'branches', 'child', 'branch_index', 'reason')

    def __init__(self, writer, reader):
        self.writer = writer
        self.reader = reader
        self.operation = 'error'
        self.actions = ()
        self.defaults = ()
        self.branches = ()
        self.child = None
        self.branch_index = -1
        self.reason = 'incompatible schemas'


def _named(writer, reader):
    return writer.name == reader.name or writer.name in reader.aliases


def _field_pairs(writer, reader):
    used, pairs, defaults = set(), {}, []
    for reader_field in reader.fields:
        direct = [wf for wf in writer.fields if wf.name == reader_field.name]
        candidates = direct or [wf for wf in writer.fields if wf.name in reader_field.aliases]
        _require(len(candidates) <= 1, 'ambiguous reader alias: ' + reader_field.name)
        if not candidates:
            _require(reader_field.default is not MISSING,
                     'missing required field: ' + reader_field.name)
            defaults.append(reader_field)
        else:
            writer_field = candidates[0]
            _require(writer_field.name not in used, 'writer field claimed twice: ' + writer_field.name)
            used.add(writer_field.name)
            pairs[writer_field.name] = reader_field
    return pairs, tuple(defaults)


def _compatible(writer, reader, active=None):
    active = set() if active is None else active
    key = (id(writer), id(reader))
    if key in active:
        return True
    active = active | {key}
    if writer.kind == 'union':
        return any(_compatible(branch, reader, active) for branch in writer.branches)
    if reader.kind == 'union':
        return any(_compatible(writer, branch, active) for branch in reader.branches)
    if reader.kind in PROMOTIONS.get(writer.kind, ()):
        return True
    if writer.kind != reader.kind:
        return False
    if writer.kind in ('record', 'enum', 'fixed') and not _named(writer, reader):
        return False
    if writer.kind == 'fixed':
        return writer.size == reader.size
    if writer.kind == 'record':
        try:
            pairs, _ = _field_pairs(writer, reader)
        except ValueError:
            return False
        return all(_compatible(wf.type, pairs[wf.name].type, active)
                   for wf in writer.fields if wf.name in pairs)
    if writer.kind in ('array', 'map'):
        return _compatible(writer.items, reader.items, active)
    return True


def _compile_resolution(writer_root, reader_root):
    cache = {}

    def compile_pair(writer, reader):
        key = (id(writer), id(reader))
        if key in cache:
            return cache[key]
        plan = _Plan(writer, reader)
        cache[key] = plan
        if writer.kind == 'union':
            plan.operation = 'writer_union'
            plan.branches = tuple(compile_pair(branch, reader) for branch in writer.branches)
        elif reader.kind == 'union':
            for index, branch in enumerate(reader.branches):
                if _compatible(writer, branch):
                    plan.operation = 'reader_union'
                    plan.branch_index = index
                    plan.child = compile_pair(writer, branch)
                    break
        elif _compatible(writer, reader):
            plan.operation = writer.kind
            if writer.kind == 'record':
                pairs, plan.defaults = _field_pairs(writer, reader)
                plan.actions = tuple(_Action(wf, pairs.get(wf.name),
                                             compile_pair(wf.type, pairs[wf.name].type)
                                             if wf.name in pairs else None)
                                     for wf in writer.fields)
            elif writer.kind in ('array', 'map'):
                plan.child = compile_pair(writer.items, reader.items)
        else:
            plan.reason = writer.identity + ' cannot resolve to ' + reader.identity
        return plan

    return compile_pair(writer_root, reader_root)


# --- byte decoding (zigzag varints, little-endian floats, strict UTF-8) ---

class _Cursor:
    def __init__(self, data):
        self.data = data
        self.position = 0

    @property
    def remaining(self):
        return len(self.data) - self.position

    def read_exact(self, count):
        _require(_is_int(count) and 0 <= count <= self.remaining,
                 'truncated or invalid byte length')
        start = self.position
        self.position += count
        return self.data[start:self.position]

    def require_end(self):
        _require(self.remaining == 0, 'unconsumed payload: ' + str(self.remaining))


def _read_long(cursor):
    value = 0
    for index in range(10):
        byte = cursor.read_exact(1)[0]
        _require(not (index == 9 and byte > 1), 'long exceeds 64 bits')
        value |= (byte & 127) << (7 * index)
        if not byte & 128:
            return (value >> 1) ^ -(value & 1)
    _fail('unterminated long')


def _read_primitive(cursor, kind):
    if kind == 'null':
        return None
    if kind == 'boolean':
        value = cursor.read_exact(1)[0]
        _require(value <= 1, 'invalid boolean')
        return bool(value)
    if kind in ('int', 'long'):
        value = _read_long(cursor)
        _require(kind != 'int' or -(1 << 31) <= value < (1 << 31), 'int exceeds 32 bits')
        return value
    if kind in ('float', 'double'):
        return struct.unpack('<f' if kind == 'float' else '<d',
                             cursor.read_exact(4 if kind == 'float' else 8))[0]
    if kind in ('bytes', 'string'):
        data = cursor.read_exact(_read_long(cursor))
        if kind == 'bytes':
            return data
        try:
            return data.decode('utf-8', errors='strict')
        except UnicodeDecodeError:
            _fail('invalid UTF-8')
    _fail('not a primitive: ' + kind)


def _promote_number(value, target):
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


# --- proof tree walk ---

def _unhex(text):
    _require(isinstance(text, str) and len(text) % 2 == 0 and text == text.lower(),
             'raw must be lowercase hex')
    try:
        return bytes.fromhex(text)
    except ValueError:
        _fail('raw is not valid hex')


def _node_kind(node):
    _require(isinstance(node, dict), 'proof node must be an object')
    kind = node.get('kind')
    _require(isinstance(kind, str), 'proof node kind must be a string')
    return kind


def _span_raw(node):
    span = node.get('span')
    _require(isinstance(span, list) and len(span) == 2 and
             all(_is_int(part) for part in span) and 0 <= span[0] <= span[1],
             'invalid proof span')
    data = _unhex(node.get('raw'))
    _require(span[1] - span[0] == len(data), 'proof span does not match raw bytes')
    return span, data


def _require_branch(value, limit):
    _require(_is_int(value) and 0 <= value < limit, 'invalid union branch index')


class _Walker:
    def __init__(self):
        self.entries = []
        self.rpath = []
        self.wpath = []

    def wire_entry(self, reader_type, writer_type, span, raw_hex):
        self.entries.append({
            'path': [list(segment) for segment in self.rpath],
            'reader_type': reader_type,
            'origin': 'wire',
            'writer_path': [list(segment) for segment in self.wpath],
            'writer_type': writer_type,
            'span': list(span),
            'raw_hex': raw_hex})

    def default_entry(self, reader_type):
        self.entries.append({
            'path': [list(segment) for segment in self.rpath],
            'reader_type': reader_type,
            'origin': 'default',
            'writer_path': None,
            'writer_type': None,
            'span': None,
            'raw_hex': None})


def _decode_leaf(plan, data):
    cursor = _Cursor(data)
    op = plan.operation
    if op == 'fixed':
        value = cursor.read_exact(plan.writer.size)
    elif op == 'enum':
        index = _read_long(cursor)
        _require(0 <= index < len(plan.writer.symbols), 'invalid enum index')
        symbol = plan.writer.symbols[index]
        if symbol in plan.reader.symbols:
            value = symbol
        elif plan.reader.default is not MISSING:
            value = plan.reader.default
        else:
            _fail('unknown reader enum symbol: ' + symbol)
    else:
        _require(op in PRIMITIVES, 'proof leaf does not match scalar plan')
        value = _read_primitive(cursor, plan.writer.kind)
        if plan.writer.kind in ('int', 'long', 'float', 'double'):
            value = _promote_number(value, plan.reader.kind)
    cursor.require_end()
    return value


def _derive_default_entries(ctx, reader_type, value):
    kind = reader_type.kind
    if kind == 'union':
        ctx.rpath.append(['branch', 0])
        _derive_default_entries(ctx, reader_type.branches[0], value.value)
        ctx.rpath.pop()
        return
    if kind == 'record':
        if not reader_type.fields:
            ctx.default_entry(reader_type.identity)
            return
        for field in reader_type.fields:
            ctx.rpath.append(['field', field.name])
            _derive_default_entries(ctx, field.type, value[field.name])
            ctx.rpath.pop()
        return
    if kind == 'array':
        if not value:
            ctx.default_entry(reader_type.identity)
            return
        for index, item in enumerate(value):
            ctx.rpath.append(['index', index])
            _derive_default_entries(ctx, reader_type.items, item)
            ctx.rpath.pop()
        return
    if kind == 'map':
        if not value:
            ctx.default_entry(reader_type.identity)
            return
        for key in value:
            ctx.rpath.append(['key', key])
            _derive_default_entries(ctx, reader_type.items, value[key])
            ctx.rpath.pop()
        return
    ctx.default_entry(reader_type.identity)


def _walk(ctx, node, plan):
    op = plan.operation
    if op == 'error':
        _fail('incompatible schemas: ' + plan.reason)
    kind = _node_kind(node)
    if op == 'writer_union':
        _require(kind == 'selector', 'writer union requires selector proof')
        _, data = _span_raw(node)
        cursor = _Cursor(data)
        index = _read_long(cursor)
        cursor.require_end()
        _require(index == node.get('branch'), 'selector branch mismatch')
        _require_branch(index, len(plan.writer.branches))
        ctx.wpath.append(['branch', index])
        value = _walk(ctx, node.get('child'), plan.branches[index])
        ctx.wpath.pop()
        return value
    if op == 'reader_union':
        _require(kind == 'union', 'reader union requires union proof')
        _require(node.get('branch') == plan.branch_index, 'reader union branch mismatch')
        ctx.rpath.append(['branch', plan.branch_index])
        value = _walk(ctx, node.get('child'), plan.child)
        ctx.rpath.pop()
        branch = plan.reader.branches[plan.branch_index]
        return _Union(plan.branch_index, branch.identity, value)
    if op == 'record':
        return _walk_record(ctx, node, plan)
    if op in ('array', 'map'):
        return _walk_collection(ctx, node, plan, op)
    _require(kind == 'value', 'scalar plan requires value proof')
    span, data = _span_raw(node)
    value = _decode_leaf(plan, data)
    ctx.wire_entry(plan.reader.identity, plan.writer.identity, span, node['raw'])
    return value


def _walk_record(ctx, node, plan):
    _require(_node_kind(node) == 'record', 'record plan requires record proof')
    fields = node.get('fields')
    _require(isinstance(fields, list), 'record proof fields must be a list')
    names = []
    children = {}
    for item in fields:
        _require(isinstance(item, list) and len(item) == 2 and isinstance(item[0], str),
                 'invalid record proof field')
        _require(item[0] not in children, 'duplicate record proof field')
        children[item[0]] = item[1]
        names.append(item[0])
    _require(names == [field.name for field in plan.reader.fields],
             'record proof fields do not match reader schema')
    values = {}
    for action in plan.actions:
        if action.reader_field is None:
            continue
        ctx.rpath.append(['field', action.reader_field.name])
        ctx.wpath.append(['field', action.writer_field.name])
        values[action.reader_field.name] = _walk(ctx, children[action.reader_field.name], action.plan)
        ctx.wpath.pop()
        ctx.rpath.pop()
    for field in plan.defaults:
        child = children[field.name]
        _require(_node_kind(child) == 'default', 'defaulted field requires default proof')
        _require(_canon(child.get('literal')) == _canon(field.default),
                 'default literal does not match reader schema')
        ctx.rpath.append(['field', field.name])
        value = _materialize_default(field.type, field.default)
        _derive_default_entries(ctx, field.type, value)
        ctx.rpath.pop()
        values[field.name] = value
    result = {field.name: values[field.name] for field in plan.reader.fields}
    if not plan.reader.fields:
        span, data = _span_raw(node)
        _require(data == b'', 'empty record proof must carry zero bytes')
        ctx.wire_entry(plan.reader.identity, plan.writer.identity, span, node['raw'])
    return result


def _walk_collection(ctx, node, plan, kind):
    _require(_node_kind(node) == kind, 'collection plan requires matching proof')
    blocks = node.get('blocks')
    _require(isinstance(blocks, list), 'collection blocks must be a list')
    total = 0
    for block in blocks:
        _require(isinstance(block, dict), 'invalid collection block')
        count = block.get('count')
        _require(_is_int(count) and count >= 1, 'invalid collection block count')
        size = block.get('size')
        if size is not None:
            _require(_is_int(size) and size >= 0, 'invalid collection block size')
        total += count
    if kind == 'array':
        items = node.get('items')
        _require(isinstance(items, list) and len(items) == total,
                 'array items do not match block counts')
        result = []
        for index, child in enumerate(items):
            ctx.rpath.append(['index', index])
            ctx.wpath.append(['index', index])
            result.append(_walk(ctx, child, plan.child))
            ctx.wpath.pop()
            ctx.rpath.pop()
        if not items:
            _empty_container(ctx, node, plan)
        return result
    entries = node.get('entries')
    _require(isinstance(entries, list) and len(entries) == total,
             'map entries do not match block counts')
    result = {}
    for item in entries:
        _require(isinstance(item, list) and len(item) == 2 and isinstance(item[0], str),
                 'invalid map proof entry')
        key = item[0]
        _require(key not in result, 'duplicate map key')
        ctx.rpath.append(['key', key])
        ctx.wpath.append(['key', key])
        result[key] = _walk(ctx, item[1], plan.child)
        ctx.wpath.pop()
        ctx.rpath.pop()
    if not entries:
        _empty_container(ctx, node, plan)
    return {key: result[key] for key in sorted(result)}


def _empty_container(ctx, node, plan):
    span, data = _span_raw(node)
    cursor = _Cursor(data)
    _require(_read_long(cursor) == 0, 'empty container must carry its zero terminator')
    cursor.require_end()
    ctx.wire_entry(plan.reader.identity, plan.writer.identity, span, node['raw'])


# --- entries view checking ---

def _check_path(path):
    _require(isinstance(path, list), 'entry path must be a list')
    for segment in path:
        _require(isinstance(segment, list) and len(segment) == 2, 'invalid path segment')
        tag, value = segment
        _require(tag in SEGMENT_TAGS, 'invalid path segment tag')
        if tag in ('field', 'key'):
            _require(isinstance(value, str), 'invalid path segment value')
        else:
            _require(_is_int(value) and value >= 0, 'invalid path segment value')


def _normalize_entry(entry):
    _require(isinstance(entry, dict), 'entry must be an object')
    projected = {}
    for key in ENTRY_KEYS:
        _require(key in entry, 'entry missing key: ' + key)
        projected[key] = entry[key]
    _check_path(projected['path'])
    _require(isinstance(projected['reader_type'], str), 'invalid entry reader_type')
    origin = projected['origin']
    if origin == 'wire':
        _check_path(projected['writer_path'])
        _require(isinstance(projected['writer_type'], str), 'invalid entry writer_type')
        span = projected['span']
        _require(isinstance(span, list) and len(span) == 2 and
                 all(_is_int(part) for part in span) and 0 <= span[0] <= span[1],
                 'invalid entry span')
        _require(span[1] - span[0] == len(_unhex(projected['raw_hex'])),
                 'entry span does not match raw_hex')
    elif origin == 'default':
        _require(projected['writer_path'] is None and projected['writer_type'] is None and
                 projected['span'] is None and projected['raw_hex'] is None,
                 'default entry must not carry wire facts')
    else:
        _fail('invalid entry origin')
    return _canon(projected)


# --- export shape ---

def _export_value(value):
    if isinstance(value, _Union):
        return {'branch_index': value.branch_index, 'branch_type': value.branch_type,
                'value': _export_value(value.value)}
    if isinstance(value, bytes):
        return {'type': 'bytes', 'hex': value.hex()}
    if type(value) is float and not math.isfinite(value):
        label = 'NaN' if math.isnan(value) else ('Infinity' if value > 0 else '-Infinity')
        return {'type': 'float64', 'value': label}
    if isinstance(value, dict):
        return {key: _export_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_export_value(item) for item in value]
    return value


def _schema_digest(schema):
    return hashlib.sha256(_canon(schema).encode('utf-8')).hexdigest()


def _replay(document):
    _require(isinstance(document, dict), 'evidence document must be an object')
    result_id = document.get('result_id')
    _require(isinstance(result_id, str), 'invalid result_id')
    binding = document.get('binding')
    _require(isinstance(binding, dict), 'invalid binding')
    for key in ('package_id', 'package_digest', 'writer_schema_digest',
                'reader_id', 'reader_digest'):
        _require(isinstance(binding.get(key), str), 'invalid binding key: ' + key)
    receipt = document.get('receipt')
    _require(isinstance(receipt, dict), 'invalid receipt')
    task_id = receipt.get('task_id')
    _require(isinstance(task_id, str), 'invalid receipt task_id')
    _require(result_id == 'result:' + task_id, 'result_id does not match receipt task_id')
    row_count = receipt.get('row_count')
    _require(_is_int(row_count) and row_count >= 0, 'invalid receipt row_count')
    _require(_schema_digest(document.get('writer_schema')) == binding['writer_schema_digest'],
             'writer schema digest mismatch')
    _require(_schema_digest(document.get('reader_schema')) == binding['reader_digest'],
             'reader schema digest mismatch')
    writer_root = _build_schema(document.get('writer_schema'))
    reader_root = _build_schema(document.get('reader_schema'))
    plan = _compile_resolution(writer_root, reader_root)
    if plan.operation == 'error':
        _fail(plan.reason)
    rows = document.get('rows')
    _require(isinstance(rows, list) and len(rows) == row_count,
             'rows do not match receipt row_count')
    output = []
    for ordinal, row in enumerate(rows):
        _require(isinstance(row, dict), 'row must be an object')
        source = row.get('source')
        _require(isinstance(source, dict), 'invalid source')
        _require(isinstance(source.get('package_id'), str), 'invalid source package_id')
        _require(_is_int(source.get('ordinal')) and source['ordinal'] == ordinal,
                 'source ordinal mismatch')
        for key in ('block_index', 'record_index'):
            _require(_is_int(source.get(key)) and source[key] >= 0, 'invalid source ' + key)
        entries = row.get('entries')
        _require(isinstance(entries, list), 'row entries must be a list')
        walker = _Walker()
        value = _walk(walker, row.get('proof'), plan)
        claimed = sorted(_normalize_entry(entry) for entry in entries)
        derived = sorted(_canon(entry) for entry in walker.entries)
        _require(claimed == derived, 'entries do not match proof')
        output.append({'source': source, 'record': _export_value(value)})
    return output


def replay(document):
    """Replay one evidence document, returning the published rows.

    The document is reconstructed solely from its frozen dual schemas, proof
    trees and raw encodings; every entries item is checked against the proof.
    Invalid documents raise ValueError.
    """
    try:
        return _replay(document)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('invalid evidence document: ' + str(exc)) from exc


def _reject_constant(value):
    raise ValueError('non-finite JSON constant: ' + value)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print('usage: python3.11 -B evidence_replay.py evidence.json', file=sys.stderr)
        return 2
    try:
        with open(args[0], 'r', encoding='utf-8') as handle:
            document = json.load(handle, parse_constant=_reject_constant)
        result = replay(document)
        text = json.dumps(result, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    except Exception as exc:
        print('evidence replay failed: ' + str(exc), file=sys.stderr)
        return 1
    sys.stdout.write(text + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
