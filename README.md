# 北岸实验数据交换归档

Python 3.11 标准库工程。供应实验室交付离线 Avro 对象容器，数据工程师固定中心读端口径并整批转换，研究员读取统一实验行及来源，导出 JSON 行用于分析。只支持 null 编解码，不接入网络。

在项目根目录运行：

```sh
python3.11 -B -m labavro.processing.execution verify
python3.11 -B -m labavro.processing.execution demo
python3.11 -B -m labavro.processing.execution demo --db day.sqlite
python3.11 -B evidence_cases.py verify
python3.11 -B evidence_cases.py demo
```

verify 执行完整 unittest discover。demo 从 samples/day.json 导入实际字节，对照手算预期后导出真实成果。指定项目内数据库路径可封存成果；重复运行不会重置数据。完整命令参数也见 .pointwise-runtime.json 与 run-manifest.json。测试只在测试临时目录创建磁盘数据库。

evidence_cases.py verify 额外执行凭据基线与联合核验（含磁盘重开与独立子进程重放），evidence_cases.py demo 展示递归与 map 重排的真实凭据及独立重放；两者是运行清单中的现行 verify/demo 命令。凭据特性见 EVIDENCE.md 与 docs/evidence.md。

## 一天的工作

1. 工程师收到包 ID 和容器字节，用 register_package 封存；校验头部、写端模式并保存原字节摘要。
2. 登记中心口径 register_reader；名称、引用、缺省全部合法后成为不可变修订。
3. create_task 固定包及读端修订，进入 pending；后来登记修订不会换绑。
4. execute 根据固定写读类型对建立递归计划。
5. 按写端字段和联合顺序消费字节，核对所有块边界，按读端口径构造统一行；暂存不对研究员可见。
6. 成功后同一事务发布行、来源、回执和 published 状态。模式或字节错误整批 rejected；SQL 失败保持 pending，可以重试。
7. 研究员 read_result 读取固定成果的独立副本，核对来源，不重新解码。
8. export_result 向文本文件对象导出带联合标签的 JSON 行，供下游分析。

## 公开 Python API

所有入口第一个参数都是 ArchiveDatabase 对象，支持 :memory: 或项目内 SQLite 文件路径。连接由调用方 close。示例：

```python
import json
from pathlib import Path
from labavro.archive.database import ArchiveDatabase
from labavro.sources.packages import register_package
from labavro.schemas.catalog import register_reader
from labavro.processing.tasks import create_task
from labavro.processing.execution import execute
from labavro.archive.reading import read_result
from labavro.archive.export import export_result

sample = json.loads(Path("samples/day.json").read_text())
db = ArchiveDatabase("day.sqlite")
try:
    register_package(db, "supply", bytes.fromhex(sample["container_hex"]))
    register_reader(db, "center-v1", sample["reader_r1"])
    create_task(db, "import-1", "supply", "center-v1")
    receipt = execute(db, "import-1")
    result = read_result(db, receipt.result_id)
    with open("result.jsonl", "w", encoding="utf-8") as output:
        export_result(db, receipt.result_id, output)
finally:
    db.close()
```

| 入口（模块） | 成功结果 | 错误 |
| --- | --- | --- |
| register_package(db, package_id, raw)，sources/packages.py | SourcePackage：id、raw、digest、writer_schema、schema_digest、header | 非法头或写端模式 PackageError；ID 异字节 ConflictError |
| register_reader(db, revision_id, schema)，schemas/catalog.py | ReaderRevision：id、schema、digest | 非法模式或 ID 异模式 SchemaError |
| create_task(db, task_id, package_id, revision_id)，processing/tasks.py | FrozenTask：binding、state、拒收信息、result_id | 缺引用 KeyError；异绑定 ConflictError |
| execute(db, task_id)，processing/execution.py | PublicationReceipt：result_id、task_id、row_count | ResolutionError 或 DecodeError；SQL 异常原样传播 |
| execute_evidenced(db, task_id)，processing/execution.py | 同 execute 的回执；实验行与凭据共同发布 | 同 execute；旧成果无凭据时 ConflictError |
| read_result(db, result_id)，archive/reading.py | result_id、binding、receipt、rows | 未发布或未知成果 KeyError |
| read_evidence(db, result_id)，archive/reading.py | 凭据文档独立副本：双模式、逐行 entries 与 proof | 未知成果 KeyError；无凭据 ConflictError |
| export_result(db, result_id, output)，archive/export.py | 写出的行数 | 缺成果 KeyError；输出异常原样传播 |
| export_evidence(db, result_id, output)，archive/export.py | 成果行数；写出一个完整 JSON 文档及换行 | 同 read_evidence；输出异常原样传播 |

execute_evidenced 发布的成果用 read_result/export_result 读取仍是原格式。evidence_replay.py 是脱离本包的独立重放器，仅凭凭据文档重建实验行，见 docs/evidence.md。

rows 每项含 source 和 value。source 含 package_id、从 0 连续的 ordinal、block_index、record_index。binding 固定 package_id、package_digest、writer_schema_digest、reader_id、reader_digest。receipt 含 task_id 和 row_count。record 为保留字段顺序的 dict，map 按键排序，数组为 list，二进制为 bytes，联合为 schemas.types.UnionValue(branch_index, branch_type, value)。

联合标签使用**读端**分支索引和完整类型名。归档使用独立外层类型封装，不把业务字典中的 type、union 或 value 字段当标记。JSON 导出每行含 result_id、binding、source、record；联合输出 branch_index、branch_type、value；bytes/fixed 输出 type=bytes 和 hex。非有限浮点输出 type=float64 和 NaN/Infinity/-Infinity 字符串；有限浮点与负零按 Python JSON 数值输出，固定读端模式说明其原始类型。

## 状态与失败

SourcePackage 的 sealed 表示原字节已固定，不代表数据块已通过执行。ReaderRevision 不可改写；相同规范 JSON 重复登记幂等。任务只有 pending、rejected、published 三种状态。相同包字节、读端模式及任务绑定重复登记幂等；改变输入须新 ID。所有返回值均与数据库隔离。

头部登记校验与执行期块校验分开。损坏记录、负集合块长度错误、同步标记错误或多余有效载荷均拒收整个任务。没有本批成果、来源或完成回执；原成果保持。已拒收任务重放抛同类异常，不更换模式。SQL 失败不转业务拒收；已发布任务重放只读原回执。输出设备失败不改数据库，允许重新导出。

数据库 user_version=1；新库建表，兼容旧库只检查，不灌样例或清空。未知版本及不兼容结构拒绝打开。

## 模块与回归

26 个生产模块均接入公开业务路径。sources 的 packages/catalog/container/blocks 管封存和容器；schemas 的 catalog/names/types/defaults/resolution 管固定类型与配对；decoding 的 cursor/primitives/collections/unions/records/reader 管写端消费和读端构造；processing 的 tasks/validation/outcomes/execution 管任务和整批状态；archive 的 database/representation/publication/reading/export 管共同发布及固定交付；evidence 的 tracked/document 管带凭据解码与凭据文档发布。详细规则与手算字节见 docs/rules.md，凭据格式与不变量见 docs/evidence.md。

六个测试文件覆盖来源冲突与冻结、递归别名与缺省、联合身份与损坏块、SQL 触发器制造的真实事务失败、重开与导出、凭据一致性与篡改边界。tests/fixtures.py 是独立的有限编码夹具，不调用生产解码或解析生成预期。完整 discover 保留所有测试，指定方法的核验只是补充分组。无压缩、逻辑类型、单位换算、外部注册服务、并发调度或流式单记录承诺。
