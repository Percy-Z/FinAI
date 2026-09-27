"""图表层的单元测试。

重点验三件事：编号规则、按需出图、每张图都能渲染出非空的 PNG。
"""

import tempfile
import unittest
from pathlib import Path

from algorithm.finai.comps import (
    CompanyMetrics,
    FootballField,
    FootballFieldBand,
    comps_valuation,
)
from algorithm.finai.sensitivity import Variable, tornado, two_way_grid
from algorithm.finai.types import ForecastAssumptions, MarketData
from algorithm.output import charts
from algorithm.output.charts.base import spread_end_labels
from algorithm.tests.test_reporting import sample_outputs

HAS_MATPLOTLIB = charts.HAS_MATPLOTLIB


class TestChartNumbering(unittest.TestCase):
    def test_numbers_are_11_to_16(self):
        numbers = sorted(number for number, _ in charts.CHART_FILES.values())
        self.assertEqual(numbers, [f"{index:02d}" for index in range(11, 17)])

    def test_filenames_carry_number_and_name(self):
        self.assertEqual(charts.chart_filename("football_field"), "11_football_field.png")
        self.assertEqual(charts.chart_filename("validation_summary"), "16_validation_summary.png")

    def test_unknown_chart_raises(self):
        with self.assertRaises(KeyError):
            charts.chart_filename("no_such_chart")

    def test_every_chart_maps_to_one_python_module(self):
        """一个文件对应一张图，这层组织方式要能一眼看出来。"""
        modules = {
            path.name
            for path in (Path(charts.__file__).parent).glob("*.py")
            if path.name not in {"__init__.py", "base.py"}
        }
        self.assertEqual(len(modules), len(charts.CHART_FILES))


class TestSpreadEndLabels(unittest.TestCase):
    def test_far_apart_values_are_untouched(self):
        values = [100.0, 200.0, 300.0]
        self.assertEqual(spread_end_labels(values, span=300.0), values)

    def test_close_values_are_pushed_apart(self):
        values = [84.0, 77.0]
        adjusted = spread_end_labels(values, span=600.0, gap_ratio=0.05)
        self.assertGreater(abs(adjusted[0] - adjusted[1]), 600.0 * 0.05 - 1e-9)
        # 原来 84 > 77，推开后仍要保持这个相对顺序；整体重心不变
        self.assertGreater(adjusted[0], adjusted[1])
        self.assertAlmostEqual(sum(adjusted), sum(values), places=9)

    def test_empty_input(self):
        self.assertEqual(spread_end_labels([], span=1.0), [])

    def test_single_value(self):
        self.assertEqual(spread_end_labels([5.0], span=10.0), [5.0])


@unittest.skipUnless(HAS_MATPLOTLIB, "没有安装 matplotlib")
class TestRenderAll(unittest.TestCase):
    def test_full_outputs_produce_six_charts(self):
        with tempfile.TemporaryDirectory() as tmp:
            produced = charts.render_all(sample_outputs(), Path(tmp))
            self.assertEqual(set(produced), set(charts.CHART_FILES))
            for name, path in produced.items():
                self.assertTrue(path.exists(), name)
                self.assertGreater(path.stat().st_size, 5000, name)
                self.assertEqual(path.parent.name, Path(tmp).name)

    def test_partial_outputs_produce_only_matching_charts(self):
        outputs = sample_outputs()
        outputs.dcf = None
        outputs.comps = None
        with tempfile.TemporaryDirectory() as tmp:
            produced = charts.render_all(outputs, Path(tmp))
            self.assertNotIn("peer_multiples", produced)
            self.assertIn("tornado", produced)
            self.assertIn("football_field", produced)

    def test_empty_outputs_produce_nothing(self):
        from algorithm.output.reporting import RunOutputs

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(charts.render_all(RunOutputs(), Path(tmp)), {})

    def test_directory_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "charts" / "nested"
            charts.render_all(sample_outputs(), target)
            self.assertTrue(target.is_dir())


@unittest.skipUnless(HAS_MATPLOTLIB, "没有安装 matplotlib")
class TestIndividualCharts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_football_field_handles_zero_width_band(self):
        field = FootballField(
            bands=[
                FootballFieldBand("A", 5.0, 5.0, 5.0),
                FootballFieldBand("B", 6.0, 7.0, 8.0),
            ],
            price=6.5,
        )
        path = charts.render_football_field(field, self.root / "f.png")
        self.assertGreater(path.stat().st_size, 2000)

    def test_tornado_with_single_row(self):
        assumptions = ForecastAssumptions(
            revenue_base=100.0,
            revenue_growth=(0.0,) * 5,
            ebit_margin=(0.20,) * 5,
            tax_rate=0.25,
            da_pct_revenue=0.05,
            capex_pct_revenue=0.05,
            nwc_pct_revenue=0.10,
            terminal_growth=0.0,
        )
        market = MarketData(
            price=10.0, diluted_shares=10.0, interest_bearing_debt=40.0, cash=10.0
        )
        rows = tornado(
            assumptions, market, 0.10,
            [Variable.absolute("WACC", "wacc", base=0.10, delta=0.01)],
        )
        path = charts.render_tornado(rows, price=10.0, path=self.root / "t.png")
        self.assertGreater(path.stat().st_size, 2000)

    def test_peer_multiples_with_one_usable_multiple(self):
        target = CompanyMetrics(
            "T", "目标", 10.0, 100.0, net_profit_attributable=62.0
        )
        peers = [
            CompanyMetrics("A", "甲", 21.0, 10.0, net_profit_attributable=15.0),
            CompanyMetrics("B", "乙", 15.0, 20.0, net_profit_attributable=20.0),
        ]
        result = comps_valuation(target, peers, multiples=("pe",))
        path = charts.render_peer_multiples(result, self.root / "p.png")
        self.assertGreater(path.stat().st_size, 2000)

    def test_peer_multiples_without_data_raises(self):
        target = CompanyMetrics("T", "目标", 10.0, 100.0, net_profit_attributable=-5.0)
        peers = [
            CompanyMetrics("A", "甲", 21.0, 10.0, net_profit_attributable=-15.0),
            CompanyMetrics("B", "乙", 15.0, 20.0, net_profit_attributable=-20.0),
        ]
        result = comps_valuation(target, peers, multiples=("pe",))
        with self.assertRaises(ValueError):
            charts.render_peer_multiples(result, self.root / "p.png")

    def test_validation_summary_with_single_year(self):
        outputs = sample_outputs()
        path = charts.render_validation_summary(
            list(outputs.validation)[:1], self.root / "v.png"
        )
        self.assertGreater(path.stat().st_size, 2000)

    def test_sensitivity_heatmap_when_price_outside_range(self):
        assumptions = ForecastAssumptions(
            revenue_base=100.0,
            revenue_growth=(0.0,) * 5,
            ebit_margin=(0.20,) * 5,
            tax_rate=0.25,
            da_pct_revenue=0.05,
            capex_pct_revenue=0.05,
            nwc_pct_revenue=0.10,
            terminal_growth=0.0,
        )
        market = MarketData(
            price=999.0, diluted_shares=10.0, interest_bearing_debt=40.0, cash=10.0
        )
        grid = two_way_grid(
            assumptions, market, 0.10,
            Variable.bounds("g", "terminal_growth", -0.01, 0.01, base=0.0),
            Variable.bounds("WACC", "wacc", 0.09, 0.11, base=0.10),
            points=3,
        )
        path = charts.render_sensitivity_heatmap(grid, self.root / "h.png")
        self.assertGreater(path.stat().st_size, 2000)


if __name__ == "__main__":
    unittest.main()
