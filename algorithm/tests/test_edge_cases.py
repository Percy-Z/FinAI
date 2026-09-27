"""边界与异常数据的回归测试。

这里的每一条都对应一次代码审查里发现的问题：NaN 混进来、数据全零、
可比公司全亏、还款超过债务之类。修完一处就在这里加一条，防止以后再犯。
"""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from algorithm.finai.comps import (
    CompanyMetrics,
    FootballField,
    FootballFieldBand,
    build_stats,
    comps_bands,
    comps_valuation,
    football_field,
    implied_from_multiple,
)
from algorithm.finai.dcf import value_dcf
from algorithm.finai.forecast import (
    BalanceSheetState,
    ForecastDrivers,
    build_forecast,
)
from algorithm.finai.sensitivity import Variable, two_way_grid
from algorithm.finai.statements import (
    BalanceSheet,
    CashFlowStatement,
    IncomeStatement,
    ReportSnapshot,
)
from algorithm.finai.types import ForecastAssumptions, MarketData, WaccInputs
from algorithm.finai.validate import Tolerance, check_data_presence, run_checks
from algorithm.output import charts
from algorithm.output.reporting import (
    RunOutputs,
    export_run,
    validate_run_id,
    write_json,
)

NAN = float("nan")
INF = float("inf")


def clean_assumptions(**overrides) -> ForecastAssumptions:
    params: dict[str, Any] = dict(
        revenue_base=100.0,
        revenue_growth=(0.0,) * 5,
        ebit_margin=(0.20,) * 5,
        tax_rate=0.25,
        da_pct_revenue=0.05,
        capex_pct_revenue=0.05,
        nwc_pct_revenue=0.10,
        terminal_growth=0.0,
    )
    params.update(overrides)
    return ForecastAssumptions(**params)


CLEAN_MARKET = MarketData(
    price=10.0, diluted_shares=10.0, interest_bearing_debt=40.0, cash=10.0
)

OPENING = BalanceSheetState(
    cash=55.0,
    accounts_receivable=60.0,
    inventory=40.0,
    fixed_assets=200.0,
    other_non_current_assets=30.0,
    accounts_payable=40.0,
    interest_bearing_debt=100.0,
    other_non_current_liabilities=25.0,
    share_capital=100.0,
    retained_earnings=120.0,
)


def drivers(**overrides) -> ForecastDrivers:
    params: dict[str, Any] = dict(
        revenue_base=400.0,
        base_year=2025,
        years=3,
        revenue_growth=0.10,
        gross_margin=0.40,
        opex_pct_revenue=0.15,
        da_pct_revenue=0.05,
        capex_pct_revenue=0.06,
        ar_pct_revenue=0.15,
        inventory_pct_revenue=0.10,
        ap_pct_revenue=0.10,
        interest_rate_on_debt=0.05,
        tax_rate=0.25,
        dividend_payout_ratio=0.30,
    )
    params.update(overrides)
    return ForecastDrivers(**params)


def target(**overrides) -> CompanyMetrics:
    params: dict[str, Any] = dict(
        code="T",
        name="目标公司",
        price=10.0,
        diluted_shares=100.0,
        net_profit_attributable=62.0,
        book_equity_attributable=220.0,
        revenue=440.0,
        ebit=88.0,
        ebitda=110.0,
        interest_bearing_debt=100.0,
        cash=55.0,
    )
    params.update(overrides)
    return CompanyMetrics(**params)


class TestNaNIsRejected(unittest.TestCase):
    """NaN 参与任何比较都是 False，普通的 x <= 0 拦不住它。"""

    def test_revenue_base(self):
        with self.assertRaises(ValueError) as ctx:
            clean_assumptions(revenue_base=NAN)
        self.assertIn("有限数字", str(ctx.exception))

    def test_revenue_growth_entry(self):
        with self.assertRaises(ValueError):
            clean_assumptions(revenue_growth=(0.1, NAN))

    def test_ebit_margin_entry(self):
        with self.assertRaises(ValueError):
            clean_assumptions(ebit_margin=(0.2, NAN))

    def test_market_price(self):
        with self.assertRaises(ValueError):
            replace(CLEAN_MARKET, price=NAN)

    def test_market_shares(self):
        with self.assertRaises(ValueError):
            replace(CLEAN_MARKET, diluted_shares=NAN)

    def test_market_cash(self):
        with self.assertRaises(ValueError):
            replace(CLEAN_MARKET, cash=INF)

    def test_wacc_inputs(self):
        with self.assertRaises(ValueError):
            WaccInputs(
                risk_free=NAN,
                beta=1.0,
                equity_risk_premium=0.05,
                cost_of_debt=0.03,
                tax_rate=0.25,
                equity_weight=0.9,
                debt_weight=0.1,
            )

    def test_company_price(self):
        with self.assertRaises(ValueError):
            target(price=NAN)

    def test_company_optional_metric(self):
        with self.assertRaises(ValueError):
            target(ebitda=NAN)

    def test_forecast_drivers(self):
        with self.assertRaises(ValueError):
            drivers(tax_rate=NAN)

    def test_nan_opening_balance_is_caught(self):
        """基期数据是外部输入，NaN 必须在构造时就拦住。

        否则 balance_gap 会变成 NaN，而 abs(nan) > 容差 是 False，
        平衡检查就形同虚设，预测会带着 NaN 一路算下去。
        """
        with self.assertRaises(ValueError) as ctx:
            replace(OPENING, cash=NAN)
        self.assertIn("有限数字", str(ctx.exception))

    def test_nan_in_any_opening_account_is_caught(self):
        for field in ("fixed_assets", "retained_earnings", "accounts_payable"):
            with self.assertRaises(ValueError, msg=field):
                replace(OPENING, **{field: NAN})


class TestNonFiniteNeverReachesOutput(unittest.TestCase):
    def test_write_json_turns_nan_into_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(
                Path(tmp) / "x.json", {"a": NAN, "b": INF, "c": [1.0, NAN]}
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsNone(payload["a"])
        self.assertIsNone(payload["b"])
        self.assertIsNone(payload["c"][1])

    def test_written_json_is_strictly_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(Path(tmp) / "x.json", {"a": NAN})
            raw = path.read_text(encoding="utf-8")
        self.assertNotIn("NaN", raw)
        json.loads(
            raw,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"非法常量 {token}")
            ),
        )

    def test_zero_ebitda_case_no_longer_writes_nan(self):
        """EBITDA 为 0 时隐含倍数是 NaN，写到文件里必须变成 null。"""
        assumptions = clean_assumptions(ebit_margin=(0.0,) * 5, da_pct_revenue=0.0)
        result = value_dcf(assumptions, CLEAN_MARKET, 0.10)
        self.assertNotEqual(result.implied_exit_multiple, result.implied_exit_multiple)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(Path(tmp) / "v.json", result.to_dict())
            self.assertNotIn("NaN", path.read_text(encoding="utf-8"))


class TestEmptyDataIsCaught(unittest.TestCase):
    """抽取失败时最常见的表现就是全零，不能让它报出假通过。"""

    def empty_snapshot(self) -> ReportSnapshot:
        return ReportSnapshot(
            fiscal_year=2025,
            balance=BalanceSheet(0.0, 0.0, 0.0),
            income=IncomeStatement(0.0, 0.0),
            cashflow=CashFlowStatement(0.0, 0.0, 0.0),
        )

    def test_all_zero_data_fails(self):
        result = check_data_presence(self.empty_snapshot())
        self.assertEqual(result.status, "fail")
        self.assertIn("什么都没抽到", result.note)

    def test_run_checks_reports_a_failure(self):
        report = run_checks(self.empty_snapshot())
        self.assertEqual(report.summary["fail"], 1)
        self.assertEqual(report.checks[0].check_id, "DQ-01")
        self.assertEqual(report.failures[0].check_id, "DQ-01")

    def test_zero_assets_fails_even_with_revenue(self):
        snapshot = ReportSnapshot(
            fiscal_year=2025,
            balance=BalanceSheet(0.0, 0.0, 0.0),
            income=IncomeStatement(500.0, 60.0),
            cashflow=CashFlowStatement(70.0, -30.0, -10.0),
        )
        result = check_data_presence(snapshot)
        self.assertEqual(result.status, "fail")
        self.assertIn("资产总计", result.note)

    def test_normal_data_passes(self):
        snapshot = ReportSnapshot(
            fiscal_year=2025,
            balance=BalanceSheet(1000.0, 400.0, 600.0),
            income=IncomeStatement(500.0, 60.0),
            cashflow=CashFlowStatement(70.0, -30.0, -10.0),
        )
        self.assertEqual(check_data_presence(snapshot).status, "pass")

    def test_zero_profit_is_mentioned_but_passes(self):
        snapshot = ReportSnapshot(
            fiscal_year=2025,
            balance=BalanceSheet(1000.0, 400.0, 600.0),
            income=IncomeStatement(500.0, 0.0),
            cashflow=CashFlowStatement(70.0, -30.0, -10.0),
        )
        result = check_data_presence(snapshot)
        self.assertEqual(result.status, "pass")
        self.assertIn("净利润", result.note)


class TestToleranceValidation(unittest.TestCase):
    def test_negative_absolute_rejected(self):
        with self.assertRaises(ValueError):
            Tolerance(absolute=-1.0)

    def test_negative_relative_rejected(self):
        with self.assertRaises(ValueError):
            Tolerance(relative=-1e-6)

    def test_nan_rejected(self):
        with self.assertRaises(ValueError):
            Tolerance(absolute=NAN)

    def test_infinite_scale_rejected(self):
        with self.assertRaises(ValueError):
            Tolerance().limit_for(INF)


class TestInfiniteStatementFigures(unittest.TestCase):
    def test_inf_does_not_pass_the_tie_check(self):
        """容差会跟着 inf 一起变成无穷大，必须先把非有限值拦下来。"""
        snapshot = ReportSnapshot(
            fiscal_year=2025,
            balance=BalanceSheet(INF, 400.0, 600.0),
            income=IncomeStatement(500.0, 60.0),
            cashflow=CashFlowStatement(70.0, -30.0, -10.0),
        )
        report = run_checks(snapshot)
        self.assertEqual(report.by_id("BS-01").status, "fail")
        self.assertIn("有限值", report.by_id("BS-01").note)


class TestCompsEdgeCases(unittest.TestCase):
    def test_non_finite_multiple_is_excluded_from_stats(self):
        peers = [
            target(code="A", ebitda=10.0, interest_bearing_debt=0.0, cash=0.0),
            target(code="B", ebitda=10.0, interest_bearing_debt=0.0, cash=0.0),
        ]
        entry = build_stats(peers, names=("ev_ebitda",))["ev_ebitda"]
        self.assertEqual(entry.count, 2)

    def test_negative_implied_equity_is_flagged(self):
        item = implied_from_multiple(
            target(interest_bearing_debt=5000.0), "ev_ebitda", 5.0
        )
        implied_equity = item.implied_equity_value
        self.assertIsNotNone(implied_equity)
        self.assertLess(implied_equity or 0.0, 0)
        self.assertIn("隐含权益价值为负", item.note)

    def test_missing_pair_is_skipped_with_warning(self):
        """可比公司全亏时 PE 横条不存在，重叠检查要跳过而不是崩掉。"""
        peers = [
            target(
                code=f"P{i}",
                net_profit_attributable=-5.0,
                book_equity_attributable=30.0,
            )
            for i in range(3)
        ]
        result = comps_valuation(target(), peers, multiples=("pe", "pb"))
        bands = [FootballFieldBand("DCF", 8.0, 9.3, 12.0)] + comps_bands(result)
        field = football_field(bands, price=10.0, check_pairs=(("DCF", "PE"),))
        self.assertEqual(len(field.bands), 2)
        self.assertTrue(any("跳过" in warning for warning in field.warnings))
        self.assertFalse(any("不重叠" in warning for warning in field.warnings))

    def test_existing_pair_still_checked(self):
        peers = [
            target(
                code=f"P{i}",
                net_profit_attributable=10.0,
                book_equity_attributable=30.0,
            )
            for i in range(3)
        ]
        result = comps_valuation(target(), peers, multiples=("pe",))
        bands = [FootballFieldBand("DCF", 100.0, 110.0, 120.0)] + comps_bands(result)
        field = football_field(bands, price=10.0, check_pairs=(("DCF", "PE"),))
        self.assertTrue(any("不重叠" in warning for warning in field.warnings))


class TestForecastWarnings(unittest.TestCase):
    def test_negative_debt_is_flagged(self):
        forecast = build_forecast(OPENING, drivers(net_debt_repayment=200.0))
        self.assertTrue(any("有息负债为" in w for w in forecast.warnings))

    def test_loss_year_tax_benefit_is_flagged(self):
        forecast = build_forecast(
            OPENING, drivers(gross_margin=0.10, opex_pct_revenue=0.30)
        )
        self.assertTrue(any("税收抵减" in w for w in forecast.warnings))

    def test_large_repayment_flags_interest_basis(self):
        forecast = build_forecast(OPENING, drivers(net_debt_repayment=30.0))
        self.assertTrue(any("利息" in w for w in forecast.warnings))

    def test_negative_equity_is_flagged(self):
        forecast = build_forecast(
            OPENING,
            drivers(
                gross_margin=0.05,
                opex_pct_revenue=0.40,
                dividend_payout_ratio=0.0,
            ),
        )
        self.assertTrue(any("所有者权益为负" in w for w in forecast.warnings))

    def test_clean_forecast_has_no_warnings(self):
        self.assertEqual(build_forecast(OPENING, drivers()).warnings, [])

    def test_one_warning_per_loss_year(self):
        forecast = build_forecast(
            OPENING, drivers(gross_margin=0.10, opex_pct_revenue=0.30)
        )
        tax_notes = [w for w in forecast.warnings if "税收抵减" in w]
        self.assertEqual(len(tax_notes), 3)


class TestDiagnosticsWording(unittest.TestCase):
    def test_zero_ebitda_does_not_print_nan(self):
        assumptions = clean_assumptions(ebit_margin=(0.0,) * 5, da_pct_revenue=0.0)
        notes = value_dcf(assumptions, CLEAN_MARKET, 0.10).diagnostics
        joined = " ".join(notes)
        self.assertNotIn("nan", joined.lower())
        self.assertIn("无法反算隐含退出倍数", joined)

    def test_negative_terminal_value_is_flagged(self):
        assumptions = clean_assumptions(
            ebit_margin=(-0.10,) * 5,
            da_pct_revenue=0.0,
            terminal_method="exit_multiple",
            exit_multiple=6.0,
        )
        result = value_dcf(assumptions, CLEAN_MARKET, 0.10)
        self.assertLess(result.terminal_value, 0)
        self.assertTrue(any("终值为负" in note for note in result.diagnostics))

    def test_zero_enterprise_value_is_explained(self):
        assumptions = clean_assumptions(
            ebit_margin=(0.0,) * 5,
            da_pct_revenue=0.0,
            capex_pct_revenue=0.0,
            nwc_pct_revenue=0.0,
        )
        result = value_dcf(assumptions, CLEAN_MARKET, 0.10)
        self.assertEqual(result.enterprise_value, 0.0)
        self.assertTrue(any("终值占比无法计算" in note for note in result.diagnostics))


class TestGridLookup(unittest.TestCase):
    def test_missing_value_raises_keyerror_with_options(self):
        grid = two_way_grid(
            clean_assumptions(),
            CLEAN_MARKET,
            0.10,
            Variable.bounds("g", "terminal_growth", 0.0, 0.02),
            Variable.bounds("WACC", "wacc", 0.09, 0.11),
            points=3,
        )
        with self.assertRaises(KeyError) as ctx:
            grid.cell(9.9, 0.10)
        self.assertIn("可选", str(ctx.exception))


class TestRunIdValidation(unittest.TestCase):
    def test_path_separator_rejected(self):
        with self.assertRaises(ValueError):
            validate_run_id("../evil")
        with self.assertRaises(ValueError):
            validate_run_id("a/b")

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            validate_run_id("")

    def test_dot_names_rejected(self):
        for name in (".", ".."):
            with self.assertRaises(ValueError):
                validate_run_id(name)

    def test_export_run_refuses_to_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                export_run(RunOutputs(), root=tmp, run_id="../evil")

    def test_normal_name_accepted(self):
        self.assertEqual(validate_run_id("20260101T000000"), "20260101T000000")


@unittest.skipUnless(charts.HAS_MATPLOTLIB, "没有安装 matplotlib")
class TestChartsRejectEmptyData(unittest.TestCase):
    def test_tornado_with_no_rows(self):
        with self.assertRaises(ValueError) as ctx:
            charts.render_tornado([], 10.0, "/tmp/_should_not_exist.png")
        self.assertIn("Variable", str(ctx.exception))

    def test_football_field_with_no_bands(self):
        with self.assertRaises(ValueError):
            charts.render_football_field(FootballField([], 10.0), "/tmp/_x.png")

    def test_validation_summary_with_no_reports(self):
        with self.assertRaises(ValueError):
            charts.render_validation_summary([], "/tmp/_y.png")

    def test_validation_summary_renders_single_year(self):
        snapshot = ReportSnapshot(
            fiscal_year=2025,
            balance=BalanceSheet(1000.0, 400.0, 600.0),
            income=IncomeStatement(500.0, 60.0),
            cashflow=CashFlowStatement(70.0, -30.0, -10.0),
        )
        reports = [run_checks(snapshot)]
        with tempfile.TemporaryDirectory() as tmp:
            path = charts.render_validation_summary(reports, Path(tmp) / "v.png")
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
