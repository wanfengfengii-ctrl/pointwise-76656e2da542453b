"""Evidenced decoding: replays the V1 resolution plan while recording provenance.

The tracked decoder consumes writer bytes with the same primitives, the same
validation and the same resolution plan as labavro.decoding.records, and
additionally records, for every accepted occurrence:

- entries: the flat check view (one item per reader scalar leaf and per
  reader container without children), with reader/writer paths, type
  identities, payload-relative spans and raw bytes;
- proof: a closed JSON tree mirroring the reader value tree. Wire scalar
  leaves carry their span and raw encoding, writer union selectors carry
  their own encoding and selected branch, reader union wrappers carry the
  chosen branch, collections carry block counts/negative byte lengths and
  map keys in wire order, and default subtrees carry the reader field
  default literal. Discarded writer subtrees are skipped with the original
  skip_value validation and leave no trace in the evidence.
"""
from dataclasses import dataclass

from labavro.schemas.types import MISSING, UnionValue
from labavro.schemas.defaults import materialize_default
from labavro.schemas.resolution import ResolutionError
from labavro.decoding.cursor import ByteCursor, DecodeError
from labavro.decoding.primitives import read_long, read_primitive, promote_number
from labavro.decoding.records import skip_value
from labavro.decoding.unions import select_writer_branch


@dataclass(frozen=True)
class TrackedRecord:
    block_index: int
    record_index: int
    value: object
    proof: object
    entries: list


@dataclass(frozen=True)
class EvidenceCandidate:
    binding: dict
    rows: tuple


class TrackContext:
    def __init__(self):
        self.entries = []
        self.path = []
        self.writer_path = []

    def wire_entry(self, reader_type, writer_type, span, raw_hex):
        self.entries.append({
            'path': [list(segment) for segment in self.path],
            'reader_type': reader_type,
            'origin': 'wire',
            'writer_path': [list(segment) for segment in self.writer_path],
            'writer_type': writer_type,
            'span': list(span),
            'raw_hex': raw_hex})

    def default_entry(self, reader_type):
        self.entries.append({
            'path': [list(segment) for segment in self.path],
            'reader_type': reader_type,
            'origin': 'default',
            'writer_path': None,
            'writer_type': None,
            'span': None,
            'raw_hex': None})


def _raw(cursor, start, end):
    return cursor.data[start:end].tobytes()


def _wire_leaf(cursor, plan, ctx, start):
    end = cursor.tell()
    raw = _raw(cursor, start, end)
    ctx.wire_entry(plan.reader.identity, plan.writer.identity, [start, end], raw.hex())
    return {'kind': 'value', 'span': [start, end], 'raw': raw.hex()}


def emit_default_entries(ctx, reader_type, value):
    kind = reader_type.kind
    if kind == 'union':
        ctx.path.append(['branch', 0])
        emit_default_entries(ctx, reader_type.branches[0], value.value)
        ctx.path.pop()
        return
    if kind == 'record':
        if not reader_type.fields:
            ctx.default_entry(reader_type.identity)
            return
        for field in reader_type.fields:
            ctx.path.append(['field', field.name])
            emit_default_entries(ctx, field.type, value[field.name])
            ctx.path.pop()
        return
    if kind == 'array':
        if not value:
            ctx.default_entry(reader_type.identity)
            return
        for index, item in enumerate(value):
            ctx.path.append(['index', index])
            emit_default_entries(ctx, reader_type.items, item)
            ctx.path.pop()
        return
    if kind == 'map':
        if not value:
            ctx.default_entry(reader_type.identity)
            return
        for key in value:
            ctx.path.append(['key', key])
            emit_default_entries(ctx, reader_type.items, value[key])
            ctx.path.pop()
        return
    ctx.default_entry(reader_type.identity)


def read_record_tracked(cursor, plan, ctx):
    start = cursor.tell()
    values = {}
    children = {}
    for action in plan.actions:
        if action.reader_field is None:
            skip_value(cursor, action.writer_field.type)
        else:
            ctx.path.append(['field', action.reader_field.name])
            ctx.writer_path.append(['field', action.writer_field.name])
            value, node = read_tracked(cursor, action.plan, ctx)
            ctx.writer_path.pop()
            ctx.path.pop()
            values[action.reader_field.name] = value
            children[action.reader_field.name] = node
    for field in plan.defaults:
        ctx.path.append(['field', field.name])
        value = materialize_default(field.type, field.default)
        emit_default_entries(ctx, field.type, value)
        ctx.path.pop()
        values[field.name] = value
        children[field.name] = {'kind': 'default', 'literal': field.default}
    result = {field.name: values[field.name] for field in plan.reader.fields}
    proof = {'kind': 'record',
             'fields': [[field.name, children[field.name]] for field in plan.reader.fields]}
    if not plan.reader.fields:
        # An empty reader record consumes no accepted bytes; its zero-length
        # span sits at the record start and never covers skipped writer fields.
        proof['span'] = [start, start]
        proof['raw'] = ''
        ctx.wire_entry(plan.reader.identity, plan.writer.identity, [start, start], '')
    return result, proof


def read_collection_tracked(cursor, kind, plan, ctx):
    start = cursor.tell()
    result = {} if kind == 'map' else []
    seen = set()
    items = []
    pairs = []
    blocks = []
    index = 0
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
            blocks.append({'count': count, 'size': size})
        else:
            blocks.append({'count': count})
        for _ in range(count):
            if kind == 'map':
                key = read_primitive(cursor, 'string')
                if key in seen:
                    raise DecodeError('duplicate map key')
                seen.add(key)
                segment = ['key', key]
            else:
                segment = ['index', index]
            ctx.path.append(segment)
            ctx.writer_path.append(list(segment))
            value, node = read_tracked(cursor, plan.child, ctx)
            ctx.writer_path.pop()
            ctx.path.pop()
            if end is not None and cursor.tell() > end:
                raise DecodeError('collection crosses declared block boundary')
            if kind == 'map':
                result[key] = value
                pairs.append([key, node])
            else:
                result.append(value)
                items.append(node)
                index += 1
        if end is not None and cursor.tell() != end:
            raise DecodeError('collection block byte count mismatch')
    if kind == 'map':
        proof = {'kind': 'map', 'entries': pairs, 'blocks': blocks}
        if not pairs:
            _mark_empty_container(cursor, plan, ctx, proof, start)
        return {key: result[key] for key in sorted(result)}, proof
    proof = {'kind': 'array', 'items': items, 'blocks': blocks}
    if not items:
        _mark_empty_container(cursor, plan, ctx, proof, start)
    return result, proof


def _mark_empty_container(cursor, plan, ctx, proof, start):
    end = cursor.tell()
    raw = _raw(cursor, start, end)
    proof['span'] = [start, end]
    proof['raw'] = raw.hex()
    ctx.wire_entry(plan.reader.identity, plan.writer.identity, [start, end], raw.hex())


def read_tracked(cursor, plan, ctx):
    op = plan.operation
    if op == 'error':
        raise ResolutionError(plan.reason)
    if op == 'writer_union':
        start = cursor.tell()
        branch = select_writer_branch(cursor, plan.writer)
        selector_end = cursor.tell()
        selected = plan.branches[branch.index]
        if selected.writer is not branch.type:
            raise DecodeError('union plan lost writer identity')
        ctx.writer_path.append(['branch', branch.index])
        value, child = read_tracked(cursor, selected, ctx)
        ctx.writer_path.pop()
        proof = {'kind': 'selector', 'span': [start, selector_end],
                 'raw': _raw(cursor, start, selector_end).hex(),
                 'branch': branch.index, 'child': child}
        return value, proof
    if op == 'reader_union':
        ctx.path.append(['branch', plan.branch_index])
        value, child = read_tracked(cursor, plan.child, ctx)
        ctx.path.pop()
        branch = plan.reader.branches[plan.branch_index]
        return UnionValue(plan.branch_index, branch.identity, value), {
            'kind': 'union', 'branch': plan.branch_index, 'child': child}
    if op == 'record':
        return read_record_tracked(cursor, plan, ctx)
    if op in ('array', 'map'):
        return read_collection_tracked(cursor, op, plan, ctx)
    if op == 'fixed':
        start = cursor.tell()
        value = cursor.read_exact(plan.writer.size)
        return value, _wire_leaf(cursor, plan, ctx, start)
    if op == 'enum':
        start = cursor.tell()
        index = read_long(cursor)
        if not 0 <= index < len(plan.writer.symbols):
            raise DecodeError('invalid enum index')
        symbol = plan.writer.symbols[index]
        if symbol in plan.reader.symbols:
            value = symbol
        elif plan.reader.default is not MISSING:
            value = plan.reader.default
        else:
            raise ResolutionError('unknown reader enum symbol: ' + symbol)
        return value, _wire_leaf(cursor, plan, ctx, start)
    start = cursor.tell()
    value = read_primitive(cursor, plan.writer.kind)
    if plan.writer.kind in ('int', 'long', 'float', 'double'):
        value = promote_number(value, plan.reader.kind)
    return value, _wire_leaf(cursor, plan, ctx, start)


def decode_block_tracked(block, plan):
    cursor = ByteCursor(block.payload)
    for index in range(block.count):
        ctx = TrackContext()
        value, proof = read_tracked(cursor, plan, ctx)
        yield TrackedRecord(block.index, index, value, proof, ctx.entries)
    cursor.require_end()


class EvidenceValidator:
    def __init__(self, binding):
        self.binding = dict(binding)
        self.rows = []
        self.block_index = -1
        self.record_index = 0
        self.declared = 0
        self.actual = 0

    def begin_block(self, block):
        if self.actual != self.declared or block.index != self.block_index + 1:
            raise DecodeError('incomplete or unordered block')
        self.block_index = block.index
        self.record_index = 0
        self.declared += block.count

    def add_record(self, record):
        if record.block_index != self.block_index or record.record_index != self.record_index:
            raise DecodeError('record order mismatch')
        source = {'package_id': self.binding['package_id'], 'ordinal': self.actual,
                  'block_index': record.block_index, 'record_index': record.record_index}
        self.rows.append({'source': source, 'value': record.value,
                          'proof': record.proof, 'entries': record.entries})
        self.actual += 1
        self.record_index += 1

    def finish(self):
        if self.actual != self.declared:
            raise DecodeError('batch record count mismatch')
        return EvidenceCandidate(dict(self.binding), tuple(self.rows))
