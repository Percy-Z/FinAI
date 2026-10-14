"""取数层核心测试：清单、检索解析、扫描、拼装、接流水线、行情表校验。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from finai_web import adapter, cninfo, extract, market, schema
from finai_web import targets as targets_mod
from finai_web.downloader import FetchOutcome, fetch_one, import_local, save_manifest
from finai_web.firecrawl import Firecrawl, FirecrawlError, FirecrawlUnavailable
from finai_web.http import HttpError


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 定标公司清单
# ---------------------------------------------------------------------------


class TargetsTest(unittest.TestCase):
    def test_读清单并按代码判板块(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                Path(tmp) / "targets.csv",
                "ticker,name,market,org_id,years,annual_report_url\n"
                "# 注释行要被跳过\n"
                "600519,贵州茅台,CN,gssh0600519,2023;2024,\n"
                "000858,五粮液,CN,,2024,\n",
            )
            items = targets_mod.read_targets(path)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].cninfo_column, "sse")   # 6 开头 → 沪市
        self.assertEqual(items[1].cninfo_column, "szse")  # 其余 → 深市
        self.assertEqual(items[0].years_desc, (2024, 2023))  # 从新到旧
        self.assertEqual(items[0].search_key(), "600519,gssh0600519")

    def test_表头列名拼错要报错(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "targets.csv", "ticker,shortname\n600519,贵州茅台\n")
            with self.assertRaises(targets_mod.TargetError):
                targets_mod.read_targets(path)

    def test_年份写错要报错(self) -> None:
        with self.assertRaises(targets_mod.TargetError):
            targets_mod.parse_years("2023;去年")


# ---------------------------------------------------------------------------
# 巨潮检索
# ---------------------------------------------------------------------------

CANNED = {
    "announcements": [
        {
            "secCode": "600519",
            "secName": "贵州茅台",
            "announcementTitle": "2024年年度报告摘要",
            "announcementTime": 1743436800000,
            "adjunctUrl": "finalpage/2025-04-01/1.pdf",
            "announcementId": "1",
        },
        {
            "secCode": "600519",
            "secName": "贵州茅台",
            "announcementTitle": "2024年年度报告",
            "announcementTime": 1743436800000,
            "adjunctUrl": "finalpage/2025-04-01/2.PDF",
            "announcementId": "2",
        },
        {
            "secCode": "600519",
            "secName": "贵州茅台",
            "announcementTitle": "2023年年度报告",
            "announcementTime": 1711929600000,
            "adjunctUrl": "finalpage/2024-04-01/3.PDF",
            "announcementId": "3",
        },
    ]
}


class CninfoTest(unittest.TestCase):
    def test_检索并挑出正文而不是摘要(self) -> None:
        captured: dict[str, object] = {}

        def fake_transport(url: str, form: dict, **kwargs: object) -> dict:
            captured["url"] = url
            captured["form"] = form
            return CANNED

        items = cninfo.query(code="600519", column="sse", year=2024, transport=fake_transport)
        picked = cninfo.pick_annual_report(items, 2024)

        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.title, "2024年年度报告")
        self.assertTrue(picked.pdf_url.startswith("https://static.cninfo.com.cn/finalpage/"))
        self.assertEqual(captured["form"]["seDate"], "2024-01-01~2024-12-31")  # type: ignore[index]
        self.assertEqual(captured["form"]["category"], "category_ndbg_szsh")  # type: ignore[index]

    def test_检索失败包成人话(self) -> None:
        def boom(*args: object, **kwargs: object) -> dict:
            raise HttpError("HTTP 500")

        with self.assertRaises(cninfo.CninfoError):
            cninfo.query(code="600519", column="sse", year=2024, transport=boom)


# ---------------------------------------------------------------------------
# 扫描 + 拼装
# ---------------------------------------------------------------------------

# 一份「像年报」的页文本（单位：千元，合并报表）。
# 数字刻意构造成资产 = 负债 + 权益，这样拼出来的基期表必须配平。
PAGE_1 = """贵州茅台 2024 年年度报告
合并资产负债表
单位：千元
货币资金 5,000
应收账款 1,000
存货 2,000
固定资产 3,000
资产总计 13,000
应付账款 1,500
短期借款 500
长期借款 2,000
负债合计 5,000
归属于母公司所有者权益合计 7,500
少数股东权益 500
所有者权益合计 8,000
股本 1,000
"""

PAGE_2 = """合并利润表
单位：千元
营业总收入 10,000
营业成本 6,000
利润总额 2,000
所得税费用 500
净利润 1,500
归属于母公司股东的净利润 1,400
利息费用 100

合并现金流量表（补充资料）
固定资产折旧、油气资产折耗、生产性生物资产折旧 300
无形资产摊销 100
长期待摊费用摊销 0
"""

PAGE_3 = """母公司资产负债表
单位：千元
应收账款周转率 3.5
应收账款 999
"""


class ScanTest(unittest.TestCase):
    def test_扫描认单位并跳过衍生指标行(self) -> None:
        matches = extract.scan_pages([PAGE_1, PAGE_2, PAGE_3])

        self.assertEqual(matches["cash"].value_yuan, 5_000_000)  # 千元 → 元
        self.assertEqual(matches["cash"].page, 1)
        # 母公司报表里的 999 不该赢过合并报表的 1,000
        self.assertEqual(matches["accounts_receivable"].value_yuan, 1_000_000)
        # 「应收账款周转率」那一行要跳过（否则会抽到 3.5）
        self.assertNotEqual(matches["accounts_receivable"].value_yuan, 3_500)

    def test_拼出的基期表必须配平(self) -> None:
        record = extract.build_record(
            code="600519", name="贵州茅台", year=2024, pages=[PAGE_1, PAGE_2]
        )
        opening = record["opening_balance"]
        self.assertIsNotNone(opening)
        values = opening["values"]

        total_assets = (
            values["cash"]
            + values["accounts_receivable"]
            + values["inventory"]
            + values["fixed_assets"]
            + values["other_non_current_assets"]
        )
        total_liabilities = (
            values["accounts_payable"]
            + values["interest_bearing_debt"]
            + values["other_non_current_liabilities"]
        )
        total_equity = (
            values["share_capital"] + values["retained_earnings"] + values["minority_equity"]
        )
        self.assertAlmostEqual(total_assets, 13_000_000)
        self.assertAlmostEqual(total_liabilities + total_equity, total_assets)
        # 倒挤项要标出来源，不能冒充「读到的」
        self.assertTrue(opening["provenance"]["other_non_current_assets"].startswith("derived"))
        self.assertEqual(opening["provenance"]["cash"], "read")

    def test_可比指标里_EBIT_与_EBITDA_是推导出来的(self) -> None:
        record = extract.build_record(
            code="600519", name="贵州茅台", year=2024, pages=[PAGE_1, PAGE_2]
        )
        peer = record["peer"]["values"]

        self.assertAlmostEqual(peer["revenue"], 10_000_000)
        self.assertAlmostEqual(peer["net_profit_attributable"], 1_400_000)
        self.assertAlmostEqual(peer["book_equity_attributable"], 7_500_000)
        self.assertAlmostEqual(peer["ebit"], 2_100_000)          # 利润总额 + 利息费用
        self.assertAlmostEqual(peer["ebitda"], 2_500_000)        # EBIT + 折旧摊销 400
        self.assertIn("derived", record["peer"]["provenance"]["ebit"])

    def test_没有文字层的扫描件要说清楚(self) -> None:
        record = extract.build_record(code="000001", name="扫描件", year=2024, pages=["", "  "])
        self.assertFalse(record["has_text_layer"])
        self.assertTrue(any("扫描件" in note for note in record["notes"]))


class SchemaTest(unittest.TestCase):
    def test_缺必填科目时报错并点名(self) -> None:
        with self.assertRaises(schema.SchemaError) as ctx:
            schema.build_opening_balance({"cash": 1.0})
        self.assertIn("accounts_receivable", str(ctx.exception))

    def test_没有借款科目时有息负债按0并提示(self) -> None:
        built = schema.build_opening_balance(
            {
                "cash": 10.0,
                "accounts_receivable": 5.0,
                "inventory": 5.0,
                "fixed_assets": 30.0,
                "total_assets": 50.0,
                "accounts_payable": 10.0,
                "total_liabilities": 10.0,
                "share_capital": 40.0,
                "equity_attributable": 40.0,
            }
        )
        self.assertEqual(built.values["interest_bearing_debt"], 0.0)
        self.assertTrue(any("有息负债按 0" in note for note in built.notes))

    def test_EBIT_缺利润总额时退到净利润加所得税(self) -> None:
        built = schema.build_peer_metrics(
            {
                "revenue": 100.0,
                "net_profit_attributable": 12.0,
                "equity_attributable": 50.0,
                "net_profit": 15.0,
                "income_tax": 5.0,
                "interest_expense": 2.0,
                "depreciation_fixed_assets": 8.0,
            }
        )
        self.assertAlmostEqual(built.values["ebit"], 22.0)
        self.assertAlmostEqual(built.values["ebitda"], 30.0)


# ---------------------------------------------------------------------------
# 真实年报版式（华为 2025 年报里踩出来的规则）
# ---------------------------------------------------------------------------

SUMMARY_PAGE = """五年财务摘要
单位：百万元
总资产 190,961 1,290,149
净利润 9,732 68,036
"""

BALANCE_PAGE = """概要合并财务状况表
（人民币百万元） 附注 2025年 2024年
12月31日 12月31日
物业、厂房及设备 14 189,578 176,565
存货及其他合同成本 19 195,046 179,139
应收账款及应收票据 21 3,959 4,664
现金及现金等价物 23 150,364 146,265
资产总计 1,334,930 1,290,149
本公司所有者应占权益 597,034 544,416
非控制性权益 3,086 203
权益合计 600,120 544,619
借款 24 219,861 237,508
租赁负债 7,954 7,974
非流动负债合计 232,203 250,252
借款 24 19,423 27,363
应付账款及应付票据 25 113,886 105,077
租赁负债 3,730 3,481
负债合计 734,810 745,530
"""

INCOME_PAGE = """概要合并利润及其他综合收益表
（人民币百万元） 附注 2025年 2024年
收入 8 880,941 862,072
税前利润 81,401 70,869
所得税 12 (13,365) (8,295)
净利润 68,036 62,574
净利润归属于：
本公司所有者 67,104 62,491
非控制性权益 932 83
财务收入及财务费用 11 (15,821) (8,255)
"""

PROSE_PAGE = """股本结构说明
总出资相当于公司总股本的比例约0.59%。
"""

HUAWEI_VALUES = {
    "cash": 150_364.0,
    "accounts_receivable": 130_753.0,
    "inventory": 195_046.0,
    "fixed_assets": 189_578.0,
    "total_assets": 1_334_930.0,
    "accounts_payable": 113_886.0,
    "total_liabilities": 734_810.0,
    "equity_attributable": 597_034.0,
    "minority_equity": 3_086.0,
    "borrowings": 239_284.0,
    "lease_liabilities": 11_684.0,
}


class RealReportLayoutTest(unittest.TestCase):
    """这些都是从真实年报里踩出来的规则，改抽取逻辑时要靠它们兜住。"""

    def test_跳过附注编号取金额(self) -> None:
        """「物业、厂房及设备 14 189,578」里 14 是附注号，不是金额。"""
        hits = extract.scan_pages([BALANCE_PAGE])
        self.assertEqual(hits["fixed_assets"].value_yuan, 189_578 * 1_000_000)
        self.assertIn("跳过附注编号 14", hits["fixed_assets"].note)

    def test_认人民币百万元(self) -> None:
        hits = extract.scan_pages([BALANCE_PAGE])
        self.assertEqual(hits["cash"].unit, "百万元")
        self.assertEqual(hits["cash"].value_yuan, 150_364 * 1_000_000)

    def test_正文页优先于摘要页(self) -> None:
        """摘要在第 1 页、正文在第 2 页时，必须取正文的数。"""
        hits = extract.scan_pages([SUMMARY_PAGE, BALANCE_PAGE])
        self.assertEqual(hits["total_assets"].value_yuan, 1_334_930 * 1_000_000)
        self.assertEqual(hits["total_assets"].page, 2)

    def test_同名科目按报表归属选(self) -> None:
        """非控制性权益：利润表里是少数股东损益 932，资产负债表里是 3,086。"""
        hits = extract.scan_pages([INCOME_PAGE, BALANCE_PAGE])
        self.assertEqual(hits["minority_equity"].value_yuan, 3_086 * 1_000_000)
        self.assertEqual(hits["minority_equity"].page, 2)

    def test_负债合计不被流动非流动合计顶掉(self) -> None:
        hits = extract.scan_pages([BALANCE_PAGE])
        self.assertEqual(hits["total_liabilities"].value_yuan, 734_810 * 1_000_000)

    def test_拆成两段的科目要相加(self) -> None:
        """借款分非流动 219,861 与流动 19,423 两段列示，两段都要算。"""
        hits = extract.scan_pages([BALANCE_PAGE])
        self.assertEqual(hits["borrowings"].value_yuan, (219_861 + 19_423) * 1_000_000)
        self.assertEqual(hits["lease_liabilities"].value_yuan, (7_954 + 3_730) * 1_000_000)

    def test_跨行科目靠版面规则抽(self) -> None:
        """华为的归母净利润写作「净利润归属于：/ 本公司所有者 67,104」。"""
        hits = extract.scan_pages([INCOME_PAGE])
        self.assertEqual(hits["net_profit_attributable"].value_yuan, 67_104 * 1_000_000)
        self.assertIn("版面规则", hits["net_profit_attributable"].note)

    def test_散文里的短标签不算科目(self) -> None:
        """「总出资相当于公司总股本的比例约0.59%」不是「股本 = 0.59 元」。"""
        hits = extract.scan_pages([PROSE_PAGE])
        self.assertNotIn("share_capital", hits)

    def test_有息负债两套口径不混用(self) -> None:
        """IFRS 口径抽到「借款」时，不该再把 M&A 页的「短期借款」加进来。"""
        values = dict(HUAWEI_VALUES)
        values["short_term_debt"] = 239_284.0  # 摘要页的重复数，必须被忽略
        built = schema.build_opening_balance(values)
        self.assertAlmostEqual(built.values["interest_bearing_debt"], 250_968.0)
        self.assertIn("IFRS 口径", built.provenance["interest_bearing_debt"])

    def test_华为这笔账能配平(self) -> None:
        built = schema.build_opening_balance(dict(HUAWEI_VALUES))
        v = built.values
        assets = sum(v[name] for name in schema.OPENING_FIELDS[:5])
        liabilities = (
            v["accounts_payable"]
            + v["interest_bearing_debt"]
            + v["other_non_current_liabilities"]
        )
        equity = v["share_capital"] + v["retained_earnings"] + v["minority_equity"]
        self.assertAlmostEqual(assets, 1_334_930.0)
        self.assertAlmostEqual(assets - liabilities - equity, 0.0, places=6)

    def test_EBIT用财务费用加回并取绝对值(self) -> None:
        built = schema.build_peer_metrics(
            {
                "revenue": 880_941.0,
                "net_profit_attributable": 67_104.0,
                "equity_attributable": 597_034.0,
                "total_profit": 81_401.0,
                "net_profit": 68_036.0,
                "income_tax": -13_365.0,      # IFRS 式：括号里的负数
                "finance_expense": -15_821.0,
            }
        )
        self.assertAlmostEqual(built.values["ebit"], 81_401.0 + 15_821.0)
        # 交叉验证：EBIT 应等于「营业利润 96,937 + 应占联合营公司业绩 285」
        self.assertAlmostEqual(built.values["ebit"], 96_937.0 + 285.0)


# ---------------------------------------------------------------------------
# 接流水线
# ---------------------------------------------------------------------------


# 美国 10-K 版式（Apple 2025 年报里踩出来的）。注意：引号是弯引号，
# 金额单位是「million」，报表标题是 CONSOLIDATED ...，这些都得能认。
APPLE_BALANCE = """Apple Inc.
CONSOLIDATED BALANCE SHEETS
(In millions, except number of shares, which are reflected in thousands, and par value)
September 27, September 28,
2025 2024
ASSETS:
Cash and cash equivalents $ 35,934 $ 29,943
Accounts receivable, net 39,777 33,410
Inventories 5,718 7,286
Property, plant and equipment, net 49,834 45,680
Total assets $ 359,241 $ 364,980
LIABILITIES AND SHAREHOLDERS\u2019 EQUITY:
Accounts payable $ 69,860 $ 68,960
Commercial paper 7,979 9,967
Term debt 12,350 10,912
Total current liabilities 165,631 176,392
Term debt 78,328 85,750
Total liabilities 285,508 308,030
Total shareholders\u2019 equity 73,733 56,950
Total liabilities and shareholders\u2019 equity $ 359,241 $ 364,980
"""

APPLE_INCOME = """Apple Inc.
CONSOLIDATED STATEMENTS OF OPERATIONS
(In millions, except number of shares)
Years ended
September 27, September 28, September 30,
2025 2024 2023
Net sales:
Products $ 307,003 $ 294,866 $ 298,085
Services 109,158 96,169 85,200
Total net sales 416,161 391,035 383,285
Total cost of sales 220,960 210,352 214,137
Operating income 133,050 123,216 114,301
Other income/(expense), net (321) 269 (565)
Income before provision for income taxes 132,729 123,485 113,736
Provision for income taxes 20,719 29,749 16,741
Net income $ 112,010 $ 93,736 $ 96,995
"""

APPLE_CASHFLOW = """Apple Inc.
CONSOLIDATED STATEMENTS OF CASH FLOWS
(In millions)
Operating activities:
Net income 112,010 93,736 96,995
Depreciation and amortization 11,698 11,445 11,519
Cash generated by operating activities 111,482 118,254 110,543
"""


class USReportLayoutTest(unittest.TestCase):
    """美国 10-K：英文科目名、弯引号、百万美元、CONSOLIDATED 标题。"""

    def test_认百万美元而不是千股(self) -> None:
        """表头同时写着 In millions 与 in thousands（股数），金额要按百万。"""
        hits = extract.scan_pages([APPLE_BALANCE])
        self.assertEqual(hits["cash"].unit, "millions")
        self.assertEqual(hits["cash"].value_yuan, 35_934 * 1_000_000)

    def test_认弯引号的股东权益(self) -> None:
        hits = extract.scan_pages([APPLE_BALANCE])
        self.assertEqual(hits["equity_attributable"].value_yuan, 73_733 * 1_000_000)

    def test_负债合计不被负债与权益合计顶掉(self) -> None:
        hits = extract.scan_pages([APPLE_BALANCE])
        self.assertEqual(hits["total_liabilities"].value_yuan, 285_508 * 1_000_000)

    def test_Term_debt分两段要相加(self) -> None:
        hits = extract.scan_pages([APPLE_BALANCE])
        self.assertEqual(hits["term_debt"].value_yuan, (12_350 + 78_328) * 1_000_000)

    def test_收入取合并口径而不是分部(self) -> None:
        """Products/Services 是分项，Total net sales 才是收入。"""
        hits = extract.scan_pages([APPLE_INCOME])
        self.assertEqual(hits["revenue"].value_yuan, 416_161 * 1_000_000)

    def test_合并口径英文也认(self) -> None:
        hits = extract.scan_pages([APPLE_BALANCE])
        self.assertGreaterEqual(hits["cash"].confidence, 0.9)
        self.assertNotIn("母公司报表", hits["cash"].note)

    def test_没有利息信息时EBIT取营业利润(self) -> None:
        values = {
            "revenue": 416_161.0,
            "equity_attributable": 73_733.0,
            "net_profit": 112_010.0,
            "total_profit": 132_729.0,
            "income_tax": 20_719.0,
            "operating_income": 133_050.0,
            "depreciation_fixed_assets": 11_698.0,
        }
        built = schema.build_peer_metrics(values)
        self.assertAlmostEqual(built.values["net_profit_attributable"], 112_010.0)
        self.assertAlmostEqual(built.values["ebit"], 133_050.0)
        self.assertAlmostEqual(built.values["ebitda"], 133_050.0 + 11_698.0)

    def test_美国口径的有息负债(self) -> None:
        built = schema.build_opening_balance(
            {
                "cash": 35_934.0,
                "accounts_receivable": 39_777.0,
                "inventory": 5_718.0,
                "fixed_assets": 49_834.0,
                "total_assets": 359_241.0,
                "accounts_payable": 69_860.0,
                "total_liabilities": 285_508.0,
                "equity_attributable": 73_733.0,
                "commercial_paper": 7_979.0,
                "term_debt": 90_678.0,
            }
        )
        v = built.values
        self.assertAlmostEqual(v["interest_bearing_debt"], 98_657.0)
        assets = sum(v[name] for name in schema.OPENING_FIELDS[:5])
        liabilities = v["accounts_payable"] + v["interest_bearing_debt"] + v["other_non_current_liabilities"]
        equity = v["share_capital"] + v["retained_earnings"] + v["minority_equity"]
        self.assertAlmostEqual(assets - liabilities - equity, 0.0, places=6)

    def test_币种识别(self) -> None:
        self.assertEqual(extract.detect_currency([APPLE_BALANCE]), "USD")
        self.assertEqual(extract.detect_currency(["单位：元 货币资金 1"]), "")
        self.assertEqual(extract.detect_currency(["（人民币百万元）"]), "CNY")

    def test_期末日期按财年而不是自然年(self) -> None:
        """Apple 的 FY2025 止于 9 月 27 日，不能写成 2025-12-31。"""
        pages = ["For the fiscal year ended September 27, 2025", APPLE_INCOME]
        self.assertEqual(extract.detect_period_end(pages), "2025-09-27")
        self.assertEqual(
            extract.detect_period_end(["本报告期截至2024年12月31日"]), "2024-12-31"
        )

    def test_as_of_用确切期末(self) -> None:
        record = extract.build_record(
            code="AAPL",
            name="Apple",
            year=2025,
            pages=["For the fiscal year ended September 27, 2025", APPLE_BALANCE, APPLE_INCOME],
        )
        row = adapter.peer_from(record)
        self.assertEqual(row["as_of"], "2025-09-27")

    def _usd_record(self, code: str = "AAPL") -> dict:
        record = extract.build_record(code=code, name="Apple", year=2025, pages=[APPLE_BALANCE, APPLE_INCOME])
        record["currency"] = "USD"
        return record

    def test_外币报表不给汇率就报错(self) -> None:
        """把「美元百万」当「人民币元」直接用，是最危险的静默错误。"""
        with self.assertRaises(adapter.AdapterError) as ctx:
            adapter.build_pipeline_input([self._usd_record()], target_code="AAPL")
        self.assertIn("必须显式给汇率", str(ctx.exception))

    def test_给汇率后按汇率换算并留痕(self) -> None:
        payload = adapter.build_pipeline_input(
            [self._usd_record()], target_code="AAPL", fx_rate=7.0
        )
        self.assertEqual(payload["fx"]["from"], "USD")
        self.assertEqual(payload["fx"]["rate"], 7.0)
        # 现金 35,934（百万美元）→ 35,934 × 1e6 × 7.0 元
        self.assertAlmostEqual(payload["opening_balance"]["cash"], 35_934 * 1_000_000 * 7.0)
        self.assertAlmostEqual(payload["target_actuals"]["revenue"], 416_161 * 1_000_000 * 7.0)

    def test_人民币报表不受影响(self) -> None:
        record = extract.build_record(
            code="600519", name="贵州茅台", year=2024, pages=[PAGE_1, PAGE_2]
        )
        payload = adapter.build_pipeline_input([record], target_code="600519")
        self.assertFalse(payload["fx"]["converted"])
        self.assertEqual(payload["fx"]["rate"], 1.0)


# ---------------------------------------------------------------------------
# 接流水线
# ---------------------------------------------------------------------------


class AdapterTest(unittest.TestCase):
    def _records(self) -> list[dict]:
        target = extract.build_record(
            code="600519", name="贵州茅台", year=2024, pages=[PAGE_1, PAGE_2]
        )
        peer = extract.build_record(
            code="000858", name="五粮液", year=2024, pages=[PAGE_1, PAGE_2]
        )
        peer["accounts"] = dict(peer["accounts"])
        return [target, peer]

    def test_汇总出的基期表能被流水线读取(self) -> None:
        import pipeline.config as config

        payload = adapter.build_pipeline_input(self._records(), target_code="600519")
        with tempfile.TemporaryDirectory() as tmp:
            path = adapter.write_pipeline_input(payload, Path(tmp) / "pipeline_input.json")
            original = config.DEFAULT_EXTRACTED_PAYLOAD
            config.DEFAULT_EXTRACTED_PAYLOAD = path
            try:
                opening = config.load_opening_balance(source="extracted")
                peers = config.load_peer_financials(source="extracted")
                actuals = config.load_target_actuals(source="extracted")
            finally:
                config.DEFAULT_EXTRACTED_PAYLOAD = original

        self.assertAlmostEqual(opening.cash, 5_000_000)
        self.assertAlmostEqual(opening.balance_gap, 0.0, places=6)
        self.assertEqual(sorted(peers), ["000858"])
        self.assertAlmostEqual(peers["000858"].ebitda, 2_500_000)
        self.assertIsNotNone(actuals)
        assert actuals is not None
        self.assertAlmostEqual(actuals["revenue"], 10_000_000)

    def test_数据源设成抽取但文件不在时必须报错而不是回退(self) -> None:
        import pipeline.config as config

        original = config.DEFAULT_EXTRACTED_PAYLOAD
        config.DEFAULT_EXTRACTED_PAYLOAD = Path(tempfile.gettempdir()) / "不存在_pipeline_input.json"
        try:
            with self.assertRaises(config.ConfigError) as ctx:
                config.load_opening_balance(source="extracted")
        finally:
            config.DEFAULT_EXTRACTED_PAYLOAD = original
        self.assertIn("抽取结果不存在", str(ctx.exception))

    def test_生成的peers表能被原有的读取器读回(self) -> None:
        import pipeline.config as config

        payload = adapter.build_pipeline_input(self._records(), target_code="600519")
        with tempfile.TemporaryDirectory() as tmp:
            path = adapter.write_peers_csv(payload["peers"].values(), Path(tmp) / "peers.csv")
            rows = config.load_peer_financials(path)

        self.assertEqual(sorted(rows), ["000858"])
        self.assertAlmostEqual(rows["000858"].revenue, 10_000_000)
        self.assertEqual(rows["000858"].as_of, "2024A")

    def test_占位符替换不会静默吞掉没命中的键(self) -> None:
        text, unmatched = adapter.render_placeholders(
            "期初现金 [cash] 元，EBITDA [ebitda] 元，未知 [不存在的键]",
            {"cash": 1234.5, "ebitda": 999.0},
        )
        self.assertIn("1,234.50", text)
        self.assertIn("[不存在的键]", text)
        self.assertEqual(unmatched, ["不存在的键"])


# ---------------------------------------------------------------------------
# Firecrawl 客户端
# ---------------------------------------------------------------------------


class FirecrawlTest(unittest.TestCase):
    def test_没有密钥时明确报不可用(self) -> None:
        client = Firecrawl(api_key="")
        self.assertFalse(client.available)
        with self.assertRaises(FirecrawlUnavailable):
            client.scrape("https://example.com")

    def test_结构化抽取走v2形状(self) -> None:
        calls: list[tuple[str, dict]] = []

        def fake(url: str, payload: dict, **kwargs: object) -> dict:
            calls.append((url, payload))
            return {
                "success": True,
                "data": {
                    "json": {"unit": "千元", "accounts": [{"name": "货币资金", "value": 100, "page": 3}]},
                    "markdown": "...",
                },
            }

        client = Firecrawl(api_key="k", transport=fake)
        result = client.extract("https://x/y.pdf", {"type": "object"})

        self.assertEqual(result["unit"], "千元")
        self.assertTrue(calls[0][0].endswith("/v2/scrape"))
        self.assertIn("parsers", calls[0][1])

    def test_v2不认时回退到v1(self) -> None:
        seen: list[str] = []

        def fake(url: str, payload: dict, **kwargs: object) -> dict:
            seen.append(url)
            if url.endswith("/v2/scrape"):
                raise HttpError("HTTP 400 for /v2/scrape", status=400, url=url)
            return {"success": True, "data": {"markdown": "ok"}}

        client = Firecrawl(api_key="k", transport=fake)
        data = client.scrape("https://example.com")

        self.assertEqual(data.get("markdown"), "ok")
        self.assertTrue(any(url.endswith("/v1/scrape") for url in seen))

    def test_兜底结果转成内部记录(self) -> None:
        record = extract.record_from_firecrawl(
            {
                "unit": "万元",
                "accounts": [
                    {"name": "货币资金", "value": 10, "page": 2, "raw_text": "货币资金 10"},
                    {"name": "某个没见过的科目", "value": 3},
                ],
            },
            code="600519",
            name="贵州茅台",
            year=2024,
            source_url="https://example.com/a.pdf",
        )
        self.assertEqual(record["accounts"]["cash"]["value_yuan"], 100_000.0)
        self.assertEqual(record["unmapped"][0]["name"], "某个没见过的科目")
        self.assertEqual(record["method"], "firecrawl-json")


# ---------------------------------------------------------------------------
# 下载编排
# ---------------------------------------------------------------------------


class DownloaderTest(unittest.TestCase):
    def test_已有同名PDF时跳过下载(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            pdf = folder / "600519_2024.pdf"
            pdf.write_bytes(b"%PDF-1.7" + b"0" * 5000)
            outcome = fetch_one(
                targets_mod.Target(ticker="600519", name="贵州茅台", years=(2024,)),
                2024,
                out_dir=folder,
            )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.method, "cached")
        self.assertEqual(len(outcome.sha256), 64)

    def test_手工入库认文件名并拒绝非PDF(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            good = folder / "source" / "000858_2024.pdf"
            good.parent.mkdir(parents=True, exist_ok=True)
            good.write_bytes(b"%PDF-1.7" + b"0" * 2000)
            bad = folder / "source" / "五粮液年报.pdf"
            bad.write_bytes(b"%PDF-1.7")
            not_pdf = folder / "source" / "000858_2023.pdf"
            not_pdf.write_bytes(b"<html>login</html>")

            outcomes = import_local([good, bad, not_pdf], out_dir=folder / "raw_pdf")

        self.assertTrue(outcomes[0].ok)
        self.assertFalse(outcomes[1].ok)
        self.assertIn("文件名", outcomes[1].note)
        self.assertFalse(outcomes[2].ok)

    def test_manifest记账后读得回来(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            save_manifest(
                [FetchOutcome(code="600519", year=2024, ok=True, method="cninfo", sha256="x")],
                path,
            )
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("600519_2024", data["entries"])
        self.assertEqual(data["entries"]["600519_2024"]["method"], "cninfo")


# ---------------------------------------------------------------------------
# 行情表
# ---------------------------------------------------------------------------


GOOD_MARKET = """# 注释
code,name,role,price,diluted_shares,interest_bearing_debt,cash,minority_interest,preferred_stock,non_operating_assets,as_of
600519,贵州茅台,target,1500.00,1256197800,0,50000000000,0,0,0,2026-09-30
000858,五粮液,peer,130.50,3881600000,5000000000,100000000000,0,0,0,2026-09-30
"""


class MarketTest(unittest.TestCase):
    def test_模板能被自己的校验器接受(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = market.write_template(Path(tmp) / "market.csv", as_of="2026-09-30")
            report = market.validate(path)
        self.assertFalse(report.ok)
        self.assertTrue(any("必须大于 0" in error or "是空的" in error for error in report.errors))

    def test_合法的表通过并给出基准日提示(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "market.csv", GOOD_MARKET)
            report = market.validate(path)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.target["code"], "600519")
        self.assertEqual(len(report.peers), 1)
        self.assertTrue(any("基准日" in warning for warning in report.warnings))

    def test_基准日不一致要拦住(self) -> None:
        broken = GOOD_MARKET.replace("2026-09-30\n", "2026-09-29\n", 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "market.csv", broken)
            report = market.validate(path)
        self.assertFalse(report.ok)
        self.assertTrue(any("as_of 不一致" in error for error in report.errors))

    def test_表头多一列会被流水线拒绝(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                Path(tmp) / "market.csv",
                GOOD_MARKET.replace("code,name,role", "code,name,role,net_profit"),
            )
            report = market.validate(path)
        self.assertFalse(report.ok)
        self.assertTrue(any("多了这些列" in error for error in report.errors))

    def test_与定标清单对不上时给提示(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "market.csv", GOOD_MARKET)
            report = market.validate(
                path, targets=[targets_mod.Target(ticker="300750", name="宁德时代", years=(2024,))]
            )
        self.assertTrue(report.ok)
        self.assertTrue(any("不在行情表里" in warning for warning in report.warnings))


if __name__ == "__main__":
    unittest.main()
