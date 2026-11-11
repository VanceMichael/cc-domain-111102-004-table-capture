# 外贸发布页的多级表头结构抽取

这个 Python 项目保存采集侧的来源读取工具和合成资料。代码位于 app，示例输入位于 fixtures，现有读取工具的回归位于 tests。示例不代表任何真实单位的业务记录。

## 数据与接口

TokenReader 只暴露 HTML 起止标签、文本及源位置，不展开跨格或判定数据表。trade.html 有多级表头、脚注和三类缺失值；nested.html 包含单元格内说明表；malformed.html 保留来源的跨格错误。源位置采用从 1 开始的行及从 0 开始的列。

## 结构化表格抽取（app/tables.py）

在 TokenReader 事件之上构建带源码位置的 DOM，把每个 table 展开为 rowspan/colspan 网格，再把多级表头还原为列维度路径、行表头还原为行维度路径，为每个数据单元格产出一条事实。

关键约束：

- 每个数据单元格关联完整 `row_path` 与 `column_path`，保留原始文本、去脚注数值文本、数值、单位、脚注标记、缺失原因和源位置（网格行列 + 源码行列）。
- 相邻两列即使同名，维度路径不同也是不同事实，不做合并；事实 ID 由表标识、维度路径、原始文本与网格坐标的规范 JSON 哈希得出，换无关容器或空白样式不会改变身份。
- 空白、破折号、“未披露”是 `missing` 下三种不同 `missing_reason`；显式 0 是数值 `value`，互不混淆。
- 单位只取自表题、表头、单位行或单元格自身；单位行按列向后续数据行传播。来源冲突时事实保留全部候选并打 `unit_conflict` 隔离标记，绝不从相邻行借单位；无单位时记 `unit_missing`。
- 非法 rowspan/colspan（0、负数、非整数）与占位重叠判定为整表结构不可解（`structure_solvable=false`），不补零修成矩形、不产出事实；同页其他有效表照常采集。个别单元格无法判读只记 cell 级诊断，不影响整表。
- 页面中的表按信号（数值单元格数、表头列数、表题）区分为 data/note/nested/invalid，主表选择附 `selection_signals` 可复查，绝不取页面第一个 table；信号相同无法决断时给 `ambiguous_primary_table` 诊断。
- 嵌套 table 作为独立对象抽取并记录 `contained_in_table_key`，其文字不混入外层单元格文本。
- `fact_index` 与 `reverse_lookup()` 支持从一条 fact_id 反查 table、网格坐标、源码位置与原始表头路径。
- 输出经 `stable_json()` 固定键序，可稳定比较；事实文件与结构诊断分两个文件输出。

## 本地使用

使用 Python 3.12 或更新版本，当前工具仅依赖标准库。从项目根目录执行：

```sh
python3 -m app.inspect
python3 -I tools/run_tests.py
python3 -m compileall -q app tests tools
python3 -I -c "import sys; sys.path.insert(0,'.'); from app.tables import main; main(['fixtures/trade.html'])"
python3 -I -c "import sys; sys.path.insert(0,'.'); from app.tables import main; main(['fixtures/trade.html','--lookup','<fact_id>'])"
```

读取命令输出当前素材的结构或单次读取结果。运行过程中产生的文件放在 runtime 中，与原始素材分开。公开示例仅使用 .invalid 域名，网络适配时应替换为已授权的来源。

测试以 `python3 -I` 隔离运行（禁用 PYTHONPATH 与用户站点包），fixtures 等下载素材视为不可信数据；runner 显式插入可信项目根目录。
