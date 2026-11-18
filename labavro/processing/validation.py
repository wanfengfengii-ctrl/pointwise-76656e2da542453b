from dataclasses import dataclass
from labavro.decoding.cursor import DecodeError

@dataclass(frozen=True)
class BatchCandidate:
    binding: dict
    rows: tuple

class BatchValidator:
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
        self.rows.append({'source': source, 'value': record.value})
        self.actual += 1
        self.record_index += 1

    def finish(self):
        if self.actual != self.declared:
            raise DecodeError('batch record count mismatch')
        return BatchCandidate(dict(self.binding), tuple(self.rows))
