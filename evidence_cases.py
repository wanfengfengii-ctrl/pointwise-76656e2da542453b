"""Public hand-worked Avro evidence cases; no evidence producer is supplied."""
import argparse
import copy
import importlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.fixtures import container, WRITER, READER, PAYLOAD
from labavro.archive.database import ArchiveDatabase, ConflictError
from labavro.archive.reading import read_result
from labavro.archive.representation import to_export_value
from labavro.processing.execution import execute
from labavro.processing.tasks import create_task, get_task
from labavro.sources.packages import register_package
from labavro.schemas.catalog import register_reader
from labavro.schemas.types import UnionValue
from labavro.decoding.cursor import DecodeError

def f(name):
    return ["field", name]

def b(index):
    return ["branch", index]

def k(name):
    return ["key", name]

def expected(path, reader_type, writer_path=None, writer_type=None, span=None, raw_hex=None):
    return dict(path=path, reader_type=reader_type,
                origin="default" if writer_path is None else "wire",
                writer_path=writer_path, writer_type=writer_type,
                span=span, raw_hex=raw_hex)

def cases():
    child = dict(next=UnionValue(0, "null", None),
                 signal=UnionValue(1, "double", 5.0), note="checked")
    day_rows = [dict(next=UnionValue(1, "center.Record", child),
                     signal=UnionValue(0, "int", 5), note="checked"),
                dict(next=UnionValue(0, "null", None),
                     signal=UnionValue(1, "double", 9.0), note="checked")]
    day_entries = [
        [expected([f("next"), b(1), f("next"), b(0)], "null",
                  [f("next"), b(1), f("next"), b(0)], "null", [12, 12], ""),
         expected([f("next"), b(1), f("signal"), b(1)], "double",
                  [f("next"), b(1), f("reading"), b(1)], "long", [10, 11], "0a"),
         expected([f("next"), b(1), f("note")], "string"),
         expected([f("signal"), b(0)], "int",
                  [f("reading"), b(0)], "int", [6, 7], "0a"),
         expected([f("note")], "string")],
        [expected([f("next"), b(0)], "null", [f("next"), b(0)], "null", [18, 18], ""),
         expected([f("signal"), b(1)], "double", [f("reading"), b(1)], "long", [16, 17], "12"),
         expected([f("note")], "string")]]
    ew = {"type": "enum", "name": "Grade", "symbols": ["OK", "NEW"]}
    er = {"type": "enum", "name": "Grade", "symbols": ["OK", "OTHER"], "default": "OTHER"}
    writer = {"type": "record", "name": "Evidence", "fields": [
        {"name": "private", "type": {"type": "array", "items": "string"}},
        {"name": "observations", "type": {"type": "map", "values": ["int", "long"]}},
        {"name": "grade", "type": ew},
        {"name": "empty", "type": {"type": "array", "items": "int"}},
        {"name": "void", "type": "null"}]}
    reader = {"type": "record", "name": "Evidence", "fields": [
        {"name": "readings", "aliases": ["observations"],
         "type": {"type": "map", "values": ["int", "double"]}},
        {"name": "grade", "type": er},
        {"name": "empty", "type": {"type": "array", "items": "int"}},
        {"name": "void", "type": "null"},
        {"name": "note", "type": "null", "default": None}]}
    # 0..18 private negative string block; 18..30 map: z first, a second.
    # 30 enum; 31 empty array terminator; 32 explicit null (zero bytes).
    payload = bytes.fromhex("011e1c4c41425f505249564154455f3733000108027a020a020261000a000200")
    mapped = dict(readings={"a": UnionValue(0, "int", 5),
                            "z": UnionValue(1, "double", 5.0)},
                  grade="OTHER", empty=[], void=None, note=None)
    mapped_entries = [
        expected([f("readings"), k("a"), b(0)], "int",
                 [f("observations"), k("a"), b(0)], "int", [28, 29], "0a"),
        expected([f("readings"), k("z"), b(1)], "double",
                 [f("observations"), k("z"), b(1)], "long", [23, 24], "0a"),
        expected([f("grade")], "Grade", [f("grade")], "Grade", [30, 31], "02"),
        expected([f("empty")], "array", [f("empty")], "array", [31, 32], "00"),
        expected([f("void")], "null", [f("void")], "null", [32, 32], ""),
        expected([f("note")], "null")]
    empty = {"type": "record", "name": "Defaults", "fields": []}
    default_reader = {"type": "record", "name": "Defaults", "fields": [
        {"name": "items", "type": {"type": "array", "items": {"type": "map", "values": "int"}},
         "default": [{"z": 1, "a": 2}, {}]}]}
    default_entries = [
        expected([f("items"), ["index", 0], k("a")], "int"),
        expected([f("items"), ["index", 0], k("z")], "int"),
        expected([f("items"), ["index", 1]], "map")]
    return [
        dict(id="recursive", writer=copy.deepcopy(WRITER), reader=copy.deepcopy(READER),
             payload=PAYLOAD, count=2, values=day_rows, entries=day_entries),
        dict(id="map_enum", writer=writer, reader=reader, payload=payload, count=1,
             values=[mapped], entries=[mapped_entries], secret="LAB_PRIVATE_73"),
        dict(id="default_tree", writer=empty, reader=default_reader, payload=b"", count=2,
             values=[{"items": [{"a": 2, "z": 1}, {}]} for _ in range(2)],
             entries=[copy.deepcopy(default_entries) for _ in range(2)]),
        dict(id="erased_union", writer=["int", "long"], reader="double",
             payload=bytes.fromhex("008a00020a"), count=2, values=[5.0, 5.0],
             entries=[[expected([], "double", [b(0)], "int", [1, 3], "8a00")],
                      [expected([], "double", [b(1)], "long", [4, 5], "0a")]]),
        dict(id="empty_record", writer=empty, reader=empty, payload=b"", count=2,
             values=[{}, {}], entries=[[expected([], "Defaults", [], "Defaults", [0, 0], "")] for _ in range(2)])
    ]

def bind(db, case, suffix="", payload=None):
    ident = case["id"] + suffix
    raw = container(case["writer"], [case["payload"] if payload is None else payload], [case["count"]])
    register_package(db, "p:" + ident, raw)
    register_reader(db, "r:" + ident, case["reader"])
    create_task(db, "t:" + ident, "p:" + ident, "r:" + ident)
    return "t:" + ident

def normalized(value):
    return json.dumps(to_export_value(value), ensure_ascii=True, separators=(",", ":"), allow_nan=False)

def baseline():
    for case in cases():
        db = ArchiveDatabase()
        try:
            task = bind(db, case)
            result = read_result(db, execute(db, task).result_id)
            assert normalized([row["value"] for row in result["rows"]]) == normalized(case["values"]), case["id"]
            for entries in case["entries"]:
                for entry in entries:
                    if entry["origin"] == "wire":
                        start, stop = entry["span"]
                        assert case["payload"][start:stop].hex() == entry["raw_hex"], entry
            print("EVIDENCE_BASELINE " + case["id"] + " " + normalized(case["values"]))
        finally:
            db.close()

def new_api():
    execution = importlib.import_module("labavro.processing.execution")
    reading = importlib.import_module("labavro.archive.reading")
    exporting = importlib.import_module("labavro.archive.export")
    replaying = importlib.import_module("evidence_replay")
    return execution.execute_evidenced, reading.read_evidence, exporting.export_evidence, replaying.replay

def check_document(case, result, document, replay):
    assert document["result_id"] == result["result_id"]
    assert document["binding"] == result["binding"]
    assert document["receipt"] == result["receipt"]
    assert document["writer_schema"] == case["writer"]
    assert document["reader_schema"] == case["reader"]
    assert len(document["rows"]) == len(result["rows"])
    for row, original, wanted in zip(document["rows"], result["rows"], case["entries"]):
        assert row["source"] == original["source"]
        actual = {json.dumps(entry["path"]): entry for entry in row["entries"]}
        assert len(actual) == len(row["entries"]) == len(wanted)
        for entry in wanted:
            found = actual[json.dumps(entry["path"])]
            assert all(found[key] == value for key, value in entry.items()), (entry, found)
        assert "proof" in row
    # This comparison checks value types, order, union tags, bytes and nonfinite export spelling.
    wanted_rows = [dict(source=row["source"], record=to_export_value(row["value"])) for row in result["rows"]]
    assert json.dumps(replay(copy.deepcopy(document)), separators=(",", ":"), allow_nan=False) == json.dumps(wanted_rows, separators=(",", ":"), allow_nan=False)
    if "secret" in case:
        serialized = json.dumps(document, ensure_ascii=True).lower()
        assert case["secret"].lower() not in serialized
        assert case["secret"].encode().hex() not in serialized
    return wanted_rows

def exercise(db, case, demonstrate=False):
    run, read, export, replay = new_api()
    task = bind(db, case)
    receipt = run(db, task)
    result = read_result(db, receipt.result_id)
    assert normalized([row["value"] for row in result["rows"]]) == normalized(case["values"])
    document = read(db, receipt.result_id)
    check_document(case, result, document, replay)
    output = io.StringIO()
    assert export(db, receipt.result_id, output) == receipt.row_count
    assert json.loads(output.getvalue()) == document
    another = io.StringIO()
    export(db, receipt.result_id, another)
    assert another.getvalue() == output.getvalue()
    document["rows"].clear()
    assert len(read(db, receipt.result_id)["rows"]) == receipt.row_count
    assert run(db, task) == execute(db, task) == receipt
    if demonstrate:
        print(output.getvalue())
        print(json.dumps(replay(json.loads(output.getvalue())), ensure_ascii=True, allow_nan=False))
    return task, receipt, output.getvalue()

def joint_checks():
    run, read, export, replay = new_api()
    for case in cases():
        db = ArchiveDatabase()
        try:
            exercise(db, case)
        finally:
            db.close()
    case = cases()[1]
    # Real SQL failure after row insertion; failure is not a business rejection.
    db = ArchiveDatabase()
    try:
        task = bind(db, case)
        db.connection.execute("CREATE TRIGGER fail_evidence_receipt BEFORE INSERT ON receipts BEGIN SELECT RAISE(ABORT,'receipt failure'); END")
        try:
            run(db, task)
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("receipt failure did not propagate")
        assert get_task(db, task).state == "pending"
        for table in ("batches", "result_rows", "receipts"):
            assert db.connection.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
        try:
            read(db, "result:" + task)
        except KeyError:
            pass
        else:
            raise AssertionError("evidence visible after rollback")
        db.connection.execute("DROP TRIGGER fail_evidence_receipt")
        receipt = run(db, task)
        check_document(case, read_result(db, receipt.result_id), read(db, receipt.result_id), replay)
        assert run(db, task) == receipt
        before_output_failure = read(db, receipt.result_id)
        class BrokenSink:
            def write(self, text):
                raise OSError("evidence sink failure")
        try:
            export(db, receipt.result_id, BrokenSink())
        except OSError:
            pass
        else:
            raise AssertionError("output failure did not propagate")
        assert read(db, receipt.result_id) == before_output_failure
        register_package(db, "p:empty", container("int", []))
        register_reader(db, "r:empty", "long")
        create_task(db, "t:empty", "p:empty", "r:empty")
        empty_receipt = run(db, "t:empty")
        empty_document = read(db, empty_receipt.result_id)
        assert empty_receipt.row_count == 0 and empty_document["rows"] == []
        assert replay(empty_document) == []
        old_task = bind(db, case, ":old")
        old_receipt = execute(db, old_task)
        for operation in (lambda: run(db, old_task), lambda: read(db, old_receipt.result_id),
                          lambda: export(db, old_receipt.result_id, io.StringIO())):
            try:
                operation()
            except ConflictError:
                pass
            else:
                raise AssertionError("V1 result silently augmented")
        broken = bind(db, case, ":broken", payload=case["payload"][:-1])
        for _ in range(2):
            try:
                run(db, broken)
            except DecodeError:
                pass
            else:
                raise AssertionError("corrupt batch accepted")
        assert get_task(db, broken).state == "rejected"
        try:
            read(db, "result:" + broken)
        except KeyError:
            pass
        else:
            raise AssertionError("rejected task has evidence")
    finally:
        db.close()
    # Requires a writable clean copy; deliberately not simulated by an in-memory database.
    with tempfile.TemporaryDirectory(prefix="labavro-evidence-") as directory:
        path = str(Path(directory) / "archive.sqlite")
        db = ArchiveDatabase(path)
        try:
            task, receipt, original = exercise(db, cases()[0])
        finally:
            db.close()
        db = ArchiveDatabase(path)
        try:
            def deny_inputs(action, table, *unused):
                return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table in ("packages", "readers") else sqlite3.SQLITE_OK
            db.connection.set_authorizer(deny_inputs)
            assert run(db, task) == receipt
            document = read(db, receipt.result_id)
            output = io.StringIO()
            export(db, receipt.result_id, output)
            assert output.getvalue() == original
            assert replay(document)
            replay_file = Path(directory) / "evidence_replay.py"
            replay_file.write_bytes((Path(__file__).resolve().parent / "evidence_replay.py").read_bytes())
            input_file = Path(directory) / "evidence.json"
            input_file.write_text(original, encoding="utf-8")
            detached = subprocess.run([sys.executable, "-I", "-B", str(replay_file), str(input_file)],
                                      cwd=directory, text=True, capture_output=True, check=True)
            assert json.loads(detached.stdout) == replay(document)
        finally:
            db.close()

def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("baseline", "verify", "demo"))
    args = parser.parse_args(argv)
    if args.mode == "baseline":
        baseline()
    elif args.mode == "verify":
        suite = unittest.defaultTestLoader.discover("tests")
        if not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():
            return 1
        baseline()
        joint_checks()
    else:
        db = ArchiveDatabase()
        try:
            exercise(db, cases()[0], True)
            exercise(db, cases()[1], True)
        finally:
            db.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
