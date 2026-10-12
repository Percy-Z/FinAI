"""取数层核心测试：清单、检索解析、扫描、拼装、接流水线、行情表校验。"""
from __future__ import annotations

import json
import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from finai_web import adapter, cninfo, extract, market, schema
from finai_web import targets as targets_mod
from finai_web.downloader import FetchOutcome, fetch_one, import_local, save_manifest
from finai_web.firecrawl import Firecrawl, FirecrawlError, FirecrawlUnavailable
from finai_web.http import HttpError

# 仓库根目录：本文件在 finai_web/tests/ 下，往上退三级就是仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]


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


class PathsOverrideTest(unittest.TestCase):
    """FINAI_EXTRACTED_DIR 要连带把流水线输入的路径一起挪走。

    踩过的坑：这个开关原先只挪走抽取记录，流水线输入还写回仓库里那份 ——
    离线演示（以及任何在临时目录里跑的流程）会把真实数据的 pipeline_input.json
    覆盖成演示公司，下一条命令读到的就是 A/B/C 三家演示可比公司。
    """

    def test_抽取目录改了流水线输入也跟着改(self) -> None:
        from finai_web import config as cfg

        with mock.patch.dict(os.environ, {"FINAI_EXTRACTED_DIR": "X:/tmp/extracted"}):
            reloaded = importlib.reload(cfg)
            try:
                self.assertEqual(reloaded.PATHS.extracted, Path("X:/tmp/extracted"))
                self.assertEqual(
                    reloaded.PATHS.pipeline_input,
                    Path("X:/tmp/extracted/pipeline_input.json"),
                )
            finally:
                importlib.reload(cfg)  # 恢复，别影响后面的用例


class RowMatchingTest(unittest.TestCase):
    """真实年报的陷阱：科目名不能用子串匹配。

    下面几页的行文抄的是 000661 长春高新 2025 年年度报告里真实出现过的样子：

        「非流动负债合计」里含「负债合计」   → 抽到非流动负债，倒挤出的差额变负
        「固定资产改造」里含「固定资产」     → 抽到现金流量表的科目
        「报告期末，高新地产总资产72.27亿元」→ 一句叙述被当成报表数据

    这三类误匹配在演示用的合成数据上遇不到，真实年报上第一条就中。
    """

    PAGES = (
        # 第 1 页：主要控股参股公司分析，一句叙述里提到子公司的总资产
        "3、主要控股参股公司分析\n"
        "报告期末，高新地产总资产72.27亿元，净资产24.81亿元，资产负债率65.66%。\n",
        # 第 2 页：合并资产负债表（我们真正要的那张表）
        "合并资产负债表\n单位：元\n"
        "货币资金 2,163,960,335.12 6,097,332,986.00\n"
        "应收账款 2,030,861,178.45 2,721,104,819.36\n"
        "固定资产 4,500,000,000.00 4,600,000,000.00\n"
        "资产总计 30,000,000,000.00 31,000,000,000.00\n"
        "应付账款 1,200,000,000.00 1,300,000,000.00\n"
        "流动负债合计 3,000,000,000.00 3,100,000,000.00\n"
        "非流动负债合计 245,938,105.89 2,716,067.33\n"
        "负债合计 8,000,000,000.00 8,100,000,000.00\n",
        # 第 3 页：现金流量表附注，里面也有「固定资产」开头的其他科目
        "固定资产改造 74,558,669.29 20,206,214.31\n"
        "固定资产折旧 300,000,000.00 280,000,000.00\n",
    )

    def hits(self):
        return extract.scan_pages(self.PAGES)

    def test_不被子串带偏(self):
        found = self.hits()
        self.assertEqual(found["total_liabilities"].value_yuan, 8_000_000_000.00)
        self.assertEqual(found["fixed_assets"].value_yuan, 4_500_000_000.00)

    def test_叙述句不算报表行(self):
        found = self.hits()
        self.assertEqual(found["total_assets"].value_yuan, 30_000_000_000.00)
        self.assertNotIn("高新地产", found["total_assets"].raw_text)

    def test_同样的行仍然能抽到(self):
        found = self.hits()
        self.assertEqual(found["cash"].value_yuan, 2_163_960_335.12)
        self.assertEqual(found["accounts_receivable"].value_yuan, 2_030_861_178.45)
        self.assertEqual(found["accounts_payable"].value_yuan, 1_200_000_000.00)

    # 真实年报的排法是「合并资产负债表 → 母公司资产负债表 → 合并利润表 → …」，
    # 而且一张表的尾部常常和下一张表的表头同页（000661 就是这样混排的）。
    PAGES_MIXED = (
        "合并资产负债表\n单位：元\n货币资金 100 110\n应付账款 40 45\n",
        "资产总计 300 310\n负债合计 141 150\n所有者权益合计 159 160\n",
        "母公司资产负债表\n单位：元\n货币资金 5 6\n",
        "负债合计 2 3\n合并利润表\n单位：元\n",  # 母公司表的尾行 + 下一张表的表头
        "营业总收入 1000 900\n",
    )

    def test_合并与母公司混排时取合并口径(self):
        found = extract.scan_pages(self.PAGES_MIXED)
        self.assertEqual(found["total_liabilities"].value_yuan, 141)
        self.assertEqual(found["accounts_payable"].value_yuan, 40)
        self.assertEqual(found["cash"].value_yuan, 100)
        self.assertEqual(found["total_assets"].value_yuan, 300)

    def test_利润表也按合并口径取(self):
        found = extract.scan_pages(self.PAGES_MIXED)
        self.assertEqual(found["revenue"].value_yuan, 1000)

    # 年报里还有两类假表头：目录页一页列好几张表的名字；附注标题里又出现一次
    # 「合并资产负债表项目注释」。两个都会把后面整片页误标成某张报表。
    PAGES_TOC = (
        "二、财务报表\n合并资产负债表 81\n母公司资产负债表 84\n母公司利润表 90\n",
        "合并资产负债表\n单位：元\n货币资金 100 110\n应付账款 40 45\n",
        "负债合计 50 55\n所有者权益合计 200 210\n",
        "母公司资产负债表\n单位：元\n货币资金 5 6\n",
        "负债合计 2 3\n",
        "七、合并财务报表项目注释\n合并资产负债表项目注释\n"
        "货币资金 100 110\n应付账款 999 998\n负债合计 50 55\n",
    )

    def test_目录页与附注标题不算报表表头(self):
        found = extract.scan_pages(self.PAGES_TOC)
        self.assertEqual(found["total_liabilities"].value_yuan, 50)
        self.assertEqual(found["accounts_payable"].value_yuan, 40)
        self.assertEqual(found["cash"].value_yuan, 100)


class ConsoleEncodingTest(unittest.TestCase):
    """中文 Windows 的控制台是 GBK，入口脚本不能因为打不出字就崩。

    实测：``python finai_web/examples/offline_demo.py`` 打印「✓」时抛
    ``UnicodeEncodeError: 'gbk' codec can't encode character '\\u2713'``。
    真实控制台里 Python 走 Windows 控制台 API（按 UTF-8 编码）不会崩，所以这个坑
    只在输出被重定向、被别的程序捕获、CI 里出现 —— 恰恰是评审机器上最容易踩到的
    形态。这里用 ``PYTHONIOENCODING=gbk`` 把它搬进测试。

    副作用要说明：这条用例真的会跑一遍离线演示，产物落在 data/extracted/ 下
    （已在 .gitignore 里，属于正常产出，不是垃圾）。
    """

    def run_in_gbk_console(
        self,
        args: list[str],
        timeout: int = 120,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """在 GBK 环境下跑一个子进程并取回输出（编码 GBK + errors strict）。"""
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "gbk"
        # 这两个会把子进程带偏（UTF-8 模式、数据源），与要测的东西无关
        for name in ("PYTHONUTF8", "FINAI_DATA_SOURCE"):
            env.pop(name, None)
        env.update(env_extra or {})
        return subprocess.run(
            args,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            encoding="gbk",  # 子进程按 GBK 输出，就按 GBK 读回来
            errors="replace",
            timeout=timeout,
        )

    def test_离线演示在_gbk_控制台下不会崩(self) -> None:
        """离线演示要打「✓ 代码 年份 科目数」—— 那一行过去会直接把脚本打断。

        产物写到临时目录（FINAI_EXTRACTED_DIR）：这个脚本会造出 T/A/B/C 四份
        演示记录，直接落在 data/extracted/ 的话，会把它们混进下一次
        `finai_web build` 的流水线输入里（真实公司旁边多出几家演示公司）。
        测试不该污染工作数据。
        """
        with tempfile.TemporaryDirectory() as tmp:
            done = self.run_in_gbk_console(
                [
                    sys.executable,
                    str(REPO_ROOT / "finai_web" / "examples" / "offline_demo.py"),
                ],
                env_extra={"FINAI_EXTRACTED_DIR": tmp},
            )
        self.assertEqual(done.returncode, 0, done.stdout[-2000:] + done.stderr[-2000:])
        self.assertIn("pipeline_input.json", done.stdout)

    def test_校验不过时报错也要能打出来(self) -> None:
        """走 cli.main() 那条路：校验不过时逐条打「✗ 问题」。

        这条是刻意的「失败路径」—— 前面的用例证明成功路径能打「✓」，
        这里证明报错路径能打「✗」。两处都是 GBK 编不出来的字符，
        而真正需要看到报错的时候恰恰是出问题的时候。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "market.csv"
            path.write_text(
                GOOD_MARKET.replace("code,name,role", "code,name,role,net_profit"),
                encoding="utf-8",
            )
            done = self.run_in_gbk_console(
                [
                    sys.executable,
                    "-m",
                    "finai_web",
                    "market",
                    "--check",
                    "--csv",
                    str(path),
                ]
            )
        self.assertEqual(done.returncode, 1, done.stdout[-2000:] + done.stderr[-2000:])
        # 打 ✗ 那一行崩掉的话，走不到最后的结论
        self.assertIn("结论：不通过", done.stdout)
        self.assertNotIn("UnicodeEncodeError", done.stderr)


if __name__ == "__main__":
    unittest.main()
