# 外贸发布页的多级表头结构抽取

这个 Python 项目保存采集侧的来源读取工具和合成资料。代码位于 app，示例输入位于 fixtures，现有读取工具的回归位于 tests。示例不代表任何真实单位的业务记录。

## 数据与接口

TokenReader 只暴露 HTML 起止标签、文本及源位置，不展开跨格或判定数据表。trade.html 有多级表头、脚注和三类缺失值；nested.html 包含单元格内说明表；malformed.html 保留来源的跨格错误。源位置采用从 1 开始的行及从 0 开始的列。

## 本地使用

使用 Python 3.12 或更新版本，当前工具仅依赖标准库。从项目根目录执行：

```sh
python3 -m app.inspect
python3 -m unittest discover -s tests -v
python3 -m compileall -q app tests
```

读取命令输出当前素材的结构或单次读取结果。运行过程中产生的文件放在 runtime 中，与原始素材分开。公开示例仅使用 .invalid 域名，网络适配时应替换为已授权的来源。
