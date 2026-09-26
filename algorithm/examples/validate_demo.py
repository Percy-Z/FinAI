"""勾稽校验演示。

运行：python3 examples/validate_demo.py

用一份「报表平了但利润质量存疑」的样例数据跑完全部检查，
打印结论并写出 outputs/demo/validation_report.json。

样例里的问题都是刻意埋的：
    应收账款增速 46.2%，营业收入增速 11.1%  -> X-01 告警
    经营现金流 25，净利润 60，比率 0.42     -> X-02 告警
    非经常性损益 20，占归母净利润 36.4%     -> X-03 告警
    未分配利润期末 145，按滚动应为 130      -> BS-03 失败
    货币资金 143 与现金流量表期末现金 150   -> CF-02 告警
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from algorithm.finai.statements import (  # noqa: E402
    BalanceSheet,
    CashFlowStatement,
    IncomeStatement,
    ReportSnapshot,
)
from algorithm.finai.validate import run_checks  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "outputs" / "demo"

SNAPSHOT = ReportSnapshot(
    fiscal_year=2025,
    source="示例公司_2025_annual.pdf",
    balance=BalanceSheet(
        total_assets=1000.0,
        total_liabilities=400.0,
        total_equity=600.0,
        equity_attributable=580.0,
        minority_equity=20.0,
        retained_earnings=145.0,  # 按滚动应为 130，刻意埋错
        cash_and_equivalents=143.0,  # 与现金流量表期末现金 150 相差 7
        accounts_receivable=95.0,
        inventory=120.0,
    ),
    balance_begin=BalanceSheet(
        total_assets=900.0,
        total_liabilities=380.0,
        total_equity=520.0,
        retained_earnings=100.0,
        cash_and_equivalents=120.0,
        accounts_receivable=65.0,
    ),
    income=IncomeStatement(
        revenue=500.0,
        net_profit=60.0,
        net_profit_attributable=55.0,
        non_recurring_gains=20.0,
        dividends_declared=20.0,
        reserve_appropriation=5.0,
    ),
    income_prior=IncomeStatement(revenue=450.0, net_profit=50.0),
    cashflow=CashFlowStatement(
        operating=25.0,
        investing=-30.0,
        financing=35.0,
        cash_begin=120.0,
        cash_end=150.0,
    ),
)

STATUS_MARK = {"pass": "通过", "warning": "告警", "fail": "失败", "skipped": "跳过"}


def main() -> None:
    report = run_checks(SNAPSHOT)

    print("=" * 86)
    print(f"勾稽校验报告：{SNAPSHOT.source}（{report.fiscal_year} 年）")
    print("=" * 86)
    print(f"{'编号':<8}{'类别':<14}{'结论':<6}检查项")
    print("-" * 86)
    for check in report.checks:
        print(
            f"{check.check_id:<8}{check.category:<14}"
            f"{STATUS_MARK[check.status]:<6}{check.description}"
        )

    print()
    print("=" * 86)
    print("需要处理的结论")
    print("=" * 86)
    for check in report.failures + report.warnings:
        print(f"\n[{STATUS_MARK[check.status]}] {check.check_id} {check.description}")
        if check.metric is not None:
            print(f"  指标值 {check.metric:,.4f}   触发阈值 {check.tolerance}")
        elif check.difference is not None:
            print(f"  期望 {check.expected:,.2f}   实际 {check.actual:,.2f}")
            print(f"  差异 {check.difference:,.2f}")
        if check.note:
            print(f"  {check.note}")

    summary = report.summary
    print()
    print("=" * 86)
    print(
        f"合计 {summary['total']} 项：通过 {summary['pass']}，"
        f"告警 {summary['warning']}，失败 {summary['fail']}，"
        f"跳过 {summary['skipped']}"
    )
    print("=" * 86)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUTPUT_DIR / "validation_report.json"
    target.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n已写出 {target}")


if __name__ == "__main__":
    main()
