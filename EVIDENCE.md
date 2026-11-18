# 实验字段来源凭据

研究员需要知道统一行的每次输出出现来自哪一次原始出现，并只拿到获接纳的数据。V1 已有正常转换、固定绑定和共同发布；本次新增凭据生成、持久交付及接收方独立重放。README.md、docs/rules.md 和全部原测试保持适用。evidence_cases.py 只提供有限输入和手算预期，不含凭据生成器或重放器。

## 调用与交付

新增公开函数，db 与 task/result 身份沿用原 API：

- labavro.processing.execution.execute_evidenced(db, task_id)：返回原 PublicationReceipt。pending 任务完整解码、验证并共同发布实验行与凭据；普通 read_result/export_result 对新成果仍给出原格式。
- labavro.archive.reading.read_evidence(db, result_id)：返回下述独立文档，每次返回独立容器。
- labavro.archive.export.export_evidence(db, result_id, output)：向文本 write 对象输出一个完整 JSON 文档及换行，返回成果行数；相同成果重复导出的文本相同。JSON 不使用 NaN/Infinity 数字字面量。
- evidence_replay.py 提供 replay(document)，返回来源序列表，每项为 {"source": 原来源对象, "record": 原 to_export_value 格式的实验值}。该文件可以单独复制给接收方，只依赖 Python 标准库，不导入 labavro；运行 python3.11 -B evidence_replay.py evidence.json 时将同一列表作为一个 JSON 值写到标准输出。仅靠文档内原编码、双模式及上下文重新构造实验行，不能读数据库、原包、预先解码的行或调用生产解码器。无效文档抛 ValueError；命令失败非零退出。不要求证明给定编码确实出自声明的原包。

execute_evidenced 成功后，execute 对同任务只读同一原回执；execute_evidenced 重放也只读原回执。若同任务已由旧入口发布且没有凭据，execute_evidenced、read_evidence、export_evidence 均抛现有 ConflictError，不新增成果、不补发、不重解码。未知任务/成果或尚未发布的成果抛 KeyError；已有拒收任务沿原分类重放。模式/编码拒收、SQL 异常及输出异常分别沿用 V1 语义。空批也能发布具有双模式和空 rows 的凭据文档。

行、凭据、冻结双模式、绑定、回执和任务 published 状态共同提交；解码、计算或发布失败不留下本次部分成果或凭据，SQL 失败保持 pending，重试只发布一次。保留旧库、旧成果、原结构校验和原接口。读取、导出及已发布任务重放只访问成果的持久表示，不再读取 packages/readers，也不重新解析输入或生成凭据。新登记读端、更改传入或返回容器及关闭重开不改变已有交付。凭据导出中途输出异常不修改持久成果。

## 文档外部格式

文档为有限 JSON 对象：
```text
{
  "result_id": 原成果ID,
  "binding": 原固定绑定,
  "receipt": {"task_id": 原任务ID, "row_count": 行数},
  "writer_schema": 冻结写端模式JSON,
  "reader_schema": 冻结读端模式JSON,
  "rows": [
    {"source": 原来源对象, "entries": [下述检查视图项], "proof": 自定的重放证据}
  ]
}
```

rows 与 read_result 的来源序完全一致，来源对象包含 package_id、ordinal、block_index、record_index。即便两个零字节记录的值、路径和跨度完全相同，也各保留一项。proof 的表示由实现选择，必须在交付说明中完整说明，支持独立解读，不能只是最终行或其编码副本；可用树、引用表或其他闭合表示。标准检查视图 entries 不限定内部生产表示。entries 和 proof 可以重复合法依据，但不能互相矛盾；接收方重放要消费原编码并依双模式核对转换依据，不能直接回显声称的最终值。

每个 entries 项都含以下键，允许增加已说明的依据字段：

| 键 | 含义 |
| --- | --- |
| path | 输出出现的读端路径 |
| reader_type | 读端类型 identity：命名类型用完整名，其余用类型名 |
| origin | wire 或 default |
| writer_path | wire 对应的写端具体值路径；default 为 null |
| writer_type | wire 对应写端类型 identity；default 为 null |
| span | wire 的 [start,end]；default 为 null |
| raw_hex | wire 对应跨度的原始字节小写十六进制；default 为 null |

path/writer_path 都是带类型的路径段列表。根为 []，record 字段为 ["field",字段名]，数组为 ["index",从0下标]，map 为 ["key",原键]；穿过 union 后加 ["branch",对应模式的分支索引]。这是逻辑路径，不用读端联合索引指代写端联合索引。读端新增 union 包装也出现在 path；写端原有 union 即使读端消去包装，也出现在 writer_path。

entries 恰好覆盖每个读端标量叶值（含 null、enum、fixed）及每个没有子项的 record/array/map，各一次；非空容器和 union 包装本身不另占 entries。顺序可自定，但导出须确定。span 相对于对应来源块的 payload 起点，半开且从0计，不含容器文件头或块头。它覆盖该写端叶值自身的完整编码：string/bytes 包含长度前缀，enum 包含原索引；不含外围 union 选择器。空 array/map 覆盖完整集合编码及终止符；空 record 和 null 的跨度允许 start=end，raw_hex 为 ""。记录号仍须区分同位置的不同出现。

default 是读端字段缺失后补入的值，所有后代叶值和空容器均标 default，不伪造 writer_path、跨度或编码。proof 须定位冻结读端修订中的缺省字段定义及其后代位置，重放按该定义生成值。显式 null 标 wire 且保留零长度来源，不能作为 default。enum 未知符号回退仍标 wire：保留原枚举索引，并解释读端 enum default 的选择，不能与缺失字段混同。

## 必要结构、解释与裁剪

凭据保留实际写端联合选择器的原编码、跨度、选中类型，以及对应读端首个兼容分支；通过父级上下文关联到具体出现。即便同值、同类型名或同一递归模式被多处使用，也不能共享错误的出现身份。非联合写端产生读端联合时，不能伪造写端选择器。

凭据还须保留获接纳集合的块计数、负计数长度、map键与顺序以及空容器的结构依据，使接收方能还原数组顺序、map键集和各元素所属块；数值跨度仍指原物理位置，不按输出排序重排。record 结构由固定双模式和出现关系解释。别名匹配（含字段与命名类型）、数值提升、枚举回退及缺省须能从双模式、路径和证据核对，独立重放沿用 docs/rules.md 的精确数值与联合规则，包括 bytes、浮点精度、负零及非有限值的原导出语义。

不得交付读端丢弃字段的值、原编码载荷或携带它们的整条原记录、原块、原包；压缩、转义等可逆包装也不能绕过载荷裁剪。完整双模式、模式中的字面量、路径、跨度及计数可公开，不承诺隐藏结构信息。只保留获接纳出现及其必要祖先结构的编码；不为被丢弃子树导出内部载荷。不能把原始跨度直接当成可交付载荷：一个父跨度可能包含丢弃字段。重放不需要重新证明被丢弃字节曾通过完整校验，该责任仍在原整批执行端。

可全量重算和物化，复用原兼容计划及事务；不新增性能、并发、压缩、恢复或密码学认证要求。

## 手算输入及观察

在项目根目录运行 python3.11 -B evidence_cases.py baseline，可用 V1 实际转换核对五组独立预期。该命令不调用新增 API。cases() 返回 writer、reader、原 payload、单块记录数、预期值和 entries；用 tests.fixtures.container 仅封装手算载荷。

recursive 使用原日样例的两条记录。第一条父 reading 的原值字节为 [6,7)，写端分支 int；子 reading 为 [10,11)，写端分支 long，读端为 double；值同为5。子 next 的 null 是 [12,12)，父子 note 均为缺省。第二条 reading 为 [16,17)，next 的 null 为 [18,18)。

map_enum 的 private 为负计数字符串数组，包含 LAB_PRIVATE_73；该值及载荷不能随凭据交付。接纳 map 原顺序 z、a，z 的原值在 [23,24)，a 在 [28,29)，输出按 a、z。grade 的原索引 [30,31) 选择 NEW，读端回退 OTHER；空数组 [31,32)，void 显式 null [32,32)，note 为无来源缺省 null。

erased_union 的原始 payload 为 00 8a 00 02 0a；两个记录均输出 double 5.0。第一条写端选 int，值的合法非最短 varint 为 [1,3) 的 8a00；第二条选 long，值为 [4,5) 的 0a。V1 已接纳这两种编码，凭据不能用最终值重新编码冒充原字节。

default_tree 的两个零字节记录各产生独立的嵌套缺省数组和 map，全部叶值及空 map 为 default。empty_record 的两个记录各有一个根 entries，span 均为 [0,0)，但不能合并来源行。

这些是普通小例，不限制合法输入只有这些小例。V1 所有支持类型、递归配对、联合、集合块、跳过校验与异常规则均适用。联合核验还包括完整原回归、真实 SQL 失败后无部分可见/可重试、损坏拒收、旧无凭据成果边界、修改返回值、重开及禁止读取输入目录后读取导出。evidence_cases.py verify 提供这些场景的可执行起点，并在干净临时目录独立运行单文件重放器；应补齐确有必要且不重复的业务边界。其 demo 打印真实凭据与重放结果。

## 交付运行

保留完整原回归并补独立离线核账。说明出现归属、结构裁剪、缺省与发布交接的不变量和 proof 格式。更新已有运行清单，让 verify 执行新旧业务核验、demo 展示递归和 map 重排的真实凭据及独立重放；磁盘重开由 verify 核验。运行清单沿用现有 .pointwise-runtime.json 的 version=1、kind=cli，setup 是准备命令的 argv 数组列表，verify/demo 是命令 argv 数组。已有 run-manifest.json 同步保持可运行；无需新增服务。
