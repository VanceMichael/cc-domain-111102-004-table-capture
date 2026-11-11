"""结构化表格抽取的回归测试。

覆盖需求点名的风险：多级表头、重复列名、单位行、脚注混入数字、
嵌套说明表、畸形跨格、单位冲突、事实身份稳定性与反查。
"""

import unittest

from app.tables import (
    build_grid,
    detect_unit_row,
    extract,
    reverse_lookup,
    stable_json,
)

TRADE_HTML = """
<!doctype html><meta charset="utf-8"><h1>合成外贸公报</h1>
<table id="note"><tr><td>说明，详见下表</td></tr></table>
<table id="trade"><caption>货物贸易（金额：亿元）</caption>
<thead><tr>
<th rowspan="2">地区</th><th colspan="2">累计</th><th colspan="2">当月</th>
</tr><tr>
<th>金额</th><th>同比（%）</th><th>金额</th><th>同比（%）</th>
</tr></thead>
<tbody>
<tr><th>合成甲区</th><td>1,234.50<sup>1</sup></td><td>8.4</td>
<td>0</td><td>—</td></tr>
<tr><th>合成乙区</th><td></td><td>未披露</td>
<td>20</td><td>2.0</td></tr>
</tbody></table><p>1：初步数据。</p>
"""

MALFORMED_HTML = """
<meta charset="utf-8">
<table><tr><th rowspan="0">地区</th><th colspan="-2">金额</th></tr>
<tr><td>甲</td></tr></table>
<table id="good"><tr><th>区</th><th>值</th></tr><tr><td>甲</td><td>5</td></tr></table>
"""

NESTED_HTML = """
<meta charset="utf-8"><table id="main"><caption>金额（万元）</caption>
<tr><th>区</th><th>值</th></tr>
<tr><td>甲</td><td>3<table id="foot"><tr><td>附注2</td></tr></table></td></tr>
</table>
"""


def facts_by_id(extraction):
    return {
        f["fact_id"]: f
        for t in extraction["tables"]
        for f in t["facts"]
    }


def table_of(extraction, table_id):
    return next(t for t in extraction["tables"] if t["id"] == table_id)


class MultiHeaderTests(unittest.TestCase):
    def setUp(self):
        self.extraction = extract(TRADE_HTML, source="trade")
        self.trade = table_of(self.extraction, "trade")

    def test_full_column_dimension_paths(self):
        self.assertEqual(
            self.trade["header_paths"],
            [
                [],  # 行标签列“地区”只有行维度，不构成列维度路径
                ["累计", "金额"],
                ["累计", "同比（%）"],
                ["当月", "金额"],
                ["当月", "同比（%）"],
            ],
        )

    def test_duplicate_column_names_keep_separate_facts(self):
        # 相邻两列同名“金额”，维度路径不同绝不能合并
        amount = [f for f in self.trade["facts"]
                  if f["column_path"][-1] == "金额"]
        paths = {tuple(f["column_path"]) for f in amount}
        self.assertIn(("累计", "金额"), paths)
        self.assertIn(("当月", "金额"), paths)
        self.assertEqual(len({f["fact_id"] for f in amount}), len(amount))

    def test_row_dimension_path(self):
        for fact in self.trade["facts"]:
            self.assertIn(fact["row_path"], [["合成甲区"], ["合成乙区"]])

    def test_three_header_levels(self):
        html = """
        <table><thead>
        <tr><th rowspan="3">地区</th><th colspan="2">总量</th></tr>
        <tr><th colspan="2">累计</th></tr>
        <tr><th>金额</th><th>同比（%）</th></tr>
        </thead><tbody>
        <tr><th>甲区</th><td>10</td><td>1.0</td></tr>
        </tbody></table>
        """
        result = extract(html)
        table = result["tables"][0]
        self.assertEqual(table["header_paths"][1], ["总量", "累计", "金额"])
        self.assertEqual(table["header_paths"][2],
                         ["总量", "累计", "同比（%）"])
        self.assertEqual(
            table["facts"][0]["column_path"], ["总量", "累计", "金额"])


class MissingValueTests(unittest.TestCase):
    def setUp(self):
        self.facts = facts_by_id(extract(TRADE_HTML, source="trade"))

    def _fact(self, row, path):
        return next(
            f for f in self.facts.values()
            if f["row_path"] == [row] and f["column_path"] == path
        )

    def test_blank_dash_undisclosed_zero_are_distinct(self):
        blank = self._fact("合成乙区", ["累计", "金额"])
        dash = self._fact("合成甲区", ["当月", "同比（%）"])
        undisclosed = self._fact("合成乙区", ["累计", "同比（%）"])
        zero = self._fact("合成甲区", ["当月", "金额"])

        self.assertEqual(blank["status"], "missing")
        self.assertEqual(blank["missing_reason"], "blank")
        self.assertIsNone(blank["value"])

        self.assertEqual(dash["status"], "missing")
        self.assertEqual(dash["missing_reason"], "dash")

        self.assertEqual(undisclosed["status"], "missing")
        self.assertEqual(undisclosed["missing_reason"], "undisclosed")

        # 显式 0 是数值，不得当作缺失
        self.assertEqual(zero["status"], "value")
        self.assertEqual(zero["value"], 0.0)
        self.assertIsNone(zero["missing_reason"])

    def test_blank_is_not_confused_with_zero(self):
        for fact in self.facts.values():
            if fact["missing_reason"] == "blank":
                self.assertNotEqual(fact["value"], 0.0)
                self.assertIsNone(fact["value"])


class FootnoteTests(unittest.TestCase):
    def setUp(self):
        self.extraction = extract(TRADE_HTML, source="trade")
        self.facts = facts_by_id(self.extraction)

    def test_footnote_marker_separated_from_number(self):
        fact = next(
            f for f in self.facts.values()
            if f["row_path"] == ["合成甲区"]
            and f["column_path"] == ["累计", "金额"]
        )
        # 原始文本保留脚注痕迹，数值文本剔除上标
        self.assertEqual(fact["raw_text"], "1,234.501")
        self.assertEqual(fact["numeric_text"], "1,234.50")
        self.assertEqual(fact["footnote_markers"], ["1"])
        self.assertEqual(fact["value"], 1234.5)

    def test_page_footnote_definition_collected(self):
        self.assertEqual(
            self.extraction["page_footnotes"], {"1": "初步数据。"})


class UnitTests(unittest.TestCase):
    def test_caption_and_header_units(self):
        facts = facts_by_id(extract(TRADE_HTML, source="trade"))
        amount = next(
            f for f in facts.values()
            if f["row_path"] == ["合成甲区"]
            and f["column_path"] == ["累计", "金额"]
        )
        ratio = next(
            f for f in facts.values()
            if f["column_path"] == ["累计", "同比（%）"]
        )
        self.assertEqual(amount["unit"], "亿元")
        self.assertEqual(ratio["unit"], "%")
        self.assertFalse(amount["unit_conflict"])

    def test_unit_row_is_detected_and_not_emitted_as_fact(self):
        html = """
        <table><thead><tr><th>区</th><th>值</th></tr></thead>
        <tbody>
        <tr><td></td><td>万元</td></tr>
        <tr><th>甲</th><td>10</td></tr>
        </tbody></table>
        """
        result = extract(html)
        table = result["tables"][0]
        self.assertEqual(table["unit_rows"], [1])
        # 单位行不产生事实，数值行沿用单位行单位
        fact = table["facts"][0]
        self.assertEqual(fact["unit"], "万元")
        self.assertNotIn(
            1, [f["location"]["grid_row_index"] for f in table["facts"]])

    def test_conflicting_units_are_isolated_not_guessed(self):
        html = """
        <table><caption>表（金额：万元）</caption>
        <thead><tr><th>区</th><th>金额</th></tr></thead>
        <tbody>
        <tr><th>甲</th><td>10亿元</td></tr>
        <tr><th>乙</th><td>20</td></tr>
        </tbody></table>
        """
        result = extract(html)
        table = result["tables"][0]
        first, second = table["facts"]
        # 冲突事实保留全部候选并打隔离标记，不借用相邻行
        self.assertTrue(first["unit_conflict"])
        units = {c["unit"] for c in first["unit_sources"]}
        self.assertEqual(units, {"万元", "亿元"})
        self.assertEqual(first["unit"], "亿元")  # 单元格自带单位优先
        # 相邻行没有单元格单位，仍只使用表题单位，且无冲突
        self.assertFalse(second["unit_conflict"])
        self.assertEqual(second["unit"], "万元")
        codes = {d["code"] for d in result["diagnostics"]}
        self.assertIn("unit_conflict", codes)

    def test_missing_unit_never_borrowed_from_neighbor(self):
        html = """
        <table><thead><tr><th>区</th><th>值</th></tr></thead>
        <tbody>
        <tr><th>甲</th><td>10万元</td></tr>
        <tr><th>乙</th><td>20</td></tr>
        </tbody></table>
        """
        result = extract(html)
        table = result["tables"][0]
        first, second = table["facts"]
        self.assertEqual(first["unit"], "万元")
        self.assertIsNone(second["unit"])
        self.assertIn("unit_missing",
                      {d["code"] for d in result["diagnostics"]})


class TableSelectionTests(unittest.TestCase):
    def test_primary_table_is_not_first_table(self):
        result = extract(TRADE_HTML, source="trade")
        roles = {t["id"]: (t["role"], t["is_primary"])
                 for t in result["tables"]}
        # 页面第一个 table 是说明表，不得被选为主表
        self.assertEqual(roles["note"], ("note", False))
        self.assertEqual(roles["trade"], ("data", True))

    def test_ambiguous_primary_emits_diagnostic(self):
        html = """
        <table id="a"><tr><th>x</th></tr><tr><td>1</td></tr></table>
        <table id="b"><tr><th>x</th></tr><tr><td>1</td></tr></table>
        """
        result = extract(html)
        self.assertFalse(any(t["is_primary"] for t in result["tables"]))
        self.assertIn("ambiguous_primary_table",
                      {d["code"] for d in result["diagnostics"]})


class NestedTableTests(unittest.TestCase):
    def test_nested_explanation_table_is_separate(self):
        result = extract(NESTED_HTML, source="nested")
        main = table_of(result, "main")
        foot = table_of(result, "foot")
        self.assertEqual(main["role"], "data")
        self.assertTrue(main["is_primary"])
        self.assertEqual(foot["role"], "nested")
        self.assertEqual(foot["contained_in_table_key"], main["table_key"])
        # 外层单元格的数值文本不混入嵌套表文字
        outer = main["facts"][0]
        self.assertEqual(outer["raw_text"], "3")
        self.assertEqual(outer["value"], 3.0)
        # 嵌套说明表没有数值事实
        self.assertEqual(foot["facts"], [])
        # 行标签“甲”进入行维度而不是数值事实
        self.assertEqual(outer["row_path"], ["甲"])


class MalformedStructureTests(unittest.TestCase):
    def test_invalid_spans_make_table_unsolvable(self):
        result = extract(MALFORMED_HTML, source="malformed")
        bad = next(t for t in result["tables"] if t["id"] is None)
        self.assertFalse(bad["structure_solvable"])
        self.assertEqual(bad["role"], "invalid")
        # 不可解表不产出任何事实，也不补零
        self.assertEqual(bad["facts"], [])
        codes = [d["code"] for d in result["diagnostics"]
                 if d["table_key"] == bad["table_key"]]
        self.assertIn("invalid_span", codes)
        # 结构错误绝不通过补零修成矩形
        self.assertIn("grid_hole", codes)

    def test_unrelated_valid_table_still_captured(self):
        result = extract(MALFORMED_HTML, source="malformed")
        good = table_of(result, "good")
        self.assertTrue(good["structure_solvable"])
        self.assertEqual(len(good["facts"]), 1)
        self.assertEqual(good["facts"][0]["value"], 5.0)

    def test_overlapping_cells_are_fatal_table_error(self):
        # 正常 rowspan 表格对照组可解
        normal = """
        <table><tr>
        <td rowspan="2">A</td><td>B</td>
        </tr><tr><td>C</td></tr></table>
        """
        self.assertTrue(extract(normal)["tables"][0]["structure_solvable"])

        # 注入一个既有占位（模拟上游错误），使第二行单元格与其重叠
        from app.tables import build_tree, Element, PlacedCell
        html = """
        <table><tr>
        <td rowspan="2">A</td><td>B</td>
        </tr><tr><td>C</td></tr></table>
        """
        root = build_tree(html)
        table = next(node for node in root.children
                     if isinstance(node, Element) and node.tag == "table")
        # (1,0) 本应被 rowspan=2 的 A 占据；再注入外来占位制造重叠
        blocker = PlacedCell(Element("td", {}, 0, 0), 1, 0, 1, 1)
        _placed, _grid, diags, _rc, _w = build_grid(
            table, prefilled={(1, 0): blocker})
        codes = {d["code"] for d in diags}
        self.assertIn("overlapping_cells", codes)
        self.assertTrue(
            all(d["severity"] == "table"
                for d in diags if d["code"] == "overlapping_cells"))

    def test_minor_cell_issue_does_not_kill_table(self):
        html = """
        <table><thead><tr><th>区</th><th>值</th></tr></thead>
        <tbody>
        <tr><th>甲</th><td>约十个</td></tr>
        <tr><th>乙</th><td>20</td></tr>
        </tbody></table>
        """
        result = extract(html)
        table = result["tables"][0]
        self.assertTrue(table["structure_solvable"])
        bad_fact = next(f for f in table["facts"]
                        if f["status"] == "invalid_text")
        good_fact = next(f for f in table["facts"]
                         if f["status"] == "value")
        self.assertEqual(bad_fact["missing_reason"], "unparseable")
        self.assertEqual(good_fact["value"], 20.0)


class IdentityAndProvenanceTests(unittest.TestCase):
    def test_identity_immune_to_container_and_style_changes(self):
        base = extract(TRADE_HTML, source="trade")
        restyled = extract(
            '<div class="wrap" style="margin: 0">' + TRADE_HTML + "</div>",
            source="trade-wrapped",
        )
        base_ids = sorted(facts_by_id(base))
        restyled_ids = sorted(facts_by_id(restyled))
        self.assertEqual(base_ids, restyled_ids)

    def test_identity_changes_when_fact_content_changes(self):
        base = extract(TRADE_HTML, source="trade")
        changed = extract(TRADE_HTML.replace("8.4", "8.5"), source="trade2")
        base_ids = set(facts_by_id(base))
        changed_ids = set(facts_by_id(changed))
        self.assertNotEqual(base_ids, changed_ids)

    def test_reverse_lookup_reaches_table_headers_and_position(self):
        result = extract(TRADE_HTML, source="trade")
        fact = next(
            f for t in result["tables"] if t["id"] == "trade"
            for f in t["facts"] if f["raw_text"] == "20"
        )
        hit = reverse_lookup(result, fact["fact_id"])
        self.assertEqual(hit["table_id"], "trade")
        self.assertEqual(hit["fact"]["column_path"], ["当月", "金额"])
        self.assertEqual(hit["fact"]["row_path"], ["合成乙区"])
        self.assertEqual(hit["index_entry"]["grid_col_index"], 3)
        self.assertIsNone(reverse_lookup(result, "no-such-id"))

    def test_output_is_deterministic_and_comparable(self):
        first = stable_json(extract(TRADE_HTML, source="trade"))
        second = stable_json(extract(TRADE_HTML, source="trade"))
        self.assertEqual(first, second)


class SourceLocationTests(unittest.TestCase):
    def test_fact_keeps_grid_and_source_position(self):
        result = extract(TRADE_HTML, source="trade")
        fact = next(
            f for t in result["tables"] if t["id"] == "trade"
            for f in t["facts"] if f["numeric_text"] == "1,234.50"
        )
        loc = fact["location"]
        # 网格坐标从 0 开始；两行 thead 之后，首条数据位于第 2 行；
        # 源码行从 1 开始、列从 0 开始
        self.assertEqual(loc["grid_row_index"], 2)
        self.assertEqual(loc["grid_col_index"], 1)
        self.assertGreaterEqual(loc["source_line"], 1)
        self.assertGreaterEqual(loc["source_column"], 0)


if __name__ == "__main__":
    unittest.main()
