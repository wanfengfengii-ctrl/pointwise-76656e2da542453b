# 有限 Avro 规则

| 类型 | 写端消费 | 读端接纳 |
| --- | --- | --- |
| null、boolean | null 无字节；boolean 只允许 00/01 | 仅同类型 |
| int、long | zigzag LEB128；分别有符号 32/64 位 | 同型；int→long/float/double，long→float/double |
| float、double | 小端 IEEE-754 binary32/64 | 同型；float→double |
| bytes、string | long 长度后字节；长度非负；string 严格 UTF-8 | 仅同型 |
| fixed | 写端 size 个字节 | 名称匹配且 size 相等 |
| enum | 非负合法符号索引 | 名称匹配；未知符号仅用读端 enum default，否则拒收 |
| record | 写端字段顺序完整消费 | 名称匹配；读端字段顺序构造 |
| array、map | 正负计数多块，零结束；map 键为 string | 同类，逐元素递归解析；map 唯一键、字典序输出 |
| union | 先读合法写端分支索引 | 按该类型选第一个兼容读端分支；保留读端标签 |

不收窄，不作 string/bytes 互转，不换单位。整数转 binary32 直接舍入到 24 位有效二进制数字，最近值、同距取偶，不经 binary64 二次舍入。binary64 的整数转换用同样最近偶数语义。NaN、无穷及负零保持为浮点值，归档按 64 位载体保存其位模式；提升后的值已经符合目标精度。

名称由 name 和 namespace 构成完整名；带点 name 自带命名空间。短引用只在当前命名空间解析，引用须已定义；record 在读取字段前登记自身，因此允许递归。命名类型仅接受写端完整名等于读端完整名，或命中读端完整别名。字段直接名优先于读端字段别名；多个候选或一个写字段被多次认领均拒收。重复名称、重复字段、重复 union 分支类型、直接嵌套 union、悬空引用均 SchemaError。schema 的 logicalType 不受支持。

record 只有写端没有匹配字段时才用读端 default；显式 null 必须解析 null。缺省按字段类型校验，union 缺省必须匹配首分支。enum 缺省必须是其符号。bytes/fixed 缺省为各码点在 0..255 的 JSON 字符串。record 缺省须提供字段值或该字段自己的缺省；不得无限递归。每次构造创建独立数组和对象。模式登记 JSON 必须有限，不允许非标准 NaN/Infinity 常量。

写端联合未选中的不兼容分支不妨碍接纳。兼容按类型关系判断，不凭值；例如写端 [int,long] 两个分支都给值 5，读端 [int,double] 应分别生成 UnionValue(0,int,5) 和 UnionValue(1,double,5.0)。若读端为 [double,long]，写端 int 首先选 double。

数组/映射的负计数块编码为负数量、非负字节长度、该块元素；实际元素消费位置必须恰好等于声明终点。跳过独有字段也逐项校验，不能盲信块长度。正计数块仍逐项消费。对象容器为 Obj01、元数据 map、16 字节 sync，后接各块的正记录数、非负载荷长度、载荷及 sync。缺省 codec 为 null；其他 codec 拒绝。零记录文件无数据块；空 record/null 类型可有零字节载荷的正记录数块。每块必须消费恰好声明记录数和全部载荷，尾部截断或剩余字节拒收。

## 手算日样例

samples/day.json 中容器由独立头部编码和以下手算载荷组成。sync 为 00..0f。数据块记录数为 2，即 04；载荷为 18 字节，即长度 24（十六进制）。

第一条及其递归子记录：

- 03 04 0e 10 00：discard 数组的负数量 -2、字节数 2、整数 7、8、零结束。
- 00 0a：reading 的 int 分支 0，值 5。
- 02：next 的递归分支 1。
- 00 02 0a 00：子记录 discard 为空；reading 为 long 分支 1、值 5；next 为 null 分支 0。

第二条：

- 02 01 00：discard 正数量 1，值 -1，零结束。
- 02 12：reading 为 long 分支 1，值 9。
- 00：next 为 null。

R1 将 next 放在首字段，reading 以别名成为 signal，缺少 note 时补 checked。第一条 signal 是 int 标签，子记录及第二条 signal 是 double 标签。R2 只接纳 reading 为 measurement double，并补 status=reviewed；跳过递归 next 后结果为 5.0、9.0。预期由这些字节和规则手算，未调用生产实现生成。

## 确定性与边界回归

V1/V2：头部和读端校验、内容冲突、修订与任务冻结、初始化幂等。
V3/V5：命名空间、递归不同读端、别名、独立缺省、显式 null、enum 回退与拒收。
V4/V6：相同数值不同联合身份、嵌套跳过、正负多块、数值精度、截断、非法标量与索引、损坏同步和长度。
V7：真实 SQLite 触发器分别在回执写入和任务完成更新时失败，检查已写行一并回滚，再重试一次发布；接纳和拒收重放。
V8：临时磁盘库重开、修改返回容器、新登记读端不漂移；用 SQLite authorizer 禁止读取输入目录时仍能重放、读取、导出。输出设备中途失败后持久成果保持，正常重导出一致。
