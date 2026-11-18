# 实验字段来源凭据：归属说明与独立核账

本文档是凭据特性的交付说明：出现归属、结构裁剪、缺省与发布交接的不变量，proof 格式的完整定义，以及独立重放器的核验清单。规则基础见 docs/rules.md，调用契约见 EVIDENCE.md。

## 出现归属不变量

- 每行统一实验行的每个读端标量叶值（含 null、enum、fixed）和每个无子项的 record/array/map，在该行 entries 中恰好出现一次；非空容器与 union 包装不占 entries。
- 一次输出出现由五元组定位：读端路径、读端类型 identity、来源（wire/default）、写端具体值路径与类型、块内半开跨度及原编码。路径段为 ["field",名]、["index",序位]、["key",原键]、["branch",分支序]；写端路径用写端分支序，不用读端索引指代。
- 同一递归模式、同名类型或同值的多次出现各自独立：路径含字段名、序位、键与分支，行来源含 package_id、ordinal、block_index、record_index；两个零字节记录即使路径、跨度、值全同也各保留一行一项。
- 显式 null 是 wire 来源、零长跨度、空编码；读端缺省是 default 来源，不携带写端路径、跨度或编码。enum 未知符号回退仍标 wire，保留原枚举索引编码，读端 enum default 的选择由冻结双模式解释，不与缺失字段混同。
- 跨度相对来源块 payload 起点、从 0 半开，覆盖该写端叶值自身完整编码（string/bytes 含长度前缀，enum 含原索引），不含外围 union 选择器；空 array/map 覆盖完整集合编码（终止符），空 record 与 null 允许 start=end。

## 文档与 proof 格式

凭据文档为单个有限 JSON 对象，发布时一次物化并随成果持久化；导出文本即持久文本加换行，重复导出逐字节相同。序列化固定为 ensure_ascii、键排序、紧凑分隔符、禁止 NaN/Infinity 字面量。

```text
{"result_id": 成果ID("result:"+任务ID), "binding": 固定绑定, "receipt": {"task_id","row_count"},
 "writer_schema": 冻结写端模式JSON, "reader_schema": 冻结读端模式JSON,
 "rows": [{"source": 来源对象, "entries": [检查视图项], "proof": 证明树}]}
```

entries 项含且仅含七个标准键（path、reader_type、origin、writer_path、writer_type、span、raw_hex），按解码顺序（写字段序、写 map 键序）确定排列。proof 是一棵与读端值树同构的 JSON 树，节点定义如下（除所列键外无其他键）：

| kind | 键 | 含义 |
| --- | --- | --- |
| value | span, raw | 写端标量叶：跨度与小写十六进制原编码 |
| default | literal | 缺石子树：冻结读端修订中该字段的缺省字面量 |
| record | fields:[[读端字段名,子节点]…]（读端字段序）；仅当读端 record 无字段时另带 span、raw（零长） | record 出现 |
| array | items:[子节点…]（写顺序）、blocks；仅空数组另带 span、raw | 数组出现 |
| map | entries:[[原键,子节点]…]（写顺序）、blocks；仅空 map 另带 span、raw | map 出现 |
| union | branch, child | 读端 union 包装：首个兼容读端分支序 |
| selector | span, raw, branch, child | 写端 union 选择器：自身编码、跨度与选中写端分支序 |

blocks 项为 {"count":正整数} 或 {"count":正整数,"size":非负字节长}（负计数块），按写顺序排列；元素按序位依次归属各块。空容器的 raw 是其完整集合编码（终止符），空 record 的 raw 为空串、跨度零长且位于记录起点，不覆盖被跳过的写字段。record 结构由冻结双模式与出现关系解释，故非空 record 不带跨度。

## 结构裁剪不变量

凭据只携带获接纳出现及其必要祖先结构的编码：接纳叶值的原编码、空容器终止符、写端 union 选择器、集合块计数与负计数长度、map 键与顺序、缺省字面量，以及完整双模式、路径、跨度等结构事实。被读端丢弃的字段值、其子树载荷、父级跨度、整条原记录/块/包一律不交付；不以压缩、转义等可逆包装绕过。重放方不重新证明被丢弃字节曾通过完整校验，该责任留在原整批执行端。

## 缺省与发布交接不变量

- 行、凭据、冻结双模式、绑定、回执与任务 published 状态在同一事务提交；解码、计算或发布失败不留下本次部分成果或凭据，SQL 失败保持 pending，重试只发布一次。
- execute_evidenced 与 execute 共享成果身份：任一入口发布后，两个入口对同任务都只读同一原回执。旧入口发布的成果没有凭据时，execute_evidenced、read_evidence、export_evidence 均抛 ConflictError，不新增成果、不补发、不重解码。
- read_evidence、export_evidence 及已发布任务重放只访问成果的持久表示（batches/result_rows/receipts/evidence/tasks），不读 packages/readers，不重新解析输入或生成凭据；新登记读端、修改传入或返回容器、关闭重开均不改变已有交付；凭据导出中途输出异常不修改持久成果。
- 数据库 user_version 保持 1；evidence 表对旧库加性迁移（缺失则建空表），旧成果、原结构校验与原接口不变。

## 独立重放核验清单

evidence_replay.py 可单独复制给接收方，仅依赖标准库，不导入 labavro。replay(document) 仅靠文档内冻结双模式、proof 树与原编码重建实验行，并逐项核对：

1. 文档结构：result_id 与 receipt.task_id 的派生关系、row_count 与 rows 等长、来源对象字段与 ordinal 连续序。
2. 绑定一致：写读模式按规范 JSON（键排序、紧凑、ensure_ascii、有限）的 SHA-256 分别等于 binding 中两个模式摘要。
3. 双模式合法性：命名、别名、引用、联合、缺省全部按 docs/rules.md 重新校验。
4. 解析计划：字段直名/别名匹配、命名类型别名、数值提升、读端首个兼容分支，独立重算。
5. proof 走查：节点 kind 与计划匹配；选择器原编码完整解码且等于声称分支；跨度非负半开且长度等于原编码字节数；叶值原编码按写端类型恰好耗尽地解码，再按提升/enum 回退规则转换；缺省字面量与读端模式一致并按物化规则生成；集合块计数之和等于元素数、负计数长度非负、map 键唯一；空容器编码恰好是零终止符。
6. entries 与 proof 一致：两边各自导出七键检查视图后逐项相等，不允许互相矛盾或缺漏。

重放不核验：被丢弃字节曾通过完整校验（原执行端责任），也不证明编码确实出自声明的原包（不做密码学认证）。无效文档抛 ValueError；命令行 `python3.11 -B evidence_replay.py evidence.json` 将重放结果作为一个 JSON 值写标准输出，失败非零退出。

## 独立核账

- `python3.11 -B evidence_cases.py verify`：完整原回归（含 tests/test_evidence.py 的双入口一致、proof 结构、篡改拒绝、迁移与回滚边界）+ 五组手算基线 + 联合核验（真实 SQL 失败回滚重试、损坏拒收、旧无凭据成果边界、修改返回值、磁盘重开、禁读输入目录、干净目录单文件子进程重放）。
- `python3.11 -B evidence_cases.py demo`：展示递归与 map 重排两组真实凭据文档及独立重放结果。
- 手算输入与预期见 evidence_cases.py；凭据生成不调用重放器，重放器不调用生产解码，两侧只共享文档契约。
