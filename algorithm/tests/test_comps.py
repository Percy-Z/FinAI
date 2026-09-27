"""相对估值模块的单元测试。

基准样本（三家公司，数字全部可手算）：

    目标公司 T：股价 10，股数 10 -> 市值 100
        归母净利润 12 -> PE = 100 / 12 = 8.3333
        归母净资产 80 -> PB = 100 / 80 = 1.25
        营业收入 100  -> PS = 100 / 100 = 1.0
        EBITDA 25，EBIT 20，净债务 30 -> 企业价值 130
        EV/EBITDA = 130 / 25 = 5.2

    可比公司 A：股价 20，股数 5 -> 市值 100；净现金 20 -> 企业价值 80
        PE = 10.0，PB = 2.0，EV/EBITDA = 80 / 20 = 4.0
    可比公司 B：股价 30，股数 4 -> 市值 120；净债务 30 -> 企业价值 150
        PE = 15.0，PB = 2.0，EV/EBITDA = 150 / 30 = 5.0
    可比公司 C：股价 15，股数 8 -> 市值 120；无债务无现金 -> 企业价值 120
        PE = 10.0，PB = 3.0，EV/EBITDA = 120 / 20 = 6.0

    PE 中位数 10，PB 中位数 2.0，EV/EBITDA 中位数 5.0
    隐含每股价值：PE 法 12.0，PB 法 16.0，EV/EBITDA 法 9.5
"""

import csv
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from algorithm.finai.comps import (
    CompanyMetrics,
    FootballFieldBand,
    build_stats,
    comps_bands,
    comps_valuation,
    compute_multiples,
    dcf_band,
    football_field,
    implied_from_multiple,
    percentile,
    write_comps_csv,
    write_implied_csv,
)
from algorithm.finai.dcf import value_dcf
from algorithm.finai.types import ForecastAssumptions, MarketData

from algorithm.tests._helpers import assert_close, assert_not_close  # noqa: E402


TARGET = CompanyMetrics(
    code="T",
    name="目标公司",
    price=10.0,
    diluted_shares=10.0,
    net_profit_attributable=12.0,
    book_equity_attributable=80.0,
    revenue=100.0,
    ebit=20.0,
    ebitda=25.0,
    interest_bearing_debt=40.0,
    cash=10.0,
    as_of="2026-09-30",
)

PEER_A = CompanyMetrics(
    code="A",
    name="可比甲",
    price=20.0,
    diluted_shares=5.0,
    net_profit_attributable=10.0,
    book_equity_attributable=50.0,
    revenue=80.0,
    ebit=15.0,
    ebitda=20.0,
    interest_bearing_debt=0.0,
    cash=20.0,
    as_of="2026-09-30",
)

PEER_B = CompanyMetrics(
    code="B",
    name="可比乙",
    price=30.0,
    diluted_shares=4.0,
    net_profit_attributable=8.0,
    book_equity_attributable=60.0,
    revenue=100.0,
    ebit=24.0,
    ebitda=30.0,
    interest_bearing_debt=30.0,
    cash=0.0,
    as_of="2026-09-30",
)

PEER_C = CompanyMetrics(
    code="C",
    name="可比丙",
    price=15.0,
    diluted_shares=8.0,
    net_profit_attributable=12.0,
    book_equity_attributable=40.0,
    revenue=120.0,
    ebit=16.0,
    ebitda=20.0,
    interest_bearing_debt=0.0,
    cash=0.0,
    as_of="2026-09-30",
)

PEERS = [PEER_A, PEER_B, PEER_C]


class TestCompanyBasics(unittest.TestCase):
    def test_market_cap_and_net_debt(self):
        assert_close(self, TARGET.market_cap, 100.0)
        assert_close(self, TARGET.net_debt, 30.0)
        assert_close(self, TARGET.enterprise_value, 130.0)

    def test_net_cash_company_has_negative_net_debt(self):
        assert_close(self, PEER_A.net_debt, -20.0)
        assert_close(self, PEER_A.enterprise_value, 80.0)

    def test_minority_and_preferred_enter_enterprise_value(self):
        company = replace(TARGET, minority_interest=10.0, preferred_stock=5.0)
        # 100 + 30 + 10 + 5 = 145
        assert_close(self, company.enterprise_value, 145.0)

    def test_non_operating_assets_reduce_enterprise_value(self):
        company = replace(TARGET, non_operating_assets=15.0)
        assert_close(self, company.enterprise_value, 115.0)

    def test_per_share_metrics(self):
        assert_close(self, TARGET.eps, 1.2)
        assert_close(self, TARGET.book_value_per_share, 8.0)

    def test_invalid_price_rejected(self):
        with self.assertRaises(ValueError):
            replace(TARGET, price=0.0)

    def test_invalid_shares_rejected(self):
        with self.assertRaises(ValueError):
            replace(TARGET, diluted_shares=-1.0)


class TestMultiples(unittest.TestCase):
    def test_target_multiples(self):
        values = compute_multiples(TARGET)
        assert_close(self, values["pe"], 100 / 12, places=6)
        assert_close(self, values["pb"], 1.25, places=6)
        assert_close(self, values["ps"], 1.0, places=6)
        assert_close(self, values["ev_ebitda"], 5.2, places=6)
        assert_close(self, values["ev_ebit"], 6.5, places=6)

    def test_peer_multiples(self):
        assert_close(self, compute_multiples(PEER_A)["pe"], 10.0, places=6)
        assert_close(self, compute_multiples(PEER_B)["pe"], 15.0, places=6)
        assert_close(self, 
            compute_multiples(PEER_C)["ev_ebitda"], 6.0, places=6
        )

    def test_negative_earnings_make_pe_meaningless(self):
        """亏损公司的 PE 不是「便宜」，而是没有意义，必须返回 None。"""
        loss_maker = replace(TARGET, net_profit_attributable=-5.0)
        self.assertIsNone(compute_multiples(loss_maker)["pe"])

    def test_zero_denominator_is_none(self):
        no_revenue = replace(TARGET, revenue=0.0)
        self.assertIsNone(compute_multiples(no_revenue)["ps"])

    def test_missing_data_is_none(self):
        incomplete = replace(TARGET, ebitda=None)
        self.assertIsNone(compute_multiples(incomplete)["ev_ebitda"])

    def test_peg_uses_growth_in_percent(self):
        growing = replace(TARGET, earnings_growth=0.10)
        # PE = 8.3333，增速 10% -> PEG = 8.3333 / 10 = 0.8333
        assert_close(self, compute_multiples(growing)["peg"], 8.333333 / 10, places=5)

    def test_peg_without_growth_is_none(self):
        self.assertIsNone(compute_multiples(TARGET)["peg"])


class TestPercentile(unittest.TestCase):
    def test_matches_excel_quartile_inc(self):
        """与 Excel 的 QUARTILE.INC 结果一致。"""
        data = [1.0, 2.0, 3.0, 4.0]
        assert_close(self, percentile(data, 0.25), 1.75)
        assert_close(self, percentile(data, 0.5), 2.5)
        assert_close(self, percentile(data, 0.75), 3.25)

    def test_odd_length(self):
        data = [1.0, 2.0, 3.0]
        assert_close(self, percentile(data, 0.25), 1.5)
        assert_close(self, percentile(data, 0.5), 2.0)
        assert_close(self, percentile(data, 0.75), 2.5)

    def test_single_value(self):
        assert_close(self, percentile([7.0], 0.25), 7.0)

    def test_unsorted_input_is_sorted(self):
        assert_close(self, percentile([4.0, 1.0, 3.0, 2.0], 0.5), 2.5)

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            percentile([], 0.5)

    def test_out_of_range_p_rejected(self):
        with self.assertRaises(ValueError):
            percentile([1.0, 2.0], 1.5)


class TestBuildStats(unittest.TestCase):
    def test_pe_stats(self):
        stats = build_stats(PEERS, names=("pe",))
        entry = stats["pe"]
        self.assertEqual(entry.count, 3)
        assert_close(self, entry.median, 10.0)
        assert_close(self, entry.q1, 10.0)
        assert_close(self, entry.q3, 12.5)
        assert_close(self, entry.mean, 35 / 3, places=6)

    def test_ev_ebitda_stats(self):
        stats = build_stats(PEERS, names=("ev_ebitda",))
        entry = stats["ev_ebitda"]
        assert_close(self, entry.minimum, 4.0)
        assert_close(self, entry.median, 5.0)
        assert_close(self, entry.maximum, 6.0)
        assert_close(self, entry.q1, 4.5)
        assert_close(self, entry.q3, 5.5)

    def test_negative_peers_are_excluded_not_counted(self):
        loss = replace(PEER_C, net_profit_attributable=-3.0)
        stats = build_stats([PEER_A, PEER_B, loss], names=("pe",))
        entry = stats["pe"]
        self.assertEqual(entry.count, 2)
        self.assertEqual(len(entry.excluded), 1)
        self.assertEqual(entry.excluded[0][0], "C")
        self.assertIn("分母非正", entry.excluded[0][1])

    def test_excluded_peer_does_not_move_median(self):
        loss = replace(PEER_C, net_profit_attributable=-3.0)
        with_loss = build_stats([PEER_A, PEER_B, loss], names=("pe",))
        without = build_stats([PEER_A, PEER_B], names=("pe",))
        assert_close(self, with_loss["pe"].median, without["pe"].median)

    def test_outlier_exclusion_is_off_by_default(self):
        peers = [
            replace(PEER_A, net_profit_attributable=10.0),  # PE 10
            replace(PEER_B, net_profit_attributable=9.0909),  # PE 13.2
            replace(PEER_C, net_profit_attributable=8.0),  # PE 15
            replace(PEER_A, code="D", net_profit_attributable=1.0),  # PE 100
        ]
        default = build_stats(peers, names=("pe",))
        self.assertEqual(default["pe"].count, 4)

    def test_outlier_exclusion_removes_extreme_value(self):
        peers = [
            CompanyMetrics("A", "甲", 10.0, 10.0, net_profit_attributable=10.0),
            CompanyMetrics("B", "乙", 12.0, 10.0, net_profit_attributable=9.0909),
            CompanyMetrics("C", "丙", 14.0, 10.0, net_profit_attributable=8.0),
            CompanyMetrics("D", "丁", 100.0, 10.0, net_profit_attributable=1.0),
        ]
        stats = build_stats(peers, names=("pe",), exclude_outliers=True)
        entry = stats["pe"]
        self.assertEqual(entry.count, 3)
        self.assertEqual(entry.excluded[0][0], "D")
        self.assertIn("四分位距", entry.excluded[0][1])

    def test_value_of_rejects_unknown_stat(self):
        stats = build_stats(PEERS, names=("pe",))
        with self.assertRaises(ValueError):
            stats["pe"].value_of("mode")


class TestImpliedValuation(unittest.TestCase):
    def test_pe_implied_price(self):
        item = implied_from_multiple(TARGET, "pe", 10.0)
        # 10 x 每股收益 1.2 = 12.0
        assert_close(self, item.implied_per_share, 12.0, places=6)
        self.assertIsNone(item.implied_enterprise_value)

    def test_pb_implied_price(self):
        item = implied_from_multiple(TARGET, "pb", 2.0)
        # 2.0 x 每股净资产 8 = 16.0
        assert_close(self, item.implied_per_share, 16.0, places=6)

    def test_ps_implied_price(self):
        item = implied_from_multiple(TARGET, "ps", 1.2)
        # 1.2 x 每股收入 10 = 12.0
        assert_close(self, item.implied_per_share, 12.0, places=6)

    def test_ev_multiple_goes_through_the_bridge(self):
        """EV/EBITDA 必须先算企业价值，再减净债务，最后除以股数。

        错误的做法是直接把倍数乘到每股 EBITDA 上，会得到 5.0 x 2.5 = 12.5。
        """
        item = implied_from_multiple(TARGET, "ev_ebitda", 5.0)
        assert_close(self, item.implied_enterprise_value, 125.0, places=6)
        assert_close(self, item.implied_equity_value, 95.0, places=6)
        assert_close(self, item.implied_per_share, 9.5, places=6)
        assert_not_close(self, item.implied_per_share, 12.5, places=3)

    def test_ev_bridge_deducts_minority_and_preferred(self):
        company = replace(TARGET, minority_interest=10.0, preferred_stock=5.0)
        item = implied_from_multiple(company, "ev_ebitda", 5.0)
        # 企业价值 125，减净债务 30、少数股东 10、优先股 5 -> 权益 80 -> 每股 8.0
        assert_close(self, item.implied_equity_value, 80.0, places=6)
        assert_close(self, item.implied_per_share, 8.0, places=6)

    def test_net_cash_increases_implied_equity(self):
        company = replace(TARGET, interest_bearing_debt=0.0, cash=30.0)
        item = implied_from_multiple(company, "ev_ebitda", 5.0)
        # 净现金 30 加回：权益价值 = 125 + 30 = 155 -> 每股 15.5
        assert_close(self, item.implied_per_share, 15.5, places=6)

    def test_negative_target_metric_is_not_applicable(self):
        company = replace(TARGET, ebitda=-10.0)
        item = implied_from_multiple(company, "ev_ebitda", 5.0)
        self.assertFalse(item.applicable)
        self.assertIn("不适用", item.note)

    def test_missing_target_metric_is_not_applicable(self):
        company = replace(TARGET, book_equity_attributable=None)
        item = implied_from_multiple(company, "pb", 2.0)
        self.assertFalse(item.applicable)
        self.assertIn("缺失", item.note)

    def test_peg_cannot_be_used_for_implied_value(self):
        with self.assertRaises(ValueError):
            implied_from_multiple(TARGET, "peg", 1.0)


class TestCompsValuation(unittest.TestCase):
    def setUp(self):
        self.result = comps_valuation(TARGET, PEERS)

    def test_peer_multiples_recorded_per_company(self):
        self.assertEqual(set(self.result.peer_multiples), {"A", "B", "C"})
        assert_close(self, self.result.peer_multiples["A"]["pe"], 10.0)

    def test_implied_values_at_median(self):
        by_multiple = {
            item.multiple: item.implied_per_share for item in self.result.medians
        }
        assert_close(self, by_multiple["pe"], 12.0, places=6)
        assert_close(self, by_multiple["pb"], 16.0, places=6)
        assert_close(self, by_multiple["ev_ebitda"], 9.5, places=6)

    def test_value_range_and_central(self):
        low, high = self.result.value_range
        assert_close(self, low, 9.5, places=6)
        assert_close(self, high, 16.0, places=6)
        assert_close(self, self.result.central_value, 12.0, places=6)

    def test_implied_return(self):
        # 中心值 12 / 现价 10 - 1 = 20%
        assert_close(self, self.result.implied_return, 0.20, places=6)

    def test_stat_names_expand_the_implied_list(self):
        # 三种倍数 x 三种口径 = 9 条
        self.assertEqual(len(self.result.implied), 9)

    def test_custom_multiples_subset(self):
        result = comps_valuation(TARGET, PEERS, multiples=("pe",))
        self.assertEqual({item.multiple for item in result.implied}, {"pe"})

    def test_empty_peers_rejected(self):
        with self.assertRaises(ValueError):
            comps_valuation(TARGET, [])

    def test_small_sample_warning(self):
        result = comps_valuation(TARGET, PEERS[:2])
        self.assertTrue(any("样本偏小" in w for w in result.warnings))

    def test_no_warning_with_three_peers(self):
        self.assertFalse(any("样本偏小" in w for w in self.result.warnings))

    def test_method_dropped_when_all_peers_lack_data(self):
        peers = [replace(peer, ebitda=None) for peer in PEERS]
        result = comps_valuation(TARGET, peers, multiples=("ev_ebitda",))
        self.assertTrue(any("没有任何可比公司可用" in w for w in result.warnings))
        self.assertEqual(result.implied, [])

    def test_partial_exclusion_is_reported(self):
        peers = [PEER_A, PEER_B, replace(PEER_C, net_profit_attributable=-1.0)]
        result = comps_valuation(TARGET, peers, multiples=("pe",))
        self.assertTrue(any("被排除" in w for w in result.warnings))

    def test_to_dict_is_json_serializable(self):
        payload = json.dumps(self.result.to_dict(), ensure_ascii=False)
        restored = json.loads(payload)
        self.assertEqual(restored["target"], "T")
        self.assertEqual(restored["peer_codes"], ["A", "B", "C"])
        assert_close(self, restored["multiples"]["pe"]["median"], 10.0)


class TestFootballField(unittest.TestCase):
    def setUp(self):
        self.result = comps_valuation(TARGET, PEERS)

    def test_bands_use_quartile_range(self):
        bands = {band.name: band for band in comps_bands(self.result)}
        assert_close(self, bands["PE"].low, 12.0, places=6)
        assert_close(self, bands["PE"].high, 15.0, places=6)
        assert_close(self, bands["PB"].low, 16.0, places=6)
        assert_close(self, bands["PB"].high, 20.0, places=6)
        assert_close(self, bands["EV/EBITDA"].low, 8.25, places=6)
        assert_close(self, bands["EV/EBITDA"].high, 10.75, places=6)

    def test_dcf_band_defaults_to_a_point(self):
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
        dcf = value_dcf(assumptions, market, 0.10)
        band = dcf_band(dcf)
        assert_close(self, band.low, 12.0, places=6)
        assert_close(self, band.high, 12.0, places=6)

    def test_dcf_band_accepts_explicit_range(self):
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
        dcf = value_dcf(assumptions, market, 0.10)
        band = dcf_band(dcf, low=9.0, high=15.0)
        assert_close(self, band.low, 9.0)
        assert_close(self, band.central, 12.0)
        assert_close(self, band.high, 15.0)
        assert_close(self, band.width, 6.0)

    def test_non_overlapping_bands_raise_warning(self):
        bands = [
            FootballFieldBand("DCF", low=20.0, central=22.0, high=24.0),
            FootballFieldBand("可比公司", low=8.0, central=10.0, high=12.0),
        ]
        field = football_field(bands, price=15.0, check_pairs=(("DCF", "可比公司"),))
        self.assertFalse(field.overlaps("DCF", "可比公司"))
        self.assertTrue(any("不重叠" in w for w in field.warnings))

    def test_overlapping_bands_produce_no_warning(self):
        bands = [
            FootballFieldBand("DCF", low=8.0, central=10.0, high=14.0),
            FootballFieldBand("可比公司", low=12.0, central=13.0, high=16.0),
        ]
        field = football_field(bands, price=15.0, check_pairs=(("DCF", "可比公司"),))
        self.assertTrue(field.overlaps("DCF", "可比公司"))
        self.assertFalse(any("不重叠" in w for w in field.warnings))

    def test_overall_range_spans_all_bands(self):
        bands = [
            FootballFieldBand("DCF", low=8.0, central=10.0, high=14.0),
            FootballFieldBand("可比公司", low=6.0, central=13.0, high=18.0),
        ]
        field = football_field(bands, price=12.0)
        assert_close(self, field.overall_low, 6.0)
        assert_close(self, field.overall_high, 18.0)

    def test_price_below_all_bands_is_flagged(self):
        bands = [FootballFieldBand("DCF", 20.0, 22.0, 24.0)]
        field = football_field(bands, price=10.0)
        self.assertTrue(any("低于全部方法" in w for w in field.warnings))

    def test_price_above_all_bands_is_flagged(self):
        bands = [FootballFieldBand("DCF", 5.0, 6.0, 7.0)]
        field = football_field(bands, price=20.0)
        self.assertTrue(any("高于全部方法" in w for w in field.warnings))

    def test_empty_bands_rejected(self):
        with self.assertRaises(ValueError):
            football_field([], price=10.0)

    def test_unknown_band_lookup_raises(self):
        field = football_field([FootballFieldBand("DCF", 1.0, 2.0, 3.0)], price=2.0)
        with self.assertRaises(KeyError):
            field.band("不存在")


class TestCsvOutput(unittest.TestCase):
    def setUp(self):
        self.result = comps_valuation(TARGET, PEERS)

    def test_write_comps_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_comps_csv(Path(tmp) / "nested" / "comps.csv", self.result)
            self.assertTrue(path.exists())
            with path.open(encoding="utf-8-sig") as handle:
                table = list(csv.reader(handle))
        self.assertEqual(table[0][0], "类型")
        self.assertEqual(table[1][0], "目标")
        self.assertEqual(table[1][1], "T")
        # 目标 + 三家可比
        peer_rows = [row for row in table if row and row[0] == "可比"]
        self.assertEqual(len(peer_rows), 3)
        # 末尾应有统计块
        self.assertTrue(any(row and row[0] == "统计口径" for row in table))

    def test_write_implied_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_implied_csv(Path(tmp) / "comps_implied.csv", self.result)
            with path.open(encoding="utf-8-sig") as handle:
                table = list(csv.reader(handle))
        self.assertEqual(table[0][0], "倍数")
        self.assertEqual(len(table), 10)  # 表头 + 9 条
        pe_median = [row for row in table if row[0] == "PE" and row[1] == "median"]
        self.assertEqual(len(pe_median), 1)
        assert_close(self, float(pe_median[0][3]), 12.0, places=2)

    def test_inapplicable_rows_have_empty_value(self):
        peers = [PEER_A, PEER_B, replace(PEER_C, net_profit_attributable=-1.0)]
        result = comps_valuation(TARGET, peers, multiples=("pe",))
        with tempfile.TemporaryDirectory() as tmp:
            path = write_implied_csv(Path(tmp) / "implied.csv", result)
            with path.open(encoding="utf-8-sig") as handle:
                table = list(csv.reader(handle))
        self.assertTrue(all(row[3] != "" for row in table[1:]))


if __name__ == "__main__":
    unittest.main()
