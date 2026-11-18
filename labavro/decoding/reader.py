from dataclasses import dataclass
from labavro.decoding.cursor import ByteCursor
from labavro.decoding.records import read_value

@dataclass(frozen=True)
class DecodedRecord:
    block_index: int
    record_index: int
    value: object

def decode_block(block, plan):
    cursor = ByteCursor(block.payload)
    for index in range(block.count):
        yield DecodedRecord(block.index, index, read_value(cursor, plan))
    cursor.require_end()

def iter_records(blocks, plan):
    for block in blocks:
        yield from decode_block(block, plan)
