"""年报抽取的单元测试。

测试用的 PDF 是现造的（PyMuPDF 画一张「像年报」的表），不是真实年报。
这样做的好处是数字全部可预期，能精确断言；坏处是它证明不了对真实年报的
抽取率 —— 那件事只能在拿到真实年报后人工核对。

所以这里测的是「抽取逻辑对不对」，不是「抽取率够不够高」。
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from algorithm.finai.extract import (
    ACCOUNT_LABELS,
    HAS_PDFPLUMBER,
    compare_with_opening,
    extract_financials,
    format_extraction,
)

try:
    import fitz  # PyMuPDF，只用来造测试 PDF
except ImportError:  # pragma: no cover
    fitz = None

HAS_FITZ = fitz is not None

BALANCE_LINES = [
    "单位：元",
    "货币资金                88,180,000.00        55,000,000.00",
    "应收账款                66,000,000.00        60,000,000.00",
    "存货                    44,000,000.00        40,000,000.00",
    "固定资产               204,400,000.00       200,000,000.00",
    "资产总计               432,580,000.00       385,000,000.00",
    "应付账款                44,000,000.00        40,000,000.00",
    "负债合计               169,000,000.00       165,000,000.00",
    "所有者权益合计         263,580,000.00       220,000,000.00",
]

INCOME_LINES = [
    "单位：元",
    "营业总收入             440,000,000.00       400,000,000.00",
    "净利润                  62,250,000.00        55,000,000.00",
    "归属于母公司股东的净利润 62,250,000.00        55,000,000.00",
]

CASHFLOW_LINES = [
    "单位：元",
    "经营活动产生的现金流量净额  78,250,000.00    70,000,000.00",
]


def make_pdf(path: Path, pages: list[tuple[str, list[str]]]) -> Path:
    """用 PyMuPDF 造一份「像年报」的 PDF。中文要用内置的 china-s 字体。"""
    assert fitz is not None  # 调用方都用 skipUnless 守着
    doc = fitz.open()
    for title, lines in pages:
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 60), title, fontname="china-s", fontsize=13)
        y = 90
        for line in lines:
            page.insert_text((50, y), line, fontname="china-s", fontsize=10)
            y += 18
    doc.save(path)
    doc.close()
    return path


@unittest.skipUnless(HAS_PDFPLUMBER, "没有安装 pdfplumber")
@unittest.skipUnless(HAS_FITZ, "没有安装 PyMuPDF，造不出测试 PDF")
class TestExtractFromTextPdf(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pdf = make_pdf(
            Path(self.tmp.name) / "annual.pdf",
            [
                ("示例公司 2025 年年度报告", ["单位：元"]),
                ("合并资产负债表", BALANCE_LINES),
                ("合并利润表", INCOME_LINES),
                ("合并现金流量表", CASHFLOW_LINES),
            ],
        )
        self.result = extract_financials(self.pdf)

    def test_detects_text_layer(self):
        self.assertTrue(self.result.has_text_layer)
        self.assertEqual(self.result.pages, 4)

    def test_finds_every_expected_account(self):
        self.assertEqual(self.result.missing, [], f"没抽到：{self.result.missing}")
        self.assertEqual(len(self.result.matches), len(ACCOUNT_LABELS))

    def test_values_are_correct(self):
        expected = {
            "revenue": 440_000_000.00,
            "net_profit": 62_250_000.00,
            "net_profit_attributable": 62_250_000.00,
            "total_assets": 432_580_000.00,
            "total_liabilities": 169_000_000.00,
            "total_equity": 263_580_000.00,
            "cash": 88_180_000.00,
            "accounts_receivable": 66_000_000.00,
            "inventory": 44_000_000.00,
            "fixed_assets": 204_400_000.00,
            "accounts_payable": 44_000_000.00,
            "operating_cash_flow": 78_250_000.00,
        }
        for account, want in expected.items():
            got = self.result.matches[account].value_yuan
            assert got is not None, account
            self.assertAlmostEqual(got, want, places=2, msg=account)

    def test_every_match_carries_page_and_quote(self):
        """每个数字都要能说清从哪来 —— 这是「数据来源可核验」的最小要求。"""
        for account, match in self.result.matches.items():
            self.assertGreater(match.page, 0, account)
            self.assertTrue(match.raw_text.strip(), account)
            self.assertIn(match.label, match.raw_text, account)

    def test_balance_sheet_items_land_on_page_2(self):
        for account in ("total_assets", "cash", "accounts_payable"):
            self.assertEqual(self.result.matches[account].page, 2, account)

    def test_income_items_land_on_page_3(self):
        self.assertEqual(self.result.matches["revenue"].page, 3)

    def test_consolidated_pages_get_higher_confidence(self):
        # 「合并资产负债表」出现在页面上，置信度应该是高的那一档
        self.assertGreaterEqual(self.result.matches["total_assets"].confidence, 0.9)

    def test_format_extraction_is_readable(self):
        text = format_extraction(self.result)
        self.assertIn("年报抽取", text)
        self.assertIn("440,000,000.00", text)
        self.assertIn("第 3 页", text)
        self.assertIn("原文：", text)

    def test_subset_extraction(self):
        result = extract_financials(self.pdf, accounts=["revenue", "cash"])
        self.assertEqual(sorted(result.matches), ["cash", "revenue"])


@unittest.skipUnless(HAS_PDFPLUMBER, "没有安装 pdfplumber")
@unittest.skipUnless(HAS_FITZ, "没有安装 PyMuPDF")
class TestEdgeCases(unittest.TestCase):
    def test_unit_is_normalised_to_yuan(self):
        """「单位：万元」要换算成元。不换的话会差四个数量级。"""
        with TemporaryDirectory() as tmp:
            pdf = make_pdf(
                Path(tmp) / "wan.pdf",
                [("合并资产负债表", ["单位：万元", "资产总计  43,258.00  38,500.00"])],
            )
            result = extract_financials(pdf)
        match = result.matches["total_assets"]
        self.assertEqual(match.unit, "万元")
        assert match.value_yuan is not None
        self.assertAlmostEqual(match.value_yuan, 432_580_000.00, places=2)

    def test_scanned_pdf_is_reported_not_silently_empty(self):
        """没有文字层的 PDF 要明说，不能返回空结果让人以为文件坏了。"""
        with TemporaryDirectory() as tmp:
            pdf = make_pdf(Path(tmp) / "blank.pdf", [("扫描件", [])])
            result = extract_financials(pdf)
        self.assertFalse(result.has_text_layer)
        self.assertEqual(result.matches, {})
        self.assertTrue(any("扫描件" in note for note in result.notes))

    def test_missing_file_is_reported(self):
        result = extract_financials("/tmp/这个文件不存在.pdf")
        self.assertTrue(any("不存在" in note for note in result.notes))

    def test_ratio_rows_are_not_mistaken_for_balances(self):
        """「应收账款周转率」这类衍生指标行不能被当成应收账款。"""
        with TemporaryDirectory() as tmp:
            pdf = make_pdf(
                Path(tmp) / "ratio.pdf",
                [("合并资产负债表", ["单位：元", "应收账款周转率  3.5  3.2"])],
            )
            result = extract_financials(pdf)
        self.assertNotIn("accounts_receivable", result.matches)


class TestCompareWithOpening(unittest.TestCase):
    def test_differences_are_reported(self):
        from algorithm.finai.extract import AccountMatch, ExtractionResult
        from algorithm.finai.forecast import BalanceSheetState

        opening = BalanceSheetState(
            cash=55.0, accounts_receivable=60.0, inventory=40.0,
            fixed_assets=200.0, other_non_current_assets=30.0,
            accounts_payable=40.0, interest_bearing_debt=100.0,
            other_non_current_liabilities=25.0, share_capital=100.0,
            retained_earnings=120.0,
        )
        result = ExtractionResult(pdf="x")
        result.matches["cash"] = AccountMatch(
            account="cash", label="货币资金", value_yuan=88_180_000.0,
            raw_text="货币资金 88,180,000.00", page=2, unit="元", confidence=0.9,
        )
        differences = compare_with_opening(result, opening)
        self.assertEqual(len(differences), 1)
        self.assertIn("cash", differences[0])
        self.assertIn("手填", differences[0])

    def test_matching_values_produce_no_difference(self):
        from algorithm.finai.extract import AccountMatch, ExtractionResult
        from algorithm.finai.forecast import BalanceSheetState

        opening = BalanceSheetState(
            cash=100.0, accounts_receivable=0.0, inventory=0.0,
            fixed_assets=0.0, other_non_current_assets=0.0,
            accounts_payable=0.0, interest_bearing_debt=0.0,
            other_non_current_liabilities=0.0, share_capital=0.0,
            retained_earnings=0.0,
        )
        result = ExtractionResult(pdf="x")
        result.matches["cash"] = AccountMatch(
            account="cash", label="货币资金", value_yuan=100.0,
            raw_text="货币资金 100.00", page=1, unit="元", confidence=0.9,
        )
        self.assertEqual(compare_with_opening(result, opening), [])


if __name__ == "__main__":
    unittest.main()
