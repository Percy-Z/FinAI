"""三表联动预测的单元测试。

基期资产负债表（自行平衡）：
    现金 55 + 应收 60 + 存货 40 + 固定资产 200 + 其他非流动 30 = 资产 385
    应付 40 + 有息负债 100 + 其他非流动负债 25 = 负债 165
    股本 100 + 留存收益 120 + 少数股东 0 = 权益 220
    385 = 165 + 220
    经营性营运资本 = 60 + 40 - 40 = 60 = 基期收入 400 x 15%

第 1 年（2026）驱动假设：收入增速 10%、毛利率 40%、费用率 15%、
折旧摊销率 5%、资本开支率 5%、税率 25%、分红比例 30%、债务不还
    收入 440，毛利 176，费用 66，折旧 22
    EBIT = 176 - 66 - 22 = 88
    利息 = 100 x 5% = 5，税前 83，所得税 20.75，净利润 62.25
    应收 66、存货 44、应付 44；固定资产 200 + 22 - 22 = 200
    分红 = 62.25 x 30% = 18.675，留存收益 120 + 62.25 - 18.675 = 163.575
    经营现金流 = 62.25 + 22 - 6 - 4 + 4 = 78.25
    投资现金流 = -22，筹资现金流 = -18.675
    现金 = 55 + 37.575 = 92.575
    资产 = 92.575 + 66 + 44 + 200 + 30 = 432.575
    负债 + 权益 = 44 + 100 + 25 + 100 + 163.575 = 432.575
    FCFF = 88 x 0.75 + 22 - 22 - 6 = 60
"""

import unittest
from typing import Any
from dataclasses import replace

from algorithm.finai.dcf import enterprise_value
from algorithm.finai.forecast import (
    BALANCE_TOLERANCE,
    BalanceSheetState,
    ForecastDrivers,
    build_forecast,
    to_snapshots,
)
from algorithm.finai.types import ForecastAssumptions
from algorithm.finai.validate import run_checks

from algorithm.tests._helpers import assert_close  # noqa: E402


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
    minority_equity=0.0,
)

DRIVERS = ForecastDrivers(
    revenue_base=400.0,
    base_year=2025,
    years=5,
    revenue_growth=0.10,
    gross_margin=0.40,
    opex_pct_revenue=0.15,
    da_pct_revenue=0.05,
    capex_pct_revenue=0.05,
    ar_pct_revenue=0.15,
    inventory_pct_revenue=0.10,
    ap_pct_revenue=0.10,
    interest_rate_on_debt=0.05,
    tax_rate=0.25,
    dividend_payout_ratio=0.30,
)


def build(**overrides: Any):
    drivers = replace(DRIVERS, **overrides) if overrides else DRIVERS
    return build_forecast(OPENING, drivers)


class TestOpeningBalance(unittest.TestCase):
    def test_opening_balances(self):
        assert_close(self, OPENING.total_assets, 385.0)
        assert_close(self, OPENING.total_liabilities, 165.0)
        assert_close(self, OPENING.total_equity, 220.0)
        assert_close(self, OPENING.balance_gap, 0.0)

    def test_working_capital(self):
        assert_close(self, OPENING.working_capital, 60.0)

    def test_unbalanced_opening_rejected(self):
        broken = replace(OPENING, cash=OPENING.cash + 1.0)
        with self.assertRaises(ValueError) as ctx:
            build_forecast(broken, DRIVERS)
        self.assertIn("基期资产负债表本身不平", str(ctx.exception))

    def test_balance_gap_detects_error(self):
        broken = replace(OPENING, cash=OPENING.cash + 5.0)
        assert_close(self, broken.balance_gap, 5.0)


class TestDriversValidation(unittest.TestCase):
    def test_scalar_is_broadcast(self):
        drivers = replace(DRIVERS, revenue_growth=0.08)
        self.assertEqual(drivers.revenue_growth, (0.08,) * 5)
        self.assertEqual(len(drivers.series("revenue_growth")), drivers.years)

    def test_sequence_length_must_match(self):
        with self.assertRaises(ValueError):
            replace(DRIVERS, gross_margin=(0.4, 0.4))

    def test_invalid_tax_rate_rejected(self):
        with self.assertRaises(ValueError):
            replace(DRIVERS, tax_rate=0.9)

    def test_invalid_payout_rejected(self):
        with self.assertRaises(ValueError):
            replace(DRIVERS, dividend_payout_ratio=1.5)

    def test_invalid_minority_share_rejected(self):
        with self.assertRaises(ValueError):
            replace(DRIVERS, minority_share_of_profit=1.0)

    def test_negative_interest_rate_rejected(self):
        with self.assertRaises(ValueError):
            replace(DRIVERS, interest_rate_on_debt=-0.01)

    def test_non_positive_revenue_base_rejected(self):
        with self.assertRaises(ValueError):
            replace(DRIVERS, revenue_base=0.0)


class TestFirstYear(unittest.TestCase):
    def setUp(self):
        self.forecast = build()
        self.year = self.forecast.years[0]

    def test_year_label(self):
        self.assertEqual(self.year.year, 2026)

    def test_income_statement_matches_hand_calculation(self):
        income = self.year.income
        assert_close(self, income.revenue, 440.0)
        assert_close(self, income.gross_profit, 176.0)
        assert_close(self, income.cost_of_sales, 264.0)
        assert_close(self, income.operating_expenses, 66.0)
        assert_close(self, income.da, 22.0)
        assert_close(self, income.ebitda, 110.0)
        assert_close(self, income.ebit, 88.0)

    def test_interest_uses_opening_debt(self):
        assert_close(self, self.year.income.interest_expense, 5.0)

    def test_tax_and_net_profit(self):
        income = self.year.income
        assert_close(self, income.pretax_profit, 83.0)
        assert_close(self, income.income_tax, 20.75)
        assert_close(self, income.net_profit, 62.25)
        assert_close(self, income.net_profit_attributable, 62.25)

    def test_balance_sheet_items(self):
        balance = self.year.balance
        assert_close(self, balance.accounts_receivable, 66.0)
        assert_close(self, balance.inventory, 44.0)
        assert_close(self, balance.accounts_payable, 44.0)
        assert_close(self, balance.fixed_assets, 200.0)

    def test_retained_earnings_rollforward(self):
        # 120 + 62.25 - 62.25 x 30% = 163.575
        assert_close(self, self.year.balance.retained_earnings, 163.575)

    def test_cash_flow_statement(self):
        cashflow = self.year.cashflow
        assert_close(self, cashflow.operating, 78.25)
        assert_close(self, cashflow.investing, -22.0)
        assert_close(self, cashflow.financing, -18.675)
        assert_close(self, cashflow.net_change, 37.575)
        assert_close(self, cashflow.cash_end, 92.575)

    def test_delta_nwc(self):
        # 应收 +6、存货 +4、应付 +4 -> 营运资本增加 6
        assert_close(self, self.year.cashflow.delta_nwc, 6.0)

    def test_balance_sheet_balances(self):
        assert_close(self, self.year.balance.total_assets, 432.575)
        assert_close(self, self.year.balance.balance_gap, 0.0, places=9)

    def test_cash_from_statement_equals_cash_on_balance_sheet(self):
        assert_close(self, 
            self.year.balance.cash, self.year.cashflow.cash_end, places=9
        )

    def test_fcff(self):
        # 88 x 0.75 + 22 - 22 - 6 = 60
        assert_close(self, self.year.fcff, 60.0)


class TestWholeForecast(unittest.TestCase):
    def test_horizon(self):
        forecast = build()
        self.assertEqual(len(forecast.years), 5)
        self.assertEqual([y.year for y in forecast.years], [2026, 2027, 2028, 2029, 2030])

    def test_every_year_balances(self):
        """模型的现金由现金流量表推出，所以每一年都应该自动平。"""
        forecast = build()
        for gap in forecast.balance_gaps:
            assert_close(self, gap, 0.0, places=9)
        self.assertLess(forecast.worst_balance_gap, 1e-9)

    def test_revenue_compounds(self):
        forecast = build()
        assert_close(self, forecast.years[0].income.revenue, 440.0)
        assert_close(self, forecast.years[1].income.revenue, 484.0)
        assert_close(self, forecast.years[4].income.revenue, 400 * 1.1**5, places=6)

    def test_cash_accumulates(self):
        forecast = build()
        cash = [year.balance.cash for year in forecast.years]
        self.assertEqual(cash, sorted(cash))
        self.assertGreater(cash[-1], cash[0])

    def test_fcff_series(self):
        forecast = build()
        assert_close(self, forecast.fcff_series[0], 60.0)
        assert_close(self, forecast.fcff_series[1], 66.0)
        self.assertEqual(len(forecast.fcff_series), 5)

    def test_year_lookup(self):
        forecast = build()
        self.assertEqual(forecast.year(2027).year, 2027)
        with self.assertRaises(KeyError):
            forecast.year(2099)


class TestDriverEffects(unittest.TestCase):
    def test_higher_growth_raises_revenue(self):
        low = build(revenue_growth=0.05)
        high = build(revenue_growth=0.15)
        self.assertLess(
            low.years[-1].income.revenue, high.years[-1].income.revenue
        )

    def test_debt_repayment_reduces_debt_and_interest(self):
        forecast = build(net_debt_repayment=10.0)
        assert_close(self, forecast.years[0].balance.interest_bearing_debt, 90.0)
        assert_close(self, forecast.years[1].income.interest_expense, 4.5)
        # 还债是现金流出，筹资现金流更负
        self.assertLess(forecast.years[0].cashflow.financing, -18.675)

    def test_zero_payout_retains_all_profit(self):
        forecast = build(dividend_payout_ratio=0.0)
        assert_close(self, 
            forecast.years[0].balance.retained_earnings, 120.0 + 62.25
        )
        assert_close(self, forecast.years[0].cashflow.financing, 0.0)

    def test_full_payout_keeps_retained_earnings_flat(self):
        forecast = build(dividend_payout_ratio=1.0)
        assert_close(self, forecast.years[0].balance.retained_earnings, 120.0)

    def test_minority_share_splits_profit_and_still_balances(self):
        forecast = build(minority_share_of_profit=0.20)
        year = forecast.years[0]
        assert_close(self, year.income.minority_profit, 62.25 * 0.20, places=6)
        assert_close(self, 
            year.income.net_profit_attributable, 62.25 * 0.80, places=6
        )
        assert_close(self, year.balance.minority_equity, 62.25 * 0.20, places=6)
        assert_close(self, year.balance.balance_gap, 0.0, places=9)

    def test_higher_capex_lowers_fcff(self):
        low = build(capex_pct_revenue=0.03)
        high = build(capex_pct_revenue=0.08)
        self.assertGreater(low.fcff_series[0], high.fcff_series[0])


class TestConsistencyWithDcf(unittest.TestCase):
    """三表预测算出的 FCFF，应该与 dcf 模块简化路径的结果一致。

    让两者的假设对齐：
        毛利率 - 费用率 - 折旧摊销率 = EBIT 利润率
        0.40 - 0.15 - 0.05 = 0.20
        应收 + 存货 - 应付 = 营运资本占收入比
        0.15 + 0.10 - 0.10 = 0.15
    """

    def test_fcff_series_match(self):
        forecast = build()
        assumptions = ForecastAssumptions(
            revenue_base=400.0,
            revenue_growth=(0.10,) * 5,
            ebit_margin=(0.20,) * 5,
            tax_rate=0.25,
            da_pct_revenue=0.05,
            capex_pct_revenue=0.05,
            nwc_pct_revenue=0.15,
            terminal_growth=0.0,
        )
        from algorithm.finai.dcf import project

        simplified = project(assumptions)
        for heavy, light in zip(forecast.fcff_series, simplified.fcff):
            assert_close(self, heavy, light, places=9)

    def test_enterprise_value_helper_is_reachable(self):
        """两条路径都能算出企业价值，这里只确认接口没断。"""
        assumptions = ForecastAssumptions(
            revenue_base=400.0,
            revenue_growth=(0.10,) * 5,
            ebit_margin=(0.20,) * 5,
            tax_rate=0.25,
            da_pct_revenue=0.05,
            capex_pct_revenue=0.05,
            nwc_pct_revenue=0.15,
            terminal_growth=0.0,
        )
        value = enterprise_value(assumptions, 0.10)
        self.assertGreater(value, 0.0)


class TestValidationLoop(unittest.TestCase):
    """把预测结果直接喂给勾稽校验，形成闭环。"""

    def setUp(self):
        self.forecast = build()
        self.snapshots = to_snapshots(self.forecast)

    def test_snapshot_count_and_years(self):
        self.assertEqual(len(self.snapshots), 5)
        self.assertEqual(
            [snap.fiscal_year for snap in self.snapshots],
            [2026, 2027, 2028, 2029, 2030],
        )

    def test_every_year_passes_validation(self):
        for snapshot in self.snapshots:
            report = run_checks(snapshot)
            self.assertEqual(
                report.summary["fail"],
                0,
                f"{snapshot.fiscal_year} 年出现失败项：{[c.check_id for c in report.failures]}",
            )
            self.assertEqual(
                report.summary["warning"],
                0,
                f"{snapshot.fiscal_year} 年出现告警：{[c.check_id for c in report.warnings]}",
            )

    def test_non_recurring_check_is_skipped_not_passed(self):
        """预测里没有非经常性损益这项，检查应该报 skipped 而不是 pass。"""
        report = run_checks(self.snapshots[0])
        self.assertEqual(report.by_id("X-03").status, "skipped")
        self.assertEqual(report.summary["pass"], 10)
        self.assertEqual(report.summary["skipped"], 1)

    def test_receivable_check_runs_with_prior_year_data(self):
        """第二年的检查要用上一年的数据，说明联动传参是对的。"""
        report = run_checks(self.snapshots[1])
        result = report.by_id("X-01")
        self.assertEqual(result.status, "pass")
        assert_close(self, result.metric, 0.0, places=9)

    def test_broken_forecast_is_caught_by_validation(self):
        """故意改坏一年的留存收益，勾稽检查必须抓出来。"""
        snapshot = self.snapshots[0]
        retained = snapshot.balance.retained_earnings
        self.assertIsNotNone(retained)
        broken = replace(
            snapshot,
            balance=replace(
                snapshot.balance,
                retained_earnings=(retained or 0.0) + 10.0,
            ),
        )
        report = run_checks(broken)
        self.assertIn("BS-03", [check.check_id for check in report.failures])


class TestBalanceTolerance(unittest.TestCase):
    """百亿级报表的浮点残差不该被判成「数据不平」。

    真实案例（2026-10-12）：长春高新 2025 年报的基期表，资产侧与负债权益侧都是
    30,080,522,676.54 元，双精度求和后差 3.8e-06 元 —— 比该量级的最小间隔
    （ULP ≈ 6.7e-06 元）还小，属于浮点表示误差，但原来的 1e-6 阈值直接把它拦下，
    真实公司口径根本进不了流水线。

    改成按「半分钱」卡：年报金额只精确到分，真错到 1 分钱仍要拦得住。
    """

    OPENING = {
        "cash": 2163960335.12,
        "accounts_receivable": 2030861178.45,
        "inventory": 5833469318.15,
        "fixed_assets": 7262297034.86,
        "other_non_current_assets": 12789934809.96,
        "accounts_payable": 711682080.85,
        "interest_bearing_debt": 1855029982.19,
        "other_non_current_liabilities": 2644290134.80,
        "share_capital": 407937529.00,
        "retained_earnings": 21600620590.50,
        "minority_equity": 2860962359.20,
    }

    def drivers(self) -> ForecastDrivers:
        return ForecastDrivers(
            revenue_base=12083140489.05,
            base_year=2025,
            years=5,
            revenue_growth=(0.03, 0.05, 0.05, 0.04, 0.03),
            gross_margin=0.83,
            opex_pct_revenue=0.75,
            da_pct_revenue=0.0633,
            capex_pct_revenue=0.0633,
            ar_pct_revenue=0.1680739523,
            inventory_pct_revenue=0.4827775795,
            ap_pct_revenue=0.0588987674,
            interest_rate_on_debt=0.0233,
            tax_rate=0.25,
            dividend_payout_ratio=0.415,
        )

    def test_百亿级浮点残差算平(self):
        opening = BalanceSheetState(**self.OPENING)
        self.assertLess(abs(opening.balance_gap), BALANCE_TOLERANCE)
        self.assertGreater(abs(opening.balance_gap), 0.0)  # 残差确实存在，不是零
        build_forecast(opening, self.drivers())  # 不该抛异常

    def test_真错到一分钱仍然拦得住(self):
        tweaked = dict(self.OPENING)
        tweaked["other_non_current_assets"] += 0.01
        opening = BalanceSheetState(**tweaked)
        with self.assertRaises(ValueError):
            build_forecast(opening, self.drivers())


if __name__ == "__main__":
    unittest.main()
