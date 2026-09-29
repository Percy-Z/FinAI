"""敏感性分析模块的单元测试。"""

import csv
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from algorithm.finai.dcf import enterprise_value, value_dcf
from algorithm.finai.sensitivity import (
    Variable,
    apply_variable,
    attribute,
    tornado,
    two_way_grid,
    write_grid_csv,
    write_tornado_csv,
)
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


class TestVariable(unittest.TestCase):
    def test_absolute_constructor(self):
        var = Variable.absolute("永续增长率", "terminal_growth", base=0.02, delta=0.01)
        assert_close(self, var.low, 0.01)
        assert_close(self, var.high, 0.03)

    def test_relative_constructor(self):
        var = Variable.relative("收入增速", "revenue_growth", base=0.10, percent=0.5)
        assert_close(self, var.low, 0.05)
        assert_close(self, var.high, 0.15)

    def test_bounds_constructor_defaults_base_to_midpoint(self):
        var = Variable.bounds("WACC", "wacc", low=0.08, high=0.12)
        assert_close(self, var.base, 0.10)

    def test_base_outside_bounds_rejected(self):
        with self.assertRaises(ValueError):
            Variable(name="x", field="wacc", low=0.05, base=0.20, high=0.10)


class TestApplyVariable(unittest.TestCase):
    def test_wacc_is_returned_separately(self):
        modified, new_wacc = apply_variable(
            flat_assumptions(), 0.10, Variable.bounds("W", "wacc", 0.08, 0.12), 0.12
        )
        assert_close(self, new_wacc, 0.12)
        assert_close(self, modified.terminal_growth, 0.0)

    def test_vector_field_broadcasts_to_every_year(self):
        modified, _ = apply_variable(
            flat_assumptions(),
            0.10,
            Variable.absolute("增速", "revenue_growth", base=0.0, delta=0.1),
            0.25,
        )
        self.assertEqual(modified.revenue_growth, (0.25,) * 5)
        self.assertEqual(len(modified.revenue_growth), modified.years)

    def test_scalar_field_replaces_value(self):
        modified, _ = apply_variable(
            flat_assumptions(),
            0.10,
            Variable.absolute("税率", "tax_rate", base=0.25, delta=0.05),
            0.15,
        )
        assert_close(self, modified.tax_rate, 0.15)

    def test_original_object_is_not_mutated(self):
        original = flat_assumptions()
        apply_variable(
            original,
            0.10,
            Variable.absolute("税率", "tax_rate", base=0.25, delta=0.05),
            0.15,
        )
        assert_close(self, original.tax_rate, 0.25)

    def test_unknown_field_rejected(self):
        with self.assertRaises(ValueError):
            apply_variable(
                flat_assumptions(),
                0.10,
                Variable.absolute("不存在", "no_such_field", base=1.0, delta=0.1),
                1.1,
            )

    def test_terminal_growth_on_exit_multiple_model_rejected(self):
        assumptions = flat_assumptions(
            terminal_method="exit_multiple", exit_multiple=6.0
        )
        with self.assertRaises(ValueError):
            apply_variable(
                assumptions,
                0.10,
                Variable.absolute("g", "terminal_growth", base=0.0, delta=0.01),
                0.01,
            )


class TestTornado(unittest.TestCase):
    def test_base_value_matches_direct_run(self):
        assumptions, market = flat_assumptions(), flat_market()
        rows = tornado(
            assumptions,
            market,
            0.10,
            [Variable.absolute("税率", "tax_rate", base=0.25, delta=0.05)],
        )
        expected = value_dcf(assumptions, market, 0.10).per_share
        assert_close(self, rows[0].per_share_base, expected, places=10)

    def test_swing_matches_manual_computation(self):
        assumptions, market = flat_assumptions(), flat_market()
        variable = Variable.absolute(
            "永续增长率", "terminal_growth", base=0.0, delta=0.02
        )
        rows = tornado(assumptions, market, 0.10, [variable])

        low_value = value_dcf(
            replace(assumptions, terminal_growth=-0.02), market, 0.10
        ).per_share
        high_value = value_dcf(
            replace(assumptions, terminal_growth=0.02), market, 0.10
        ).per_share

        assert_close(self, rows[0].per_share_at_low, low_value, places=8)
        assert_close(self, rows[0].per_share_at_high, high_value, places=8)
        assert_close(self, rows[0].swing, abs(high_value - low_value), places=8)

    def test_rows_sorted_by_swing_descending(self):
        rows = tornado(
            flat_assumptions(),
            flat_market(),
            0.10,
            [
                Variable.absolute("税率", "tax_rate", base=0.25, delta=0.03),
                Variable.absolute("WACC", "wacc", base=0.10, delta=0.01),
                Variable.absolute("永续增长率", "terminal_growth", base=0.0, delta=0.005),
                Variable.absolute("资本开支", "capex_pct_revenue", base=0.05, delta=0.01),
            ],
        )
        swings = [row.swing for row in rows]
        self.assertEqual(swings, sorted(swings, reverse=True))
        # 在给定的变动幅度下，折现率的影响力最大
        self.assertEqual(rows[0].field, "wacc")

    def test_ranking_depends_on_the_span_you_choose(self):
        """同一个模型，把某个参数的上下界放宽，它的排名就会上升。

        所以龙卷风图的排序不只是模型的属性，也是你们选择变动范围的结果，
        给每个参数定上下界时要能说出理由。
        """
        capex = Variable.absolute(
            "资本开支", "capex_pct_revenue", base=0.05, delta=0.01
        )

        narrow = tornado(
            flat_assumptions(),
            flat_market(),
            0.10,
            [
                Variable.absolute(
                    "永续增长率", "terminal_growth", base=0.0, delta=0.002
                ),
                capex,
            ],
        )
        wide = tornado(
            flat_assumptions(),
            flat_market(),
            0.10,
            [
                Variable.absolute(
                    "永续增长率", "terminal_growth", base=0.0, delta=0.02
                ),
                capex,
            ],
        )

        self.assertEqual(narrow[0].field, "capex_pct_revenue")
        self.assertEqual(wide[0].field, "terminal_growth")

    def test_wacc_high_gives_lower_value(self):
        rows = tornado(
            flat_assumptions(),
            flat_market(),
            0.10,
            [Variable.absolute("WACC", "wacc", base=0.10, delta=0.02)],
        )
        self.assertLess(rows[0].per_share_at_high, rows[0].per_share_at_low)


class TestAttribution(unittest.TestCase):
    def test_shares_sum_to_one(self):
        rows = tornado(
            flat_assumptions(),
            flat_market(),
            0.10,
            [
                Variable.absolute("WACC", "wacc", base=0.10, delta=0.01),
                Variable.absolute("永续增长率", "terminal_growth", base=0.0, delta=0.005),
                Variable.absolute("税率", "tax_rate", base=0.25, delta=0.03),
            ],
        )
        shares = [row.share for row in attribute(rows)]
        assert_close(self, sum(shares), 1.0, places=10)

    def test_share_proportional_to_swing(self):
        rows = tornado(
            flat_assumptions(),
            flat_market(),
            0.10,
            [
                Variable.absolute("WACC", "wacc", base=0.10, delta=0.02),
                Variable.absolute("税率", "tax_rate", base=0.25, delta=0.01),
            ],
        )
        attributed = attribute(rows)
        total = sum(row.swing for row in rows)
        for row, attr in zip(rows, attributed):
            assert_close(self, attr.share, row.swing / total, places=12)


class TestTwoWayGrid(unittest.TestCase):
    def test_grid_shape(self):
        grid = two_way_grid(
            flat_assumptions(),
            flat_market(),
            0.10,
            Variable.absolute("永续增长率", "terminal_growth", base=0.0, delta=0.02),
            Variable.absolute("WACC", "wacc", base=0.10, delta=0.02),
            points=5,
        )
        self.assertEqual(len(grid.cells), 5)
        self.assertTrue(all(len(row) == 5 for row in grid.cells))
        self.assertEqual(len(grid.row_values), 5)
        self.assertEqual(len(grid.col_values), 5)

    def test_grid_endpoints_match_variable_bounds(self):
        grid = two_way_grid(
            flat_assumptions(),
            flat_market(),
            0.10,
            Variable.absolute("永续增长率", "terminal_growth", base=0.0, delta=0.02),
            Variable.absolute("WACC", "wacc", base=0.10, delta=0.02),
            points=3,
        )
        assert_close(self, grid.row_values[0], -0.02, places=10)
        assert_close(self, grid.row_values[-1], 0.02, places=10)
        assert_close(self, grid.col_values[0], 0.08, places=10)
        assert_close(self, grid.col_values[-1], 0.12, places=10)

    def test_value_decreases_across_wacc_columns(self):
        grid = two_way_grid(
            flat_assumptions(),
            flat_market(),
            0.10,
            Variable.absolute("永续增长率", "terminal_growth", base=0.0, delta=0.01),
            Variable.absolute("WACC", "wacc", base=0.10, delta=0.02),
            points=5,
        )
        for line in grid.cells:
            for left, right in zip(line, line[1:]):
                self.assertGreater(left, right)

    def test_value_increases_down_growth_rows(self):
        grid = two_way_grid(
            flat_assumptions(),
            flat_market(),
            0.10,
            Variable.absolute("永续增长率", "terminal_growth", base=0.0, delta=0.02),
            Variable.absolute("WACC", "wacc", base=0.10, delta=0.02),
            points=5,
        )
        for column in range(len(grid.col_values)):
            values = [grid.cells[i][column] for i in range(len(grid.row_values))]
            for above, below in zip(values, values[1:]):
                self.assertLess(above, below)

    def test_base_cell_corresponds_to_base_case(self):
        assumptions, market = flat_assumptions(), flat_market()
        grid = two_way_grid(
            assumptions,
            market,
            0.10,
            Variable.bounds("永续增长率", "terminal_growth", -0.02, 0.02, base=0.0),
            Variable.bounds("WACC", "wacc", 0.08, 0.12, base=0.10),
            points=5,
        )
        expected = value_dcf(assumptions, market, 0.10).per_share
        assert_close(self, grid.cell(0.0, 0.10), expected, places=8)

    def test_same_field_on_both_axes_rejected(self):
        with self.assertRaises(ValueError):
            two_way_grid(
                flat_assumptions(),
                flat_market(),
                0.10,
                Variable.absolute("WACC", "wacc", base=0.10, delta=0.01),
                Variable.absolute("WACC", "wacc", base=0.10, delta=0.02),
            )

    def test_value_range_reports_extremes(self):
        grid = two_way_grid(
            flat_assumptions(),
            flat_market(),
            0.10,
            Variable.absolute("永续增长率", "terminal_growth", base=0.0, delta=0.01),
            Variable.absolute("WACC", "wacc", base=0.10, delta=0.01),
            points=3,
        )
        low, high = grid.value_range
        self.assertLess(low, high)
        self.assertLess(low, grid.price)


class TestCsvOutput(unittest.TestCase):
    def test_write_tornado_csv(self):
        rows = tornado(
            flat_assumptions(),
            flat_market(),
            0.10,
            [Variable.absolute("WACC", "wacc", base=0.10, delta=0.01)],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = write_tornado_csv(Path(tmp) / "nested" / "tornado.csv", rows)
            self.assertTrue(path.exists())
            with path.open(encoding="utf-8-sig") as handle:
                table = list(csv.reader(handle))
        self.assertEqual(table[0][0], "变量")
        self.assertEqual(len(table), 2)
        self.assertEqual(table[1][0], "WACC")

    def test_write_grid_csv(self):
        grid = two_way_grid(
            flat_assumptions(),
            flat_market(),
            0.10,
            Variable.absolute("永续增长率", "terminal_growth", base=0.0, delta=0.01),
            Variable.absolute("WACC", "wacc", base=0.10, delta=0.01),
            points=3,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = write_grid_csv(Path(tmp) / "sensitivity.csv", grid)
            with path.open(encoding="utf-8-sig") as handle:
                table = list(csv.reader(handle))
        self.assertEqual(len(table), 4)  # 表头 + 3 行
        self.assertEqual(len(table[0]), 4)  # 行标签 + 3 列
        self.assertIn("永续增长率", table[0][0])
        self.assertIn("WACC", table[0][0])


class TestEngineReuse(unittest.TestCase):
    def test_grid_cells_match_enterprise_value_wrapper(self):
        """网格里的每一格都应该能用轻量入口独立算出来。"""
        assumptions, market = flat_assumptions(), flat_market()
        row_var = Variable.bounds("g", "terminal_growth", -0.02, 0.02, base=0.0)
        grid = two_way_grid(
            assumptions,
            market,
            0.10,
            row_var,
            Variable.bounds("WACC", "wacc", 0.08, 0.12, base=0.10),
            points=3,
        )
        for i, row_value in enumerate(grid.row_values):
            for j, col_value in enumerate(grid.col_values):
                modified, _ = apply_variable(assumptions, 0.10, row_var, row_value)
                ev = enterprise_value(modified, col_value)
                expected = (ev - market.net_debt) / market.diluted_shares
                assert_close(self, grid.cells[i][j], expected, places=8)


if __name__ == "__main__":
    unittest.main()
