"""反向求解模块的单元测试。

最强的验证方式是往返测试：先用某个参数正算出一个每股价值，再把这个价值当成目标，
看能不能反解回原来的参数。
"""

import unittest
from dataclasses import replace
from typing import Any

from algorithm.finai.dcf import value_dcf
from algorithm.finai.reverse_dcf import (
    DEFAULT_BOUNDS,
    effective_bounds,
    reverse_solve,
    reverse_solve_many,
)
from algorithm.finai.sensitivity import Variable
from algorithm.finai.types import ForecastAssumptions, MarketData

from algorithm.tests._helpers import assert_close  # noqa: E402



def flat_assumptions(**overrides: Any) -> ForecastAssumptions:
    params: dict[str, Any] = dict(
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


def flat_market(**overrides: Any) -> MarketData:
    params: dict[str, Any] = dict(
        price=10.0,
        diluted_shares=10.0,
        interest_bearing_debt=40.0,
        cash=10.0,
    )
    params.update(overrides)
    return MarketData(**params)


class TestEffectiveBounds(unittest.TestCase):
    def test_growth_bounds_capped_below_wacc(self):
        variable = Variable.bounds("g", "terminal_growth", -0.02, 0.09)
        _, upper = effective_bounds(variable, wacc=0.08)
        self.assertLess(upper, 0.08)

    def test_wacc_lower_bound_is_positive(self):
        variable = Variable.bounds("wacc", "wacc", 0.0, 0.30)
        lower, _ = effective_bounds(variable, wacc=0.10)
        self.assertGreater(lower, 0.0)

    def test_explicit_bounds_override_defaults(self):
        variable = Variable.absolute("税率", "tax_rate", base=0.25, delta=0.05)
        lower, upper = effective_bounds(variable, wacc=0.10, bounds=(0.10, 0.30))
        assert_close(self, lower, 0.10)
        assert_close(self, upper, 0.30)

    def test_unknown_field_falls_back_to_base_span(self):
        variable = Variable.absolute("折旧率", "da_pct_revenue", base=0.05, delta=0.01)
        lower, upper = effective_bounds(variable, wacc=0.10)
        self.assertLess(lower, 0.05)
        self.assertGreater(upper, 0.05)

    def test_invalid_after_clipping_rejected(self):
        variable = Variable.bounds("g", "terminal_growth", -0.02, 0.09)
        with self.assertRaises(ValueError):
            effective_bounds(variable, wacc=0.08, bounds=(0.085, 0.09))

    def test_wacc_lower_bound_climbs_above_growth_floor(self):
        """永续增长法下，反解 WACC 的下界必须高于永续增长率。"""
        variable = Variable.bounds("WACC", "wacc", 0.01, 0.40, base=0.10)
        lower, _ = effective_bounds(variable, wacc=0.10, growth_floor=0.03)
        self.assertGreater(lower, 0.03)

    def test_without_growth_floor_lower_bound_stays_positive(self):
        variable = Variable.bounds("WACC", "wacc", 0.0, 0.40, base=0.10)
        lower, _ = effective_bounds(variable, wacc=0.10)
        self.assertGreater(lower, 0.0)


class TestRoundTrip(unittest.TestCase):
    """往返测试：先正算，再反解，应该回到原来的参数。"""

    def test_recover_wacc(self):
        assumptions, market = flat_assumptions(), flat_market()
        true_wacc = 0.093
        target = value_dcf(assumptions, market, true_wacc).per_share

        result = reverse_solve(
            assumptions,
            market,
            wacc=0.10,
            variable=Variable.bounds("WACC", "wacc", 0.02, 0.40, base=0.10),
            target_per_share=target,
        )
        assert_close(self, result.implied_value, true_wacc, places=6)
        self.assertLess(abs(result.residual), 1e-6)

    def test_recover_terminal_growth(self):
        assumptions, market = flat_assumptions(), flat_market()
        true_growth = 0.018
        target = value_dcf(
            replace(assumptions, terminal_growth=true_growth), market, 0.10
        ).per_share

        result = reverse_solve(
            assumptions,
            market,
            wacc=0.10,
            variable=Variable.absolute(
                "永续增长率", "terminal_growth", base=0.0, delta=0.02
            ),
            target_per_share=target,
        )
        assert_close(self, result.implied_value, true_growth, places=6)

    def test_recover_ebit_margin(self):
        assumptions, market = flat_assumptions(), flat_market()
        true_margin = 0.26
        target = value_dcf(
            replace(assumptions, ebit_margin=(true_margin,) * 5), market, 0.10
        ).per_share

        result = reverse_solve(
            assumptions,
            market,
            wacc=0.10,
            variable=Variable.absolute(
                "EBIT 利润率", "ebit_margin", base=0.20, delta=0.10
            ),
            target_per_share=target,
        )
        assert_close(self, result.implied_value, true_margin, places=6)

    def test_recover_revenue_growth(self):
        assumptions, market = flat_assumptions(), flat_market()
        true_growth = 0.12
        target = value_dcf(
            replace(assumptions, revenue_growth=(true_growth,) * 5), market, 0.10
        ).per_share

        result = reverse_solve(
            assumptions,
            market,
            wacc=0.10,
            variable=Variable.absolute(
                "收入增速", "revenue_growth", base=0.0, delta=0.15
            ),
            target_per_share=target,
        )
        assert_close(self, result.implied_value, true_growth, places=6)

    def test_default_target_is_current_price(self):
        """省略目标价时默认反解到当前股价，这就是标准的反向 DCF。"""
        result = reverse_solve(
            flat_assumptions(),
            flat_market(price=15.0),
            wacc=0.10,
            variable=Variable.absolute(
                "永续增长率", "terminal_growth", base=0.0, delta=0.03
            ),
        )
        assert_close(self, result.target_per_share, 15.0)
        assert_close(self, result.achieved_per_share, 15.0, places=6)

    def test_solving_wacc_does_not_break_on_high_terminal_growth(self):
        """回归测试：永续增长率 3% 时反解 WACC，不能被「终值不收敛」打断。

        默认搜索下界是 1%，低于永续增长率，如果不做裁剪就会直接抛错。
        """
        assumptions = flat_assumptions(terminal_growth=0.03)
        result = reverse_solve(
            assumptions,
            flat_market(price=12.0),
            wacc=0.10,
            variable=Variable.bounds("WACC", "wacc", 0.01, 0.40, base=0.10),
        )
        self.assertGreater(result.implied_value, assumptions.terminal_growth)
        assert_close(self, result.achieved_per_share, 12.0, places=6)


class TestInterpretation(unittest.TestCase):
    def test_higher_price_implies_higher_growth_expectation(self):
        """股价越高，市场隐含的增长率越高。"""
        variable = Variable.absolute(
            "永续增长率", "terminal_growth", base=0.0, delta=0.03
        )
        cheap = reverse_solve(
            flat_assumptions(), flat_market(price=9.0), 0.10, variable
        )
        rich = reverse_solve(
            flat_assumptions(), flat_market(price=15.0), 0.10, variable
        )
        self.assertLess(cheap.implied_value, rich.implied_value)
        self.assertEqual(rich.direction, "higher")

    def test_direction_reports_lower_when_implied_below_base(self):
        variable = Variable.bounds(
            "永续增长率", "terminal_growth", -0.02, 0.04, base=0.02
        )
        result = reverse_solve(
            flat_assumptions(), flat_market(price=9.0), 0.10, variable
        )
        self.assertEqual(result.direction, "lower")
        self.assertLess(result.implied_value, 0.02)

    def test_describe_mentions_name_and_gap(self):
        variable = Variable.bounds("永续增长率", "terminal_growth", -0.02, 0.04, base=0.0)
        result = reverse_solve(
            flat_assumptions(), flat_market(price=14.0), 0.10, variable
        )
        text = result.describe()
        self.assertIn("永续增长率", text)
        self.assertIn("市场隐含", text)

    def test_implied_wacc_is_higher_when_price_is_lower(self):
        """股价越低，市场要求的回报率（隐含折现率）越高。"""
        variable = Variable.bounds("WACC", "wacc", 0.02, 0.40, base=0.10)
        cheap = reverse_solve(
            flat_assumptions(), flat_market(price=9.0), 0.10, variable
        )
        rich = reverse_solve(
            flat_assumptions(), flat_market(price=15.0), 0.10, variable
        )
        self.assertGreater(cheap.implied_value, rich.implied_value)


class TestFailureModes(unittest.TestCase):
    def test_unreachable_target_raises_with_helpful_message(self):
        variable = Variable.bounds(
            "永续增长率", "terminal_growth", -0.02, 0.02, base=0.0
        )
        with self.assertRaises(ValueError) as ctx:
            reverse_solve(
                flat_assumptions(), flat_market(price=1000.0), 0.10, variable
            )
        self.assertIn("无法达到目标价", str(ctx.exception))

    def test_converges_in_reasonable_iterations(self):
        result = reverse_solve(
            flat_assumptions(),
            flat_market(price=13.0),
            0.10,
            Variable.bounds("永续增长率", "terminal_growth", -0.02, 0.05, base=0.0),
        )
        self.assertLess(result.iterations, 200)
        self.assertLess(abs(result.residual), 1e-6)

    def test_custom_tolerance_is_respected(self):
        result = reverse_solve(
            flat_assumptions(),
            flat_market(price=11.0),
            0.10,
            Variable.bounds("永续增长率", "terminal_growth", -0.02, 0.05, base=0.0),
            tolerance=1e-4,
        )
        self.assertLess(abs(result.residual), 1e-3)


class TestBatchSolve(unittest.TestCase):
    def test_solve_multiple_variables(self):
        variables = [
            Variable.bounds("永续增长率", "terminal_growth", -0.02, 0.05, base=0.0),
            Variable.bounds("WACC", "wacc", 0.02, 0.40, base=0.10),
            Variable.absolute("EBIT 利润率", "ebit_margin", base=0.20, delta=0.15),
        ]
        results = reverse_solve_many(
            flat_assumptions(), flat_market(price=14.0), 0.10, variables
        )
        self.assertEqual(len(results), 3)
        for result in results:
            assert_close(self, result.achieved_per_share, 14.0, places=6)

    def test_each_solve_keeps_other_assumptions_at_base(self):
        """逐个反解，互不影响，结果顺序与传入顺序一致。"""
        variables = [
            Variable.bounds("永续增长率", "terminal_growth", -0.02, 0.05, base=0.0),
            Variable.bounds("WACC", "wacc", 0.02, 0.40, base=0.10),
        ]
        results = reverse_solve_many(
            flat_assumptions(), flat_market(price=14.0), 0.10, variables
        )
        self.assertEqual([r.field for r in results], ["terminal_growth", "wacc"])


class TestDefaultsTable(unittest.TestCase):
    def test_default_bounds_cover_common_fields(self):
        for field in ("wacc", "terminal_growth", "revenue_growth", "ebit_margin"):
            self.assertIn(field, DEFAULT_BOUNDS)
            low, high = DEFAULT_BOUNDS[field]
            self.assertLess(low, high)


if __name__ == "__main__":
    unittest.main()
