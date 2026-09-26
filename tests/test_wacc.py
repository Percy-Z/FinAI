"""资本成本模块的单元测试。用能手算的数字做断言，保证公式实现正确。"""

import unittest

from finai.types import WaccInputs
from finai.wacc import (
    after_tax_cost_of_debt,
    check_wacc_sanity,
    compute_wacc,
    cost_of_equity,
    wacc_from_market,
    weights_from_market,
)


class TestCapm(unittest.TestCase):
    def test_cost_of_equity_matches_hand_calculation(self):
        # Re = 2.35% + 0.85 x 5.5% = 7.025%
        re = cost_of_equity(0.0235, 0.85, 0.055)
        self.assertAlmostEqual(re, 0.07025, places=10)

    def test_after_tax_cost_of_debt(self):
        # 3.2% x (1 - 25%) = 2.4%
        self.assertAlmostEqual(after_tax_cost_of_debt(0.032, 0.25), 0.024, places=10)


class TestWeights(unittest.TestCase):
    def test_weights_from_market(self):
        equity_weight, debt_weight = weights_from_market(1000.0, 100.0)
        self.assertAlmostEqual(equity_weight, 1000.0 / 1100.0, places=10)
        self.assertAlmostEqual(debt_weight, 100.0 / 1100.0, places=10)
        self.assertAlmostEqual(equity_weight + debt_weight, 1.0, places=10)

    def test_net_cash_company_has_zero_debt_weight(self):
        equity_weight, debt_weight = weights_from_market(1000.0, 0.0)
        self.assertEqual(debt_weight, 0.0)
        self.assertEqual(equity_weight, 1.0)

    def test_negative_value_rejected(self):
        with self.assertRaises(ValueError):
            weights_from_market(-1.0, 100.0)


class TestWacc(unittest.TestCase):
    def test_wacc_matches_hand_calculation(self):
        # E=1000、D=100 -> 权重 0.909091 / 0.090909
        # WACC = 0.909091 x 7.025% + 0.090909 x 2.4% = 6.6045%
        wacc = wacc_from_market(
            risk_free=0.0235,
            beta=0.85,
            equity_risk_premium=0.055,
            cost_of_debt=0.032,
            tax_rate=0.25,
            market_cap=1000.0,
            debt_value=100.0,
        )
        expected = (1000 / 1100) * 0.07025 + (100 / 1100) * 0.024
        self.assertAlmostEqual(wacc, expected, places=10)
        self.assertAlmostEqual(wacc, 0.066045, places=6)

    def test_sanity_check_flags_bad_weights(self):
        inputs = WaccInputs(
            risk_free=0.0235,
            beta=0.85,
            equity_risk_premium=0.055,
            cost_of_debt=0.032,
            tax_rate=0.25,
            equity_weight=0.8,
            debt_weight=0.5,
        )
        warnings = check_wacc_sanity(inputs)
        self.assertTrue(any("权重之和" in w for w in warnings))

    def test_sanity_check_flags_beta_out_of_range(self):
        inputs = WaccInputs(
            risk_free=0.0235,
            beta=4.0,
            equity_risk_premium=0.055,
            cost_of_debt=0.032,
            tax_rate=0.25,
            equity_weight=0.9,
            debt_weight=0.1,
        )
        warnings = check_wacc_sanity(inputs)
        self.assertTrue(any("beta" in w for w in warnings))

    def test_clean_inputs_produce_no_warnings(self):
        inputs = WaccInputs(
            risk_free=0.0235,
            beta=0.85,
            equity_risk_premium=0.055,
            cost_of_debt=0.032,
            tax_rate=0.25,
            equity_weight=0.909091,
            debt_weight=0.090909,
        )
        self.assertEqual(check_wacc_sanity(inputs), [])

    def test_compute_wacc_uses_inputs(self):
        inputs = WaccInputs(
            risk_free=0.0,
            beta=1.0,
            equity_risk_premium=0.10,
            cost_of_debt=0.10,
            tax_rate=0.0,
            equity_weight=0.5,
            debt_weight=0.5,
        )
        # 股权成本 10%、债务成本 10%，各占一半 -> 10%
        self.assertAlmostEqual(compute_wacc(inputs), 0.10, places=10)


if __name__ == "__main__":
    unittest.main()
