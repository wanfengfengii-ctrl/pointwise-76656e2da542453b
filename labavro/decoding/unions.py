from dataclasses import dataclass
from labavro.schemas.types import UnionValue
from labavro.decoding.cursor import DecodeError
from labavro.decoding.primitives import read_long

@dataclass(frozen=True)
class WriterBranch:
    index: int
    type: object

def select_writer_branch(cursor, writer):
    index = read_long(cursor)
    if not 0 <= index < len(writer.branches):
        raise DecodeError('invalid writer union index')
    return WriterBranch(index, writer.branches[index])

def read_union(cursor, plan, consume):
    branch = select_writer_branch(cursor, plan.writer)
    selected = plan.branches[branch.index]
    if selected.writer is not branch.type:
        raise DecodeError('union plan lost writer identity')
    return consume(cursor, selected)

def skip_union(cursor, writer, consume):
    branch = select_writer_branch(cursor, writer)
    consume(cursor, branch.type)

def wrap_reader_union(plan, value):
    branch = plan.reader.branches[plan.branch_index]
    return UnionValue(plan.branch_index, branch.identity, value)
