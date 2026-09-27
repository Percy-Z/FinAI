"""勾稽校验模块的单元测试。

基准样本是一份完全自洽的年报数据，可以手算验证每一条恒等式：
    资产 1000 = 负债 400 + 权益 600
    权益 600 = 归母 580 + 少数股东 20
    未分配利润 130 = 期初 100 + 归母净利 55 - 提取盈余公积 5 - 分红 20
    期末现金 150 = 期初 120 + 经营 70 - 投资 30 - 筹资 10
    现金流量表期末现金 150 = 资产负债表货币资金 150
    收入增速 11.1%，应收增速 23.1%，相差 12.0 个百分点（未触发阈值）
    经营现金流 / 净利润 = 70 / 60 = 1.17
    非经常性损益占归母净利润 10 / 55 = 18.2%
    收入 / 总资产 = 500 / 1000 = 0.5
"""

import json
import unittest
from dataclasses import replace

from algorithm.finai.statements import (
    BalanceSheet,
    CashFlowStatement,
    IncomeStatement,
    ReportSnapshot,
)
from algorithm.finai.validate import (
    Tolerance,
    check_assets_floor,
    check_balance_sheet_tie,
    check_cash_definition,
    check_cash_rollforward,
    check_equity_composition,
    check_non_recurring_share,
    check_profit_cash_conversion,
    check_restatement,
    check_retained_earnings_rollforward,
    check_revenue_receivable_divergence,
    check_scale_sanity,
    run_checks,
)

from algorithm.tests._helpers import assert_close  # noqa: E402

BASE_BALANCE = BalanceSheet(
    total_assets=1000.0,
    total_liabilities=400.0,
    total_equity=600.0,
    equity_attributable=580.0,
    minority_equity=20.0,
    retained_earnings=130.0,
    cash_and_equivalents=150.0,
    accounts_receivable=80.0,
    inventory=120.0,
)

BASE_BALANCE_BEGIN = BalanceSheet(
    total_assets=900.0,
    total_liabilities=380.0,
    total_equity=520.0,
    retained_earnings=100.0,
    cash_and_equivalents=120.0,
    accounts_receivable=65.0,
)

BASE_INCOME = IncomeStatement(
    revenue=500.0,
    net_profit=60.0,
    net_profit_attributable=55.0,
    non_recurring_gains=10.0,
    dividends_declared=20.0,
    reserve_appropriation=5.0,
)

BASE_INCOME_PRIOR = IncomeStatement(revenue=450.0, net_profit=50.0)

BASE_CASHFLOW = CashFlowStatement(
    operating=70.0,
    investing=-30.0,
    financing=-10.0,
    cash_begin=120.0,
    cash_end=150.0,
)

BASE_SNAPSHOT = ReportSnapshot(
    fiscal_year=2025,
    balance=BASE_BALANCE,
    income=BASE_INCOME,
    cashflow=BASE_CASHFLOW,
    balance_begin=BASE_BALANCE_BEGIN,
    income_prior=BASE_INCOME_PRIOR,
    source="2025_annual.pdf",
)


def clean_snapshot(**overrides) -> ReportSnapshot:
    return replace(BASE_SNAPSHOT, **overrides)


def balance(**overrides) -> BalanceSheet:
    return replace(BASE_BALANCE, **overrides)


def income(**overrides) -> IncomeStatement:
    return replace(BASE_INCOME, **overrides)


def cashflow(**overrides) -> CashFlowStatement:
    return replace(BASE_CASHFLOW, **overrides)


class TestCleanFixture(unittest.TestCase):
    """基准样本应当十项全过，这是所有其它测试的前提。"""

    def test_every_check_passes(self):
        report = run_checks(BASE_SNAPSHOT)
        self.assertEqual(report.summary["fail"], 0)
        self.assertEqual(report.summary["warning"], 0)
        self.assertEqual(report.summary["skipped"], 0)
        self.assertEqual(report.summary["pass"], 11)
        self.assertEqual(report.summary["total"], 11)

    def test_report_carries_source_and_year(self):
        report = run_checks(BASE_SNAPSHOT)
        self.assertEqual(report.fiscal_year, 2025)
        self.assertEqual(report.source, "2025_annual.pdf")


class TestBalanceSheetTie(unittest.TestCase):
    def test_imbalance_fails(self):
        result = check_balance_sheet_tie(
            clean_snapshot(balance=balance(total_equity=590.0)), Tolerance()
        )
        self.assertEqual(result.status, "fail")
        assert_close(self, result.difference, 10.0)

    def test_difference_within_tolerance_passes(self):
        result = check_balance_sheet_tie(
            clean_snapshot(balance=balance(total_equity=599.5)), Tolerance()
        )
        self.assertEqual(result.status, "pass")

    def test_expected_value_is_liabilities_plus_equity(self):
        result = check_balance_sheet_tie(BASE_SNAPSHOT, Tolerance())
        assert_close(self, result.expected, 1000.0)
        assert_close(self, result.actual, 1000.0)


class TestEquityComposition(unittest.TestCase):
    def test_missing_minority_is_skipped(self):
        result = check_equity_composition(
            clean_snapshot(balance=balance(minority_equity=None)), Tolerance()
        )
        self.assertEqual(result.status, "skipped")
        self.assertIn("minority_equity", result.note)

    def test_mismatch_fails(self):
        result = check_equity_composition(
            clean_snapshot(balance=balance(minority_equity=30.0)), Tolerance()
        )
        self.assertEqual(result.status, "fail")
        # 期望 580 + 30 = 610，实际权益 600，差 -10
        assert_close(self, result.difference, -10.0)


class TestRetainedEarningsRollforward(unittest.TestCase):
    def test_manual_calculation(self):
        result = check_retained_earnings_rollforward(BASE_SNAPSHOT, Tolerance())
        # 100 + 55 - 5 - 20 = 130
        assert_close(self, result.expected, 130.0)
        self.assertEqual(result.status, "pass")

    def test_incomplete_inputs_cap_at_warning(self):
        """没提供分红与提取盈余公积时，差额可能由它们解释，不该判 fail。"""
        snapshot = clean_snapshot(
            balance=balance(retained_earnings=145.0),
            income=income(dividends_declared=None, reserve_appropriation=None),
        )
        result = check_retained_earnings_rollforward(snapshot, Tolerance())
        self.assertEqual(result.status, "warning")
        self.assertIn("所有者权益变动表", result.note)

    def test_complete_inputs_report_failure(self):
        snapshot = clean_snapshot(balance=balance(retained_earnings=145.0))
        result = check_retained_earnings_rollforward(snapshot, Tolerance())
        self.assertEqual(result.status, "fail")

    def test_missing_beginning_balance_is_skipped(self):
        result = check_retained_earnings_rollforward(
            clean_snapshot(balance_begin=None), Tolerance()
        )
        self.assertEqual(result.status, "skipped")


class TestAssetsFloor(unittest.TestCase):
    def test_clean_fixture_passes(self):
        self.assertEqual(check_assets_floor(BASE_SNAPSHOT, Tolerance()).status, "pass")

    def test_receivable_larger_than_assets_fails(self):
        result = check_assets_floor(
            clean_snapshot(balance=balance(accounts_receivable=1500.0)), Tolerance()
        )
        self.assertEqual(result.status, "fail")
        self.assertIn("应收账款", result.note)

    def test_missing_details_do_not_trigger(self):
        snapshot = clean_snapshot(
            balance=balance(cash_and_equivalents=None, accounts_receivable=None)
        )
        self.assertEqual(check_assets_floor(snapshot, Tolerance()).status, "pass")


class TestCashRollforward(unittest.TestCase):
    def test_clean_fixture_passes(self):
        result = check_cash_rollforward(BASE_SNAPSHOT, Tolerance())
        # 120 + 70 - 30 - 10 = 150
        assert_close(self, result.expected, 150.0)
        self.assertEqual(result.status, "pass")

    def test_fx_effect_is_included(self):
        snapshot = clean_snapshot(
            cashflow=cashflow(cash_end=155.0, fx_effect=5.0)
        )
        result = check_cash_rollforward(snapshot, Tolerance())
        assert_close(self, result.expected, 155.0)
        self.assertEqual(result.status, "pass")

    def test_wrong_cash_end_fails(self):
        result = check_cash_rollforward(
            clean_snapshot(cashflow=cashflow(cash_end=140.0)), Tolerance()
        )
        self.assertEqual(result.status, "fail")
        assert_close(self, result.difference, -10.0)

    def test_missing_begin_is_skipped(self):
        result = check_cash_rollforward(
            clean_snapshot(cashflow=cashflow(cash_begin=None)), Tolerance()
        )
        self.assertEqual(result.status, "skipped")


class TestCashDefinition(unittest.TestCase):
    def test_identical_values_pass(self):
        self.assertEqual(
            check_cash_definition(BASE_SNAPSHOT, Tolerance()).status, "pass"
        )

    def test_small_difference_is_warning(self):
        """货币资金含受限资金是常态，差异小的时候降级为 warning。"""
        snapshot = clean_snapshot(
            balance=balance(cash_and_equivalents=143.0)
        )
        result = check_cash_definition(snapshot, Tolerance())
        self.assertEqual(result.status, "warning")
        self.assertIn("受限资金", result.note)

    def test_large_difference_is_failure(self):
        snapshot = clean_snapshot(balance=balance(cash_and_equivalents=100.0))
        result = check_cash_definition(snapshot, Tolerance())
        self.assertEqual(result.status, "fail")
        assert_close(self, result.difference, 50.0)

    def test_missing_values_are_skipped(self):
        snapshot = clean_snapshot(balance=balance(cash_and_equivalents=None))
        self.assertEqual(check_cash_definition(snapshot, Tolerance()).status, "skipped")


class TestRevenueReceivableDivergence(unittest.TestCase):
    def test_clean_fixture_passes(self):
        result = check_revenue_receivable_divergence(BASE_SNAPSHOT)
        # 收入 500/450-1 = 11.1%，应收 80/65-1 = 23.1%，差 12.0 个百分点
        assert_close(self, result.metric, 0.119658, places=6)
        self.assertIsNone(result.difference)
        self.assertEqual(result.status, "pass")

    def test_large_gap_triggers_warning(self):
        snapshot = clean_snapshot(
            balance_begin=replace(BASE_BALANCE_BEGIN, accounts_receivable=55.0)
        )
        result = check_revenue_receivable_divergence(snapshot)
        # 应收 80/55-1 = 45.5%，与收入增速相差 34.3 个百分点
        assert_close(self, result.metric, 0.343434, places=6)
        self.assertEqual(result.status, "warning")
        self.assertIn("收入确认", result.note)

    def test_threshold_is_configurable(self):
        result = check_revenue_receivable_divergence(
            BASE_SNAPSHOT, threshold=0.10
        )
        self.assertEqual(result.status, "warning")

    def test_missing_prior_data_is_skipped(self):
        self.assertEqual(
            check_revenue_receivable_divergence(
                clean_snapshot(income_prior=None)
            ).status,
            "skipped",
        )

    def test_non_positive_prior_is_skipped(self):
        snapshot = clean_snapshot(
            balance_begin=replace(BASE_BALANCE_BEGIN, accounts_receivable=0.0)
        )
        self.assertEqual(
            check_revenue_receivable_divergence(snapshot).status, "skipped"
        )


class TestProfitCashConversion(unittest.TestCase):
    def test_clean_fixture_passes(self):
        result = check_profit_cash_conversion(BASE_SNAPSHOT)
        # 70 / 60 = 1.1667
        assert_close(self, result.metric, 1.166667, places=6)
        self.assertIsNone(result.difference)
        self.assertEqual(result.status, "pass")

    def test_low_conversion_triggers_warning(self):
        snapshot = clean_snapshot(cashflow=cashflow(operating=20.0))
        result = check_profit_cash_conversion(snapshot)
        assert_close(self, result.metric, 1 / 3, places=6)
        self.assertEqual(result.status, "warning")
        self.assertIn("含金量", result.note)

    def test_negative_profit_is_not_flagged(self):
        snapshot = clean_snapshot(income=income(net_profit=-10.0))
        result = check_profit_cash_conversion(snapshot)
        self.assertEqual(result.status, "pass")
        self.assertIn("不适用", result.note)


class TestNonRecurringShare(unittest.TestCase):
    def test_clean_fixture_passes(self):
        result = check_non_recurring_share(BASE_SNAPSHOT)
        # 10 / 55 = 18.2%
        assert_close(self, result.metric, 10 / 55, places=6)
        self.assertEqual(result.status, "pass")

    def test_high_share_triggers_warning(self):
        snapshot = clean_snapshot(income=income(non_recurring_gains=25.0))
        result = check_non_recurring_share(snapshot)
        assert_close(self, result.metric, 25 / 55, places=6)
        self.assertEqual(result.status, "warning")
        self.assertIn("不应把这块外推", result.note)

    def test_missing_data_is_skipped(self):
        snapshot = clean_snapshot(income=income(non_recurring_gains=None))
        self.assertEqual(check_non_recurring_share(snapshot).status, "skipped")

    def test_negative_attributable_profit_is_skipped(self):
        snapshot = clean_snapshot(
            income=income(net_profit_attributable=-5.0, non_recurring_gains=10.0)
        )
        self.assertEqual(check_non_recurring_share(snapshot).status, "skipped")


class TestScaleSanity(unittest.TestCase):
    def test_clean_fixture_passes(self):
        result = check_scale_sanity(BASE_SNAPSHOT)
        assert_close(self, result.metric, 0.5, places=10)
        self.assertEqual(result.status, "pass")

    def test_unit_mix_up_is_caught(self):
        """收入按元、资产按万元时，比值会高出两个数量级。"""
        snapshot = clean_snapshot(income=income(revenue=500.0 * 10000))
        result = check_scale_sanity(snapshot)
        self.assertEqual(result.status, "fail")
        self.assertIn("单位", result.note)

    def test_too_small_ratio_is_caught(self):
        snapshot = clean_snapshot(income=income(revenue=1.0))
        self.assertEqual(check_scale_sanity(snapshot).status, "fail")

    def test_zero_assets_is_skipped(self):
        snapshot = clean_snapshot(balance=balance(total_assets=0.0))
        self.assertEqual(check_scale_sanity(snapshot).status, "skipped")


class TestRestatement(unittest.TestCase):
    def test_consistent_values_pass(self):
        result = check_restatement("营业收入", 450.0, 450.0, 2025, Tolerance())
        self.assertEqual(result.status, "pass")

    def test_inconsistent_values_fail(self):
        result = check_restatement("营业收入", 440.0, 450.0, 2025, Tolerance())
        self.assertEqual(result.status, "fail")
        assert_close(self, result.difference, -10.0)
        self.assertIn("追溯调整", result.note)

    def test_check_id_and_category(self):
        result = check_restatement("营业收入", 450.0, 450.0, 2025, Tolerance())
        self.assertEqual(result.check_id, "X-05")
        self.assertEqual(result.category, "cross_period")


class TestTolerance(unittest.TestCase):
    def test_absolute_dominates_for_small_scale(self):
        assert_close(self, Tolerance(absolute=1.0, relative=1e-6).limit_for(100.0), 1.0)

    def test_relative_dominates_for_large_scale(self):
        assert_close(self, 
            Tolerance(absolute=1.0, relative=1e-6).limit_for(10_000_000.0), 10.0
        )

    def test_custom_tolerance_changes_verdict(self):
        snapshot = clean_snapshot(balance=balance(total_equity=590.0))
        strict = check_balance_sheet_tie(snapshot, Tolerance(absolute=0.1))
        loose = check_balance_sheet_tie(snapshot, Tolerance(absolute=20.0))
        self.assertEqual(strict.status, "fail")
        self.assertEqual(loose.status, "pass")


class TestReport(unittest.TestCase):
    def test_summary_counts_every_check(self):
        # 只动资产总计，这样只有 BS-01 会失败（BS-02 用的是权益内部结构，不受影响）
        snapshot = clean_snapshot(
            income=income(non_recurring_gains=None),  # X-03 skipped
            balance=balance(total_assets=1010.0),  # BS-01 fail
        )
        report = run_checks(snapshot)
        summary = report.summary
        self.assertEqual(summary["total"], 11)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["fail"], 1)
        self.assertEqual(
            summary["pass"] + summary["warning"] + summary["fail"] + summary["skipped"],
            11,
        )

    def test_failures_and_warnings_accessors(self):
        snapshot = clean_snapshot(
            balance=balance(total_equity=590.0),
            cashflow=cashflow(operating=20.0),
        )
        report = run_checks(snapshot)
        self.assertIn("BS-01", [check.check_id for check in report.failures])
        self.assertIn("X-02", [check.check_id for check in report.warnings])

    def test_by_id(self):
        report = run_checks(BASE_SNAPSHOT)
        self.assertEqual(report.by_id("BS-01").status, "pass")
        with self.assertRaises(KeyError):
            report.by_id("NO-SUCH")

    def test_to_dict_is_json_serializable(self):
        report = run_checks(BASE_SNAPSHOT)
        payload = json.dumps(report.to_dict(), ensure_ascii=False)
        restored = json.loads(payload)
        self.assertEqual(restored["fiscal_year"], 2025)
        self.assertEqual(restored["summary"]["total"], 11)
        self.assertEqual(len(restored["checks"]), 11)
        # 第一条是数据完整性检查，它必须排在最前面，否则全零数据会先报出假通过
        self.assertEqual(restored["checks"][0]["check_id"], "DQ-01")

    def test_thresholds_are_configurable_from_run_checks(self):
        report = run_checks(BASE_SNAPSHOT, ar_divergence_threshold=0.10)
        self.assertEqual(report.by_id("X-01").status, "warning")

    def test_amount_differences_and_metrics_do_not_mix(self):
        """恒等式类检查填 difference（元），信号类检查填 metric，二者不混用。"""
        report = run_checks(BASE_SNAPSHOT)
        for check_id in ("BS-01", "BS-02", "BS-03", "CF-01", "CF-02"):
            self.assertIsNotNone(report.by_id(check_id).difference, check_id)
            self.assertIsNone(report.by_id(check_id).metric, check_id)
        for check_id in ("X-01", "X-02", "X-03", "X-04"):
            self.assertIsNone(report.by_id(check_id).difference, check_id)
            self.assertIsNotNone(report.by_id(check_id).metric, check_id)


if __name__ == "__main__":
    unittest.main()
