import json
from labavro.archive.reading import read_result
from labavro.archive.representation import to_export_value

def iter_json_lines(db, result_id):
    result = read_result(db, result_id)
    for row in result['rows']:
        exported = {'result_id': result_id, 'binding': result['binding'],
                    'source': row['source'], 'record': to_export_value(row['value'])}
        yield json.dumps(exported, ensure_ascii=True, separators=(',', ':'), allow_nan=False) + '\n'

def export_result(db, result_id, output):
    count = 0
    for line in iter_json_lines(db, result_id):
        output.write(line)
        count += 1
    return count
