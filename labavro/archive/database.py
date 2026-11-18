import sqlite3
from contextlib import contextmanager

class ConflictError(ValueError):
    pass

_TABLES = {
    'packages': ('id', 'raw', 'digest', 'schema_json', 'schema_digest'),
    'readers': ('id', 'schema_json', 'digest'),
    'tasks': ('id', 'binding', 'state', 'error_kind', 'error_message', 'result_id'),
    'batches': ('id', 'task_id', 'binding'),
    'result_rows': ('batch_id', 'ordinal', 'source', 'value'),
    'receipts': ('batch_id', 'task_id', 'row_count'),
}

_EVIDENCE_COLUMNS = ('batch_id', 'document')
_EVIDENCE_STATEMENT = ('CREATE TABLE evidence(batch_id TEXT PRIMARY KEY REFERENCES batches(id), '
                       'document TEXT NOT NULL)')

class ArchiveDatabase:
    def __init__(self, path=':memory:'):
        self.connection = sqlite3.connect(path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')
        try:
            self.initialize()
        except BaseException:
            self.connection.close()
            raise

    def initialize(self):
        c = self.connection
        version = c.execute('PRAGMA user_version').fetchone()[0]
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if version == 0:
            if tables:
                raise RuntimeError('unversioned existing database')
            statements = (
                'CREATE TABLE packages(id TEXT PRIMARY KEY, raw BLOB NOT NULL, digest TEXT NOT NULL, schema_json TEXT NOT NULL, schema_digest TEXT NOT NULL)',
                'CREATE TABLE readers(id TEXT PRIMARY KEY, schema_json TEXT NOT NULL, digest TEXT NOT NULL)',
                "CREATE TABLE tasks(id TEXT PRIMARY KEY, binding TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','published','rejected')), error_kind TEXT, error_message TEXT, result_id TEXT)",
                'CREATE TABLE batches(id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL REFERENCES tasks(id), binding TEXT NOT NULL)',
                'CREATE TABLE result_rows(batch_id TEXT NOT NULL REFERENCES batches(id), ordinal INTEGER NOT NULL, source TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(batch_id,ordinal))',
                'CREATE TABLE receipts(batch_id TEXT PRIMARY KEY REFERENCES batches(id), task_id TEXT UNIQUE NOT NULL REFERENCES tasks(id), row_count INTEGER NOT NULL)',
                _EVIDENCE_STATEMENT,
            )
            with self.transaction():
                for statement in statements:
                    c.execute(statement)
                c.execute('PRAGMA user_version=1')
        elif version != 1:
            raise RuntimeError('unsupported database version: ' + str(version))
        for table, expected in _TABLES.items():
            actual = tuple(row[1] for row in c.execute('PRAGMA table_info(' + table + ')'))
            if actual != expected:
                raise RuntimeError('incompatible table: ' + table)
        # Additive migration for pre-evidence v1 archives: the evidence table
        # is created empty when missing; existing rows are never touched.
        evidence_columns = tuple(row[1] for row in c.execute('PRAGMA table_info(evidence)'))
        if not evidence_columns:
            with self.transaction() as t:
                t.execute(_EVIDENCE_STATEMENT)
        elif evidence_columns != _EVIDENCE_COLUMNS:
            raise RuntimeError('incompatible table: evidence')

    @contextmanager
    def transaction(self):
        c = self.connection
        c.execute('BEGIN IMMEDIATE')
        try:
            yield c
            c.execute('COMMIT')
        except BaseException:
            if c.in_transaction:
                c.execute('ROLLBACK')
            raise

    def close(self):
        self.connection.close()
