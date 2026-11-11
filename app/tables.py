"""结构化表格抽取。

在 ``app.markup`` 的标签事件之上构建轻量 DOM，把每个 table 展开为带
rowspan/colspan 的网格，再把多级表头还原为列维度路径、行表头还原为行维度
路径，最终为每个数据单元格产出一条数值事实（fact）。

设计约束：
- 不补零、不猜结构：非法跨格与重叠跨格判定为整表不可解，同页其他表照常采集；
- 空白、破折号、“未披露”、显式 0 是不同状态，互不混淆；
- 单位只取自表题、表头、单位行或单元格自身，冲突时隔离事实并记录全部候选，
  绝不从相邻行借单位；
- 事实身份只由结构性内容决定，容器、样式、源码行号变化不会改变 fact_id。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.markup import tokens

# ---------------------------------------------------------------- 常量

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
ROW_GROUPS = ("thead", "tbody", "tfoot")

DASH_TOKENS = {"—", "–", "−", "-", "－", "—"}
UNDISCLOSED_TOKENS = {"未披露"}

# 带千分位与小数的数字，不含单位
NUMBER_RE = re.compile(r"^[+-]?[0-9][0-9,]*(?:\.[0-9]+)?$")
# 数字 + 可选尾随单位
CELL_VALUE_RE = re.compile(
    r"^\s*([+-]?[0-9][0-9,]*(?:\.[0-9]+)?)\s*"
    r"(亿美元|万美元|亿元|万元|亿|万|元|%|％)?\s*$"
)
# 单位行允许的纯单位记号
UNIT_TOKEN_RE = re.compile(r"^(?:%|％|[亿万千百]*元)$")
SPAN_RE = re.compile(r"^[1-9][0-9]*$")
HEADER_PAREN_RE = re.compile(r"[（(]([^（）()]+)[）)]\s*$")
CAPTION_PAIR_RE = re.compile(r"[（(]([^（）()：:]+)[：:]([^（）()]+)[）)]")
CAPTION_UNIT_RE = re.compile(r"[（(]([^（）()]+)[）)]")
FOOTNOTE_DEF_RE = re.compile(r"^\s*([0-9]+)\s*[：:、.．]\s*(.+)$")
WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------- DOM

@dataclass
class TextNode:
    data: str
    line: int
    column: int
    parent: "Element | None" = None


@dataclass
class Element:
    tag: str
    attrs: dict
    line: int
    column: int
    children: list = field(default_factory=list)
    parent: "Element | None" = None

    def find_children(self, tag):
        return [c for c in self.children if isinstance(c, Element) and c.tag == tag]


def build_tree(html):
    """把 TokenReader 的事件流构建为带源码位置的 DOM，返回虚拟根节点。"""
    root = Element("#root", {}, 0, 0)
    stack = [root]
    for ev in tokens(html):
        if ev.kind == "start":
            node = Element(ev.tag, dict(ev.attrs), ev.line, ev.column,
                           parent=stack[-1])
            stack[-1].children.append(node)
            if ev.tag not in VOID_TAGS:
                stack.append(node)
        elif ev.kind == "end":
            # 向后查找匹配的开始标签；找不到则忽略来源的孤立闭合标签
            for i in range(len(stack) - 1, 0, -1):
                if stack[i].tag == ev.tag:
                    del stack[i:]
                    break
        else:
            stack[-1].children.append(
                TextNode(ev.text, ev.line, ev.column, stack[-1])
            )
    return root


def iter_tables(root):
    """按文档顺序产出 (table 元素, 祖先 table 元素列表)。"""
    found = []

    def walk(node, table_chain):
        for child in node.children:
            if not isinstance(child, Element):
                continue
            if child.tag == "table":
                found.append((child, list(table_chain)))
                walk(child, table_chain + [child])
            else:
                walk(child, table_chain + [child])

    walk(root, [])
    return found


def normalize_ws(text):
    return WS_RE.sub(" ", text).strip()


def _walk_text(node, sink, skip_sup=False, in_sup=False):
    for child in node.children:
        if isinstance(child, TextNode):
            if not in_sup:
                sink.append(child.data)
        elif isinstance(child, Element):
            if child.tag == "table":
                continue  # 嵌套表是独立对象，文本不计入外层单元格
            if in_sup or (skip_sup and child.tag == "sup"):
                _walk_text(child, sink, skip_sup, True)
            else:
                _walk_text(child, sink, skip_sup, False)


def own_text(node, *, exclude_sup=False):
    """节点自身文本（不进入嵌套 table）。exclude_sup 时整段剔除脚注上标。"""
    parts = []
    _walk_text(node, parts, skip_sup=exclude_sup, in_sup=False)
    return normalize_ws("".join(parts))


def raw_cell_text(cell):
    """单元格原始文本，包含脚注上标，排除嵌套表。"""
    return own_text(cell, exclude_sup=False)


def numeric_cell_text(cell):
    """去掉脚注上标后的文本，用于数值判读。"""
    return own_text(cell, exclude_sup=True)


def footnote_markers(cell):
    markers = []
    _collect_sup(cell, markers)
    return markers


def _collect_sup(node, markers):
    if node.tag == "sup":
        text = own_text(node)
        if text:
            markers.append(text)
        return
    for child in node.children:
        if isinstance(child, Element):
            if child.tag == "table":
                continue
            _collect_sup(child, markers)


def location_of(node):
    return {"source_line": node.line, "source_column": node.column}


# ---------------------------------------------------------------- 网格

@dataclass
class PlacedCell:
    cell: Element
    origin_row: int
    origin_col: int
    rowspan: int
    colspan: int

    @property
    def origin(self):
        return self.origin_row, self.origin_col


def table_rows(table):
    """按文档顺序返回 (tr 元素, 行组标签)，嵌套表的 tr 不会混入。"""
    rows = []
    for child in table.children:
        if not isinstance(child, Element):
            continue
        if child.tag in ROW_GROUPS:
            for tr in child.find_children("tr"):
                rows.append((tr, child.tag))
        elif child.tag == "tr":
            rows.append((child, "tbody"))
    return rows


def row_cells(tr):
    return [c for c in tr.children
            if isinstance(c, Element) and c.tag in ("td", "th")]


def parse_span(value):
    """返回 (跨格数, 非法原值)。0、负数、非整数均判为非法。"""
    if value is None:
        return 1, None
    value = value.strip()
    if SPAN_RE.fullmatch(value):
        return int(value), None
    return None, value


def build_grid(table, prefilled=None):
    """展开网格。返回 (placed 列表, grid 字典, 诊断列表, 行数, 列数)。

    诊断中 severity == "table" 的问题意味着整表结构不可解，
    调用方不得据此补零修成矩形。prefilled 仅用于注入来源无法表达的
    既有占位（如上游错误产生的重叠），正常抽取不传。
    """
    rows = table_rows(table)
    grid = dict(prefilled or {})
    placed = []
    diagnostics = []

    for r, (tr, _group) in enumerate(rows):
        c = 0
        for cell in row_cells(tr):
            while (r, c) in grid:
                c += 1
            rowspan, bad_rs = parse_span(cell.attrs.get("rowspan"))
            colspan, bad_cs = parse_span(cell.attrs.get("colspan"))
            if bad_rs is not None or bad_cs is not None:
                diagnostics.append({
                    "severity": "table",
                    "code": "invalid_span",
                    "message": "跨格属性非法，无法确定网格结构",
                    "detail": {
                        "rowspan": cell.attrs.get("rowspan"),
                        "colspan": cell.attrs.get("colspan"),
                    },
                    "location": {**location_of(cell),
                                 "grid_row_index": r,
                                 "grid_col_index": c},
                })
                continue
            pc = PlacedCell(cell, r, c, rowspan, colspan)
            overlap = [
                (r + dr, c + dc)
                for dr in range(rowspan) for dc in range(colspan)
                if (r + dr, c + dc) in grid
            ]
            if overlap:
                diagnostics.append({
                    "severity": "table",
                    "code": "overlapping_cells",
                    "message": "单元格跨格与既有占位重叠",
                    "detail": {"occupied_slots": overlap},
                    "location": {**location_of(cell),
                                 "grid_row_index": r,
                                 "grid_col_index": c},
                })
                continue
            for dr in range(rowspan):
                for dc in range(colspan):
                    grid[(r + dr, c + dc)] = pc
            placed.append(pc)

    width = max((c + 1 for (_r, c) in grid), default=0)
    # 空洞诊断：只记录、不填充
    for r in range(len(rows)):
        for c in range(width):
            if (r, c) not in grid:
                diagnostics.append({
                    "severity": "cell",
                    "code": "grid_hole",
                    "message": "网格存在未被任何单元格覆盖的空位，未做填充",
                    "location": {"grid_row_index": r, "grid_col_index": c},
                })
    return placed, grid, diagnostics, len(rows), width


# ---------------------------------------------------------------- 表头与单位

def header_row_indices(table, rows):
    thead_rows = [r for r, (tr, group) in enumerate(rows) if group == "thead"]
    if thead_rows:
        return thead_rows
    header = []
    for r, (tr, _group) in enumerate(rows):
        cells = row_cells(tr)
        if cells and all(c.tag == "th" for c in cells):
            header.append(r)
        else:
            break
    return header


def column_paths(grid, width, header_rows, body_label_columns=()):
    """每个网格列对应的完整列维度路径（按表头层自上而下）。

    行标签列（正文区由行表头 th 或标签 td 构成）不产生列维度路径，
    即使其表头单元格没有跨到正文区。
    """
    label_cols = set(body_label_columns)
    paths = {}
    for c in range(width):
        if c in label_cols:
            paths[c] = []
            continue
        path = []
        seen = set()
        for hr in header_rows:
            pc = grid.get((hr, c))
            if pc is None or pc.cell.tag != "th":
                continue
            if pc.origin in seen:
                continue
            seen.add(pc.origin)
            path.append(normalize_ws(own_text(pc.cell)))
        paths[c] = path
    return paths


def structural_label_columns(grid, width, body_start, row_count):
    """结构判定：正文区该列的非空单元格全部是行表头 th。"""
    labels = set()
    for c in range(width):
        saw_th = False
        saw_td = False
        for r in range(body_start, row_count):
            pc = grid.get((r, c))
            if pc is None or pc.origin != (r, c):
                continue
            if pc.cell.tag == "th":
                saw_th = True
            else:
                saw_td = True
        if saw_th and not saw_td:
            labels.add(c)
    return labels


def detect_label_columns(grid, width, header_rows, rows, body_start,
                         unit_row_indices):
    """行标签列：表头为 th，正文 td 只出现文本标签、无数值/缺失记号。"""
    labels = set()
    for c in range(width):
        header_is_th = any(
            (pc := grid.get((hr, c))) is not None
            and pc.cell.tag == "th" and pc.origin_col == c
            for hr in header_rows
        )
        if not header_is_th:
            continue
        has_text_label = False
        has_data_token = False
        for r in range(body_start, len(rows)):
            if r in unit_row_indices:
                continue
            pc = grid.get((r, c))
            if pc is None or pc.origin != (r, c) or pc.cell.tag != "td":
                continue
            if not numeric_cell_text(pc.cell):
                continue
            status, *_ = interpret_cell(pc.cell)
            if status == "invalid_text":
                has_text_label = True
            else:
                has_data_token = True
        if has_text_label and not has_data_token:
            labels.add(c)
    return labels


def row_dimension_path(grid, width, r, body_start, label_cols):
    """行维度路径：覆盖该行的行表头，含 rowspan 延续与标签列的 td 标签。"""
    path = []
    seen = set()
    for c in range(width):
        pc = grid.get((r, c))
        if pc is None or pc.origin_row < body_start or pc.origin in seen:
            continue
        is_th = pc.cell.tag == "th"
        is_label_td = pc.cell.tag == "td" and c in label_cols
        if not (is_th or is_label_td):
            continue
        seen.add(pc.origin)
        path.append(normalize_ws(own_text(pc.cell)))
    return path


def header_unit(label):
    match = HEADER_PAREN_RE.search(label)
    if not match:
        return None
    inner = match.group(1).strip()
    if UNIT_TOKEN_RE.fullmatch(inner):
        return inner
    return None


def parse_caption(caption_node):
    """返回 (表题文本, [(度量, 单位)], 位置)。度量为 None 表示整表通用单位。"""
    if caption_node is None:
        return "", [], None
    text = raw_cell_text(caption_node)
    units = []
    for measure, unit in CAPTION_PAIR_RE.findall(text):
        units.append((measure.strip(), unit.strip()))
    paired = {m for m, _ in units}
    for solo in CAPTION_UNIT_RE.findall(text):
        solo = solo.strip()
        if solo in paired or not UNIT_TOKEN_RE.fullmatch(solo):
            continue
        units.append((None, solo))
    return text, units, location_of(caption_node)


def detect_unit_row(grid, width, r):
    """单位行：所有非空 td 都是纯单位记号且不含数字，否则返回 None。"""
    units = {}
    saw_nonblank = False
    for c in range(width):
        pc = grid.get((r, c))
        if pc is None or pc.origin != (r, c) or pc.cell.tag != "td":
            continue
        text = numeric_cell_text(pc.cell)
        if not text:
            continue
        saw_nonblank = True
        if not UNIT_TOKEN_RE.fullmatch(text):
            return None
        units[c] = (text, location_of(pc.cell))
    if not saw_nonblank:
        return None
    return units


# ---------------------------------------------------------------- 数值判读

def interpret_cell(cell):
    """返回 (状态, 数值, 单位, 缺失原因)。"""
    text = numeric_cell_text(cell)
    if text == "":
        return "missing", None, None, "blank"
    if text in DASH_TOKENS:
        return "missing", None, None, "dash"
    if text in UNDISCLOSED_TOKENS:
        return "missing", None, None, "undisclosed"
    match = CELL_VALUE_RE.fullmatch(text)
    if match:
        number_text, cell_unit = match.groups()
        if NUMBER_RE.fullmatch(number_text):
            value = float(number_text.replace(",", ""))
            return "value", value, cell_unit, None
    return "invalid_text", None, None, "unparseable"


def resolve_unit(column_path, column_index, cell_unit, caption_units,
                 header_unit_by_layer, unit_row_units, caption_loc):
    """只依据表题/表头/单位行/单元格自身判单位，冲突时隔离。"""
    candidates = []
    for measure, unit in caption_units:
        if measure is None or any(measure in label for label in column_path):
            candidates.append({"source": "caption", "unit": unit,
                               "location": caption_loc})
    for layer_index, unit in header_unit_by_layer:
        candidates.append({"source": f"header_layer_{layer_index}",
                           "unit": unit, "location": None})
    if column_index in unit_row_units:
        unit, loc = unit_row_units[column_index]
        candidates.append({"source": "unit_row", "unit": unit, "location": loc})
    if cell_unit:
        candidates.append({"source": "cell", "unit": cell_unit,
                           "location": None})

    distinct = {c["unit"] for c in candidates}
    conflict = len(distinct) > 1
    if cell_unit:
        chosen = cell_unit
    elif header_unit_by_layer:
        chosen = header_unit_by_layer[-1][1]
    elif column_index in unit_row_units:
        chosen = unit_row_units[column_index][0]
    elif caption_units:
        chosen = caption_units[-1][1]
    else:
        chosen = None
    return chosen, candidates, conflict


# ---------------------------------------------------------------- 身份与序列化

def _canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def stable_table_key(table, caption_text, header_paths, row_count, width):
    table_id = table.attrs.get("id")
    if table_id:
        return f"id:{table_id}"
    signature = _canonical({
        "caption": caption_text,
        "width": width,
        "rows": row_count,
        "header_paths": [header_paths.get(c, []) for c in range(width)],
    })
    return "sig:" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]


def make_fact_id(table_key, row_path, column_path, raw_text, r, c):
    payload = _canonical({
        "table_key": table_key,
        "row_path": row_path,
        "column_path": column_path,
        "raw_text": raw_text,
        "grid_row_index": r,
        "grid_col_index": c,
    })
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_json(obj):
    """可稳定比较的 JSON：固定键序、无环境相关字段。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)


# ---------------------------------------------------------------- 单表抽取

def extract_table(table, table_chain):
    placed, grid, diagnostics, row_count, width = build_grid(table)
    table_loc = location_of(table)
    for diag in diagnostics:
        diag.setdefault("location", {}).setdefault("table_source", table_loc)

    solvable = not any(d["severity"] == "table" for d in diagnostics)
    caption_node = next((c for c in table.children
                         if isinstance(c, Element) and c.tag == "caption"), None)
    caption_text, caption_units, caption_loc = parse_caption(caption_node)

    rows = table_rows(table)
    header_rows = header_row_indices(table, rows)
    body_start_tmp = (max(header_rows) + 1) if header_rows else 0
    structural_labels = structural_label_columns(
        grid, width, body_start_tmp, row_count) if solvable else set()
    hdr_paths = (column_paths(grid, width, header_rows, structural_labels)
                 if solvable else {})
    table_key = stable_table_key(table, caption_text, hdr_paths,
                                 row_count, width)

    base = {
        "table_key": table_key,
        "id": table.attrs.get("id"),
        "caption": caption_text,
        "contained_in_table_key": None,  # 由调用方补全
        "structure_solvable": solvable,
        "grid": {"row_count": row_count, "column_count": width},
        "header_paths": [hdr_paths.get(c, []) for c in range(width)],
        "unit_rows": [],
        "facts": [],
        "diagnostics": diagnostics,
    }
    if not solvable:
        return base, grid, rows, header_rows, hdr_paths, caption_units, caption_loc

    body_start = (max(header_rows) + 1) if header_rows else 0

    # 单位行识别（在事实判读之前完成）
    unit_row_indices = set()
    unit_row_units_by_row = {}
    for r, (_tr, _group) in enumerate(rows):
        if r < body_start:
            continue
        units = detect_unit_row(grid, width, r)
        if units is not None:
            unit_row_indices.add(r)
            unit_row_units_by_row[r] = units
    base["unit_rows"] = sorted(unit_row_indices)

    label_cols = structural_labels | detect_label_columns(
        grid, width, header_rows, rows, body_start, unit_row_indices)

    # 数值单元格统计与主表信号。数据列 = 拥有列表头路径的列；
    # 行标签（th）与无表头的说明/标签列不产出数值事实。
    data_columns = {c for c, path in hdr_paths.items() if path}
    numeric_count = 0
    th_count = sum(1 for p in placed if p.cell.tag == "th")
    facts = []
    # 单位行是表级显式声明：其按列单位向之后的数据行传播（不是借相邻行猜）
    propagated_unit_row = {}
    for r, (_tr, _group) in enumerate(rows):
        if r < body_start or r in unit_row_indices:
            if r in unit_row_indices:
                propagated_unit_row.update(unit_row_units_by_row[r])
            continue
        row_path = row_dimension_path(grid, width, r, body_start, label_cols)
        unit_row_units = propagated_unit_row
        for c in range(width):
            pc = grid.get((r, c))
            if pc is None or pc.origin != (r, c) or pc.cell.tag != "td":
                continue
            if c not in data_columns or c in label_cols:
                # 无列表头：按行标签/说明文字处理，不进入数值事实
                if numeric_cell_text(pc.cell):
                    diagnostics.append({
                        "severity": "cell",
                        "code": "non_data_column",
                        "message": "无列表头路径的 td 视为标签列，不产出事实",
                        "location": {**location_of(pc.cell),
                                     "grid_row_index": r, "grid_col_index": c},
                    })
                continue
            cell = pc.cell
            raw_text = raw_cell_text(cell)
            num_text = numeric_cell_text(cell)
            status, value, cell_unit, missing_reason = interpret_cell(cell)
            if status == "value":
                numeric_count += 1
            col_path = hdr_paths.get(c, [])
            header_layers = []
            for layer, label in enumerate(col_path):
                layer_unit = header_unit(label)
                if layer_unit is not None:
                    header_layers.append((layer, layer_unit))
            unit, unit_sources, conflict = resolve_unit(
                col_path, c, cell_unit, caption_units, header_layers,
                unit_row_units, caption_loc,
            )
            if unit is None and status == "value":
                diagnostics.append({
                    "severity": "cell",
                    "code": "unit_missing",
                    "message": "表题、表头、单位行与单元格均未提供单位，"
                               "未从相邻行推断",
                    "location": {**location_of(cell),
                                 "grid_row_index": r, "grid_col_index": c},
                })
            if conflict:
                diagnostics.append({
                    "severity": "cell",
                    "code": "unit_conflict",
                    "message": "单位来源互相冲突，事实已隔离待人工复查",
                    "detail": {"unit_candidates": unit_sources},
                    "location": {**location_of(cell),
                                 "grid_row_index": r, "grid_col_index": c},
                })
            if status == "invalid_text":
                diagnostics.append({
                    "severity": "cell",
                    "code": "unparseable_cell",
                    "message": "数据列单元格无法判读为数值或已知缺失记号",
                    "detail": {"raw_text": raw_text},
                    "location": {**location_of(cell),
                                 "grid_row_index": r, "grid_col_index": c},
                })
            markers = footnote_markers(cell)
            fact = {
                "fact_id": make_fact_id(table_key, row_path, col_path,
                                        raw_text, r, c),
                "table_key": table_key,
                "row_path": row_path,
                "column_path": col_path,
                "raw_text": raw_text,
                "numeric_text": num_text,
                "status": status,
                "value": value,
                "missing_reason": missing_reason,
                "unit": unit,
                "unit_sources": unit_sources,
                "unit_conflict": conflict,
                "footnote_markers": markers,
                "location": {
                    "grid_row_index": r,
                    "grid_col_index": c,
                    **location_of(cell),
                },
            }
            facts.append(fact)

    facts.sort(key=lambda f: (f["location"]["grid_row_index"],
                              f["location"]["grid_col_index"]))
    base["facts"] = facts
    base["selection_signals"] = {
        "has_caption": bool(caption_text),
        "header_column_count": sum(1 for p in hdr_paths.values() if p),
        "th_cell_count": th_count,
        "numeric_cell_count": numeric_count,
        "row_count": row_count,
        "column_count": width,
        "nested": bool(table_chain),
    }
    return base, grid, rows, header_rows, hdr_paths, caption_units, caption_loc


# ---------------------------------------------------------------- 整页抽取

def extract(html, source="<string>"):
    """抽取整页，返回可直接 JSON 序列化的结构化结果。"""
    root = build_tree(html)
    tables_out = []
    diagnostics = []
    key_to_table = {}

    for table, chain in iter_tables(root):
        record, *_ = extract_table(table, chain)
        key = record["table_key"]
        key_to_table[id(table)] = key
        parent_table = next(
            (node for node in reversed(chain)
             if isinstance(node, Element) and node.tag == "table"),
            None,
        )
        if parent_table is not None:
            # 父表在文档顺序上先于嵌套表被抽取
            record["contained_in_table_key"] = key_to_table.get(
                id(parent_table), "unknown")
        tables_out.append(record)

    # 角色判定与主表选择（可复查：附全部信号）
    data_tables = [
        t for t in tables_out
        if t["structure_solvable"]
        and t["contained_in_table_key"] is None
        and t["selection_signals"]["header_column_count"] >= 1
        and t["selection_signals"]["numeric_cell_count"] >= 1
    ]
    for record in tables_out:
        if record["contained_in_table_key"] is not None:
            record["role"] = "nested"
        elif not record["structure_solvable"]:
            record["role"] = "invalid"
        elif record in data_tables:
            record["role"] = "data"
        else:
            record["role"] = "note"
        record["is_primary"] = False

    if data_tables:
        def score(t):
            sig = t["selection_signals"]
            return (sig["numeric_cell_count"] * 10
                    + sig["header_column_count"] * 2
                    + (1 if sig["has_caption"] else 0))

        ranked = sorted(data_tables, key=score, reverse=True)
        top = score(ranked[0])
        winners = [t for t in ranked if score(t) == top]
        if len(winners) == 1:
            winners[0]["is_primary"] = True
        else:
            diagnostics.append({
                "severity": "page",
                "code": "ambiguous_primary_table",
                "message": "多个数据表信号相同，未自动指定主表",
                "detail": {"table_keys": [t["table_key"] for t in winners]},
                "location": {},
            })

    # 页面脚注定义，如 <p>1：初步数据。</p>
    page_footnotes = {}
    for node in _walk_elements(root):
        if node.tag == "p":
            text = own_text(node)
            match = FOOTNOTE_DEF_RE.match(text)
            if match:
                page_footnotes[match.group(1)] = match.group(2).strip()

    # 汇总诊断并建立事实反查索引
    fact_index = {}
    for record in tables_out:
        for diag in record.pop("diagnostics"):
            diag["table_key"] = record["table_key"]
            diagnostics.append(diag)
        for fact in record["facts"]:
            fact_index[fact["fact_id"]] = {
                "table_key": fact["table_key"],
                "grid_row_index": fact["location"]["grid_row_index"],
                "grid_col_index": fact["location"]["grid_col_index"],
                "source_line": fact["location"]["source_line"],
                "source_column": fact["location"]["source_column"],
                "row_path": fact["row_path"],
                "column_path": fact["column_path"],
            }
    diagnostics.sort(key=lambda d: (d.get("table_key") or "",
                                    d["code"],
                                    json.dumps(d.get("location", {}),
                                               ensure_ascii=False,
                                               sort_keys=True)))

    return {
        "source": source,
        "page_footnotes": page_footnotes,
        "tables": tables_out,
        "diagnostics": diagnostics,
        "fact_index": fact_index,
    }


def _walk_elements(root):
    stack = list(root.children)
    while stack:
        node = stack.pop()
        if isinstance(node, Element):
            yield node
            stack.extend(node.children)


def reverse_lookup(extraction, fact_id):
    """从 fact_id 反查 table、网格坐标、源码位置与原始表头路径。"""
    entry = extraction["fact_index"].get(fact_id)
    if entry is None:
        return None
    for table in extraction["tables"]:
        for fact in table["facts"]:
            if fact["fact_id"] == fact_id:
                return {
                    "table_key": table["table_key"],
                    "table_id": table["id"],
                    "caption": table["caption"],
                    "fact": fact,
                    "index_entry": entry,
                }
    return None


# ---------------------------------------------------------------- CLI

def _default_output(path, suffix):
    runtime = Path("runtime")
    runtime.mkdir(exist_ok=True)
    return runtime / f"{Path(path).stem}-{suffix}.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description="结构化表格抽取")
    parser.add_argument("html_path", help="本地 HTML 文件路径")
    parser.add_argument("--facts", help="事实输出路径")
    parser.add_argument("--diagnostics", help="诊断输出路径")
    parser.add_argument("--lookup", help="按 fact_id 反查并打印位置")
    args = parser.parse_args(argv)

    html = Path(args.html_path).read_text(encoding="utf-8")
    extraction = extract(html, source=args.html_path)

    if args.lookup:
        print(stable_json(reverse_lookup(extraction, args.lookup)))
        return 0

    facts_path = Path(args.facts) if args.facts else _default_output(
        args.html_path, "facts")
    diag_path = Path(args.diagnostics) if args.diagnostics else _default_output(
        args.html_path, "diagnostics")
    facts_path.write_text(stable_json(extraction), encoding="utf-8")
    diag_path.write_text(
        stable_json(extraction["diagnostics"]), encoding="utf-8")

    fact_count = sum(len(t["facts"]) for t in extraction["tables"])
    print(f"tables={len(extraction['tables'])} facts={fact_count} "
          f"diagnostics={len(extraction['diagnostics'])}")
    print(f"facts -> {facts_path}")
    print(f"diagnostics -> {diag_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
