"""假设草稿的测试：能不能从取数结果生成一份「配置层真读得进去」的 assumptions.toml。

最后那一组（TestRendersLoadableConfig）是集成检查 —— 生成的 TOML 必须能被
pipeline.config 的各个 load_* 函数读成对象。只对着字符串断言是不够的：
字段名拼错、类型写错、小节漏掉，都要到流水线跑起来才炸。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline import assumptions_gen as gen  # noqa: E402
from pipeline import config  # noqa: E402


def account(value: float, page: int = 80) -> dict:
    return {
        "value_yuan": value,
        "label": "科目",
        "page": page,
        "raw_text": f"科目 {value:,.2f}",
        "unit": "元",
        "scale": 1.0,
        "confidence": 0.95,
        "note": "",
    }


def record(
    *,
    year: int,
    revenue: float,
    operating_cost: float,
    net_profit: float,
    total_profit: float = 100.0,
    interest: float = 10.0,
) -> dict:
    accounts = {
        "revenue": account(revenue, 87),
        "operating_cost": account(operating_cost, 88),
        "net_profit_attributable": account(net_profit, 88),
        "total_profit": account(total_profit, 88),
        "interest_expense": account(interest, 155),
        "depreciation_fixed_assets": account(20.0, 159),
        "amortization_intangible": account(5.0, 160),
        "amortization_lt_prepaid": account(5.0, 160),
        "accounts_receivable": account(200.0, 81),
        "inventory": account(300.0, 82),
        "accounts_payable": account(50.0, 83),
        "cash": account(55.0, 81),
        "fixed_assets": account(200.0, 82),
        "total_assets": account(385.0, 84),
        "total_liabilities": account(165.0, 84),
        "share_capital": account(100.0, 84),
        "minority_equity": account(0.0, 84),
    }
    return {
        "code": "000661",
        "name": "长春高新",
        "year": year,
        "period": f"{year}A",
        "pdf": f"000661_{year}.pdf",
        "sha256": "0" * 64,
        "pages": 200,
        "has_text_layer": True,
        "backend": "pdfplumber",
        "method": "cninfo",
        "source_url": "https://static.cninfo.com.cn/x.PDF",
        "extracted_at": "2026-10-14T22:00:00+08:00",
        "accounts": accounts,
        "notes": [],
        "opening_balance": {"values": {}, "provenance": {}, "notes": []},
        "peer": {},
        "missing_accounts": [],
        "cross_check": {},
    }


# 基期资产负债表必须配平，否则配置层会拦下来（这本身也是设计的一部分）
OPENING = {
    "cash": 55.0,
    "accounts_receivable": 60.0,
    "inventory": 40.0,
    "fixed_assets": 200.0,
    "other_non_current_assets": 30.0,
    "accounts_payable": 40.0,
    "interest_bearing_debt": 100.0,
    "other_non_current_liabilities": 25.0,
    "share_capital": 100.0,
    "retained_earnings": 120.0,
    "minority_equity": 0.0,
}


def make_company_dir(tmp: Path, *, records) -> Path:
    base = tmp / "companies" / "000661"
    (base / "extracted").mkdir(parents=True)
    for item in records:
        (base / "extracted" / f"000661_{item['year']}.json").write_text(
            json.dumps(item, ensure_ascii=False), encoding="utf-8"
        )
    (base / "extracted" / "pipeline_input.json").write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": "2026-10-14T22:00:00+08:00",
                "opening_balance": OPENING,
                "target_actuals": {"code": "000661"},
                "peers": {},
                "provenance": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (base / "company.json").write_text(
        json.dumps(
            {
                "version": 1,
                "target": {"code": "000661", "name": "长春高新", "org_id": "gssz0000661",
                           "years": [2026, 2025]},
                "peers": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (base / "manifest.json").write_text(
        json.dumps(
            {
                "entries": [
                    {"code": "000661", "year": 2026, "title": "2025年年度报告", "ok": True},
                    {"code": "000661", "year": 2025, "title": "2024年年度报告", "ok": True},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return base


class YearFactsTest(unittest.TestCase):
    def test_从记录里算出比例(self) -> None:
        facts = gen.year_facts(record(year=2026, revenue=1000.0, operating_cost=100.0, net_profit=80.0))
        self.assertAlmostEqual(facts["gross_margin"], 0.9)
        self.assertAlmostEqual(facts["da_pct"], 0.03)          # (20+5+5)/1000
        self.assertAlmostEqual(facts["ebit"], 110.0)           # 利润总额 + 利息费用
        self.assertAlmostEqual(facts["ebit_margin"], 0.11)

    def test_科目抽不到时是None不是0(self) -> None:
        """0 会被当成真值参与计算（比如费用率变成 0），None 才会走「需人工填」那条路。"""
        raw = record(year=2026, revenue=1000.0, operating_cost=100.0, net_profit=80.0)
        del raw["accounts"]["operating_cost"]
        facts = gen.year_facts(raw)
        self.assertIsNone(facts["gross_margin"])
        self.assertIsNone(facts["opex_pct"])


class NormalizationAdviceTest(unittest.TestCase):
    def test_盈利塌陷时建议归一(self) -> None:
        latest = record(year=2026, revenue=1000.0, operating_cost=900.0, net_profit=10.0)
        prior = record(year=2025, revenue=900.0, operating_cost=800.0, net_profit=90.0)
        advice = gen.normalization_advice(latest, prior)
        self.assertTrue(advice["applied"])
        # 归一到的年份是**报表年度**：year=2025 那条记录披露的是 2024 年年报
        self.assertEqual(advice["year"], 2024)
        self.assertIn("归母净利润", advice["reason"])

    def test_没有塌陷时不归一(self) -> None:
        latest = record(year=2026, revenue=1000.0, operating_cost=100.0, net_profit=80.0)
        prior = record(year=2025, revenue=900.0, operating_cost=90.0, net_profit=90.0)
        advice = gen.normalization_advice(latest, prior)
        self.assertFalse(advice["applied"])
        self.assertIn("没有出现盈利塌陷", advice["reason"])

    def test_只有一份年报时明说无法判断(self) -> None:
        advice = gen.normalization_advice(
            record(year=2026, revenue=1000.0, operating_cost=100.0, net_profit=80.0), None
        )
        self.assertFalse(advice["applied"])
        self.assertIn("无法判断", advice["reason"])


class DraftTest(unittest.TestCase):
    def test_状态分类与出处(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = make_company_dir(
                Path(tmp),
                records=[
                    record(year=2026, revenue=1000.0, operating_cost=100.0, net_profit=80.0),
                    record(year=2025, revenue=900.0, operating_cost=90.0, net_profit=90.0),
                ],
            )
            draft = gen.build_draft(base, beta=1.4561)

        self.assertAlmostEqual(draft.value("drivers.revenue_base"), 1000.0)
        self.assertEqual(draft.value("drivers.base_year"), 2025)  # 从标题「2025年年度报告」来
        self.assertAlmostEqual(draft.value("drivers.gross_margin"), 0.9)
        self.assertAlmostEqual(draft.value("drivers.da_pct_revenue"), 0.03)
        self.assertAlmostEqual(draft.value("drivers.capex_pct_revenue"), 0.03)  # 维持性口径
        self.assertAlmostEqual(draft.value("drivers.ar_pct_revenue"), 0.2)      # 200/1000
        self.assertAlmostEqual(draft.value("drivers.interest_rate_on_debt"), 0.1)  # 10/100
        self.assertAlmostEqual(draft.value("wacc.beta"), 1.4561)
        self.assertAlmostEqual(draft.value("wacc.cost_of_debt"), 0.1)
        self.assertEqual(draft.fields["drivers.tax_rate"].status, gen.DEFAULT)
        self.assertEqual(draft.fields["drivers.revenue_base"].status, gen.DERIVED)
        self.assertEqual(draft.fields["wacc.beta"].status, gen.DERIVED)
        # 分红比例抽不到：必须留白等人填，不能默认成 0
        self.assertEqual(draft.fields["drivers.dividend_payout_ratio"].status, gen.MISSING)
        self.assertEqual(gen.missing_fields(draft), ["drivers.dividend_payout_ratio"])
        self.assertIn("drivers.dividend_payout_ratio", draft.notes[-1])
        with self.assertRaises(gen.DraftError):
            gen.require_complete(draft)
        # 基期 11 个科目都在，且都标成自动推导（它们的出处是抽取记录）
        opening = [key for key in draft.fields if key.startswith("opening_balance.")]
        self.assertEqual(len(opening), 11)
        self.assertTrue(all(draft.fields[key].status == gen.DERIVED for key in opening))

    def test_归一命中时用上一年的盈利能力(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = make_company_dir(
                Path(tmp),
                records=[
                    record(year=2026, revenue=1000.0, operating_cost=900.0, net_profit=10.0),
                    record(year=2025, revenue=900.0, operating_cost=90.0, net_profit=90.0),
                ],
            )
            draft = gen.build_draft(base)
        self.assertTrue(draft.normalized["applied"])
        # 基期仍是最近一年，盈利能力取上一年：0.9 而不是 0.1
        self.assertAlmostEqual(draft.value("drivers.revenue_base"), 1000.0)
        self.assertAlmostEqual(draft.value("drivers.gross_margin"), 0.9)

    def test_缺流水线输入时明确报错(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "empty"
            (base / "extracted").mkdir(parents=True)
            (base / "extracted" / "000661_2026.json").write_text(
                json.dumps(record(year=2026, revenue=1.0, operating_cost=0.5, net_profit=0.1)),
                encoding="utf-8",
            )
            # 有清单、没流水线输入：报错要指出缺的是哪一个
            (base / "company.json").write_text(
                json.dumps({"target": {"code": "000661", "name": "长春高新"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaises(gen.DraftError) as caught:
                gen.build_draft(base)
        self.assertIn("pipeline_input", str(caught.exception))

    def test_缺公司清单时说明是哪一步缺(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "empty"
            (base / "extracted").mkdir(parents=True)
            with self.assertRaises(gen.DraftError) as caught:
                gen.build_draft(base)
        self.assertIn("company.json", str(caught.exception))


class RenderTest(unittest.TestCase):
    def _draft(self) -> gen.Draft:
        with tempfile.TemporaryDirectory() as tmp:
            base = make_company_dir(
                Path(tmp),
                records=[
                    record(year=2026, revenue=1000.0, operating_cost=100.0, net_profit=80.0),
                    record(year=2025, revenue=900.0, operating_cost=90.0, net_profit=90.0),
                ],
            )
            draft = gen.build_draft(base, beta=1.4561)
        # 模拟确认屏上人把抽不到的字段填上（分红比例不在抽取的科目表里）
        draft.set_value("drivers.dividend_payout_ratio", 0.415)
        return draft

    def test_没填的字段不会被写成0或None(self) -> None:
        """留白是对的，默认成 0 是错的：分红比例 0 的意思是「一分钱不分」。

        所以渲染时只留一行注释，让配置层在必填字段缺失时直接报错 —— 逼人来填，
        而不是悄悄拿一个假设的 0 去算。
        """
        draft = self._draft()
        draft.fields["drivers.dividend_payout_ratio"] = gen.Field(
            "drivers.dividend_payout_ratio", None, gen.MISSING, "年报里没有这个数，需要人填"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "assumptions.toml"
            gen.write_assumptions(draft, path)
            text = path.read_text(encoding="utf-8")
            with self.assertRaises(config.ConfigError):
                config.load_drivers(path)
        self.assertIn("# dividend_payout_ratio = ???", text)
        self.assertNotIn("dividend_payout_ratio = 0", text)

    def test_生成的TOML能被配置层读成对象(self) -> None:
        """集成检查：字段名、类型、小节结构都要对得上 ForecastDrivers 那些数据类。"""
        draft = self._draft()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "assumptions.toml"
            gen.write_assumptions(draft, path)
            company = config.load_company(path)
            drivers = config.load_drivers(path)
            wacc = config.load_wacc_params(path)
            terminal = config.load_terminal_params(path)
            opening = config.load_opening_balance(path, source="config")
            evidence = config.load_evidence(path)

        self.assertEqual(company.code, "000661")
        self.assertAlmostEqual(drivers.revenue_base, 1000.0)
        self.assertEqual(drivers.base_year, 2025)
        self.assertAlmostEqual(wacc.beta, 1.4561)
        self.assertEqual(terminal.terminal_method, "perpetuity")
        self.assertAlmostEqual(opening.cash, OPENING["cash"])
        self.assertIn("drivers.tax_rate", evidence)
        self.assertIn("opening_balance", evidence)

    def test_覆盖前备份且保留人写的东西(self) -> None:
        """人的东西三样不能丢：自定义的值、[evidence] 说明、[report] 运行参数。"""
        previous = (
            "[company]\n"
            'name = "长春高新技术产业（集团）股份有限公司"\n'
            'code = "000661"\n'
            "\n[drivers]\n"
            "revenue_base = 1.0\n"
            "tax_rate = 0.15\n"          # 人填的优惠税率：规则默认 25% 不能顶掉它
            "\n[report]\n"
            'model = "deepseek-flash"\n'
            "\n[evidence]\n"
            '"wacc.beta" = "人工写的依据：60 个月回归，相关系数 0.58"\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            # 公司目录里放着上一次确认过的 assumptions.toml —— 这正是真实流程里的样子
            base = make_company_dir(
                Path(tmp),
                records=[
                    record(year=2026, revenue=1000.0, operating_cost=100.0, net_profit=80.0),
                    record(year=2025, revenue=900.0, operating_cost=90.0, net_profit=90.0),
                ],
            )
            path = base / "assumptions.toml"
            path.write_text(previous, encoding="utf-8")
            draft = gen.build_draft(base, beta=1.4561)
            draft.set_value("drivers.dividend_payout_ratio", 0.415)
            written, backup = gen.write_assumptions(draft, path)
            text = written.read_text(encoding="utf-8")
            drivers = config.load_drivers(path)
            report = config.load_report_params(path)
            evidence = config.load_evidence(path)
            # 备份要在临时目录还在的时候断言 —— 出了 with 块目录就没了
            self.assertIsNotNone(backup)
            assert backup is not None
            self.assertTrue(backup.is_file())
            self.assertIn("tax_rate = 0.15", backup.read_text(encoding="utf-8"))

        self.assertIn("tax_rate = 0.15", text)
        self.assertAlmostEqual(drivers.tax_rate, 0.15, msg="人填的税率被规则默认顶掉了")
        self.assertEqual(report.model, "deepseek-flash", "[report] 小节应当整节沿用")
        self.assertIn("人工写的依据", evidence["wacc.beta"])
        self.assertAlmostEqual(drivers.revenue_base, 1000.0, msg="能算出来的字段应当更新")
        # 人写的公司全称也保留
        self.assertIn("长春高新技术产业", text)

    def test_草稿JSON往返(self) -> None:
        draft = self._draft()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / gen.DRAFT_NAME
            draft.save(path)
            back = gen.draft_for(Path(tmp))
        self.assertEqual(back.to_dict(), draft.to_dict())


if __name__ == "__main__":
    unittest.main()
