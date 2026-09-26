"""估值引擎的单元测试。

核心测试用手算就能验证的常数现金流例子：
    收入 100，EBIT 利润率 20%，税率 25%，折旧 5，资本开支 5，营运资本占收入 10%
    -> FCFF = 15（每年相同，因为收入不变则营运资本增加额为 0）
    WACC = 10%、永续增长 g = 0 时：
        PV(显式期 5 年) = 15 x 3.790787  = 56.8618
        终值            = 15 / 0.10     = 150
        PV(终值)        = 150 x 0.620921 = 93.1382
        企业价值        = 150.00   （定律：g=0 且现金流恒定时 EV = FCFF / WACC）
"""

import unittest
from dataclasses import replace

from finai.dcf import (
    discount_factors,
    enterprise_value,
    project,
    terminal_value_exit_multiple,
    terminal_value_perpetuity,
    value_dcf,
)
from finai.types import ForecastAssumptions, MarketData


def flat_assumptions(**overrides) -> ForecastAssumptions:
    """常数现金流基准情景：FCFF 每年都是 15。"""
    params = dict(
        revenue_base=100.0,
        revenue_growth=(0.0, 0.0, 0.0, 0.0, 0.0),
        ebit_margin=(0.20, 0.20, 0.20, 0.20, 0.20),
        tax_rate=0.25,
        da_pct_revenue=0.05,
        capex_pct_revenue=0.05,
        nwc_pct_revenue=0.10,
        terminal_method="perpetuity",
        terminal_growth=0.0,
    )
    params.update(overrides)
    return ForecastAssumptions(**params)


def flat_market(**overrides) -> MarketData:
    params = dict(
        price=10.0,
        diluted_shares=10.0,
        interest_bearing_debt=40.0,
        cash=10.0,
        as_of="2026-09-30",
    )
    params.update(overrides)
    return MarketData(**params)


class TestProjection(unittest.TestCase):
    def test_fcff_is_constant_when_revenue_is_flat(self):
        p = project(flat_assumptions())
        for fcff in p.fcff:
            self.assertAlmostEqual(fcff, 15.0, places=10)

    def test_delta_nwc_is_zero_when_revenue_is_flat(self):
        """收入不变时营运资本不变，因此营运资本增加额为 0。"""
        p = project(flat_assumptions())
        for delta in p.delta_nwc:
            self.assertAlmostEqual(delta, 0.0, places=10)

    def test_delta_nwc_consumes_cash_when_revenue_grows(self):
        """收入增长时营运资本占用现金，增加额为正。"""
        p = project(flat_assumptions(revenue_growth=(0.10,) * 5))
        # 第一期：收入 110，营运资本 11，上期 10 -> 增加 1
        self.assertAlmostEqual(p.delta_nwc[0], 1.0, places=10)
        self.assertGreater(p.delta_nwc[0], 0.0)

    def test_ebitda_includes_da(self):
        p = project(flat_assumptions())
        for ebit, da, ebitda in zip(p.ebit, p.da, p.ebitda):
            self.assertAlmostEqual(ebitda, ebit + da, places=10)

    def test_year_labels_start_from_base_year(self):
        p = project(flat_assumptions(), base_year=2025)
        self.assertEqual(p.years, [2026, 2027, 2028, 2029, 2030])


class TestDiscounting(unittest.TestCase):
    def test_end_of_year_factors(self):
        df = discount_factors(0.10, 3)
        self.assertAlmostEqual(df[0], 1 / 1.10, places=10)
        self.assertAlmostEqual(df[1], 1 / 1.10**2, places=10)
        self.assertAlmostEqual(df[2], 1 / 1.10**3, places=10)

    def test_mid_year_factors_are_larger(self):
        end_year = discount_factors(0.10, 5)
        mid_year = discount_factors(0.10, 5, mid_year=True)
        for e, m in zip(end_year, mid_year):
            self.assertGreater(m, e)

    def test_non_positive_wacc_rejected(self):
        with self.assertRaises(ValueError):
            discount_factors(0.0, 5)


class TestTerminalValue(unittest.TestCase):
    def test_perpetuity_with_zero_growth(self):
        self.assertAlmostEqual(
            terminal_value_perpetuity(15.0, 0.10, 0.0), 150.0, places=10
        )

    def test_perpetuity_growth_above_wacc_rejected(self):
        with self.assertRaises(ValueError):
            terminal_value_perpetuity(15.0, 0.08, 0.09)

    def test_exit_multiple(self):
        # EBITDA 25 x 6.0 = 150
        self.assertAlmostEqual(terminal_value_exit_multiple(25.0, 6.0), 150.0, places=10)


class TestValueDcf(unittest.TestCase):
    """主流程：用能手算的例子验证企业价值与每股价值。"""

    def test_enterprise_value_matches_closed_form(self):
        result = value_dcf(flat_assumptions(), flat_market(), wacc=0.10)
        # g=0 且现金流恒定时，企业价值应精确等于 FCFF / WACC
        self.assertAlmostEqual(result.enterprise_value, 150.0, places=6)
        self.assertAlmostEqual(result.enterprise_value, 15.0 / 0.10, places=6)

    def test_equity_bridge_and_per_share(self):
        result = value_dcf(flat_assumptions(), flat_market(), wacc=0.10)
        # 净债务 = 40 - 10 = 30；权益价值 = 150 - 30 = 120；每股 = 120 / 10 = 12
        self.assertAlmostEqual(result.net_debt, 30.0, places=10)
        self.assertAlmostEqual(result.equity_value, 120.0, places=6)
        self.assertAlmostEqual(result.per_share, 12.0, places=6)

    def test_implied_return(self):
        result = value_dcf(flat_assumptions(), flat_market(), wacc=0.10)
        # 每股 12 / 现价 10 - 1 = 20%
        self.assertAlmostEqual(result.implied_return, 0.20, places=6)

    def test_terminal_share(self):
        result = value_dcf(flat_assumptions(), flat_market(), wacc=0.10)
        # 93.1382 / 150 = 62.09%
        self.assertAlmostEqual(result.terminal_share_of_ev, 0.620921, places=6)

    def test_pv_components_sum_to_enterprise_value(self):
        result = value_dcf(flat_assumptions(), flat_market(), wacc=0.10)
        self.assertAlmostEqual(
            sum(result.pv_fcff) + result.pv_terminal,
            result.enterprise_value,
            places=6,
        )

    def test_exit_multiple_method_can_match_perpetuity(self):
        """EBITDA 25、倍数 6 时，退出倍数法与永续增长法结果一致。"""
        perpetual = value_dcf(flat_assumptions(), flat_market(), wacc=0.10)
        multiple = value_dcf(
            flat_assumptions(terminal_method="exit_multiple", exit_multiple=6.0),
            flat_market(),
            wacc=0.10,
        )
        self.assertAlmostEqual(
            multiple.enterprise_value, perpetual.enterprise_value, places=6
        )
        self.assertAlmostEqual(multiple.implied_exit_multiple, 6.0, places=6)

    def test_higher_wacc_lowers_value(self):
        low = value_dcf(flat_assumptions(), flat_market(), wacc=0.09)
        high = value_dcf(flat_assumptions(), flat_market(), wacc=0.11)
        self.assertGreater(low.per_share, high.per_share)

    def test_mid_year_convention_raises_value(self):
        end_year = value_dcf(flat_assumptions(), flat_market(), wacc=0.10)
        mid_year = value_dcf(flat_assumptions(mid_year=True), flat_market(), wacc=0.10)
        self.assertGreater(mid_year.per_share, end_year.per_share)

    def test_net_cash_company_adds_to_equity_value(self):
        result = value_dcf(
            flat_assumptions(),
            flat_market(interest_bearing_debt=0.0, cash=10.0),
            wacc=0.10,
        )
        # 净现金 10 应加回：权益价值 = 150 + 10 = 160
        self.assertAlmostEqual(result.equity_value, 160.0, places=6)
        self.assertAlmostEqual(result.net_debt, -10.0, places=10)

    def test_minority_interest_reduces_equity_value(self):
        result = value_dcf(
            flat_assumptions(), flat_market(minority_interest=20.0), wacc=0.10
        )
        self.assertAlmostEqual(result.equity_value, 100.0, places=6)

    def test_perpetuity_growth_above_wacc_rejected(self):
        with self.assertRaises(ValueError):
            value_dcf(flat_assumptions(terminal_growth=0.09), flat_market(), wacc=0.08)


class TestDiagnostics(unittest.TestCase):
    def test_short_horizon_with_high_growth_flags_terminal_share(self):
        """终值占比过高时必须给出诊断，这是报告里要写的结构性问题。"""
        result = value_dcf(
            flat_assumptions(terminal_growth=0.07), flat_market(), wacc=0.08
        )
        self.assertGreater(result.terminal_share_of_ev, 0.80)
        self.assertTrue(any("终值占企业价值" in note for note in result.diagnostics))

    def test_normal_case_is_quiet_about_terminal_share(self):
        result = value_dcf(flat_assumptions(), flat_market(), wacc=0.10)
        self.assertFalse(any("终值占企业价值" in note for note in result.diagnostics))

    def test_negative_equity_is_flagged(self):
        result = value_dcf(
            flat_assumptions(),
            flat_market(interest_bearing_debt=400.0, cash=0.0),
            wacc=0.10,
        )
        self.assertLess(result.equity_value, 0.0)
        self.assertTrue(any("权益价值为负" in note for note in result.diagnostics))

    def test_implied_exit_multiple_is_reported(self):
        result = value_dcf(flat_assumptions(), flat_market(), wacc=0.10)
        # 终值 150 / EBITDA 25 = 6.0
        self.assertAlmostEqual(result.implied_exit_multiple, 6.0, places=6)


class TestReusability(unittest.TestCase):
    """估值函数必须能被敏感性分析和反向求解反复调用。"""

    def test_enterprise_value_wrapper_matches_full_run(self):
        full = value_dcf(flat_assumptions(), flat_market(), wacc=0.10)
        light = enterprise_value(flat_assumptions(), wacc=0.10)
        self.assertAlmostEqual(light, full.enterprise_value, places=6)

    def test_replace_builds_a_sensitivity_grid(self):
        """替换假设后重算，就是敏感性分析的基本动作。"""
        base = flat_assumptions()
        values = {}
        for wacc in (0.09, 0.10, 0.11):
            for growth in (0.0, 0.02):
                variant = replace(base, terminal_growth=growth)
                values[(wacc, growth)] = enterprise_value(variant, wacc)

        self.assertEqual(len(values), 6)
        # 同一增长率下，WACC 越高估值越低
        self.assertGreater(values[(0.09, 0.02)], values[(0.10, 0.02)])
        self.assertGreater(values[(0.10, 0.02)], values[(0.11, 0.02)])
        # 同一 WACC 下，永续增长率越高估值越高
        self.assertGreater(values[(0.10, 0.02)], values[(0.10, 0.0)])


class TestInputValidation(unittest.TestCase):
    def test_mismatched_horizon_lengths_rejected(self):
        with self.assertRaises(ValueError):
            flat_assumptions(ebit_margin=(0.2, 0.2))

    def test_unknown_terminal_method_rejected(self):
        with self.assertRaises(ValueError):
            flat_assumptions(terminal_method="magic")

    def test_exit_multiple_method_requires_multiple(self):
        with self.assertRaises(ValueError):
            flat_assumptions(terminal_method="exit_multiple", exit_multiple=None)

    def test_non_positive_shares_rejected(self):
        with self.assertRaises(ValueError):
            flat_market(diluted_shares=0.0)


if __name__ == "__main__":
    unittest.main()
