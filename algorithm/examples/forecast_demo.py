"""三表联动预测演示。

运行（在仓库根目录 FinAI/ 下）：
    python3 algorithm/examples/forecast_demo.py

打印三张表的逐年明细、平衡校验、自由现金流，
并把预测结果喂给勾稽校验，最后写出 projections.json。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from algorithm.finai.forecast import (  # noqa: E402
    BalanceSheetState,
    ForecastDrivers,
    build_forecast,
    to_snapshots,
)
from algorithm.finai.validate import run_checks  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "outputs" / "demo"

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

DRIVERS = ForecastDrivers(
    revenue_base=400.0,
    base_year=2025,
    years=5,
    revenue_growth=(0.10, 0.09, 0.08, 0.07, 0.06),
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

ROWS = [
    ("营业收入", lambda y: y.income.revenue),
    ("毛利", lambda y: y.income.gross_profit),
    ("期间费用", lambda y: y.income.operating_expenses),
    ("折旧摊销", lambda y: y.income.da),
    ("EBIT", lambda y: y.income.ebit),
    ("利息费用", lambda y: y.income.interest_expense),
    ("所得税", lambda y: y.income.income_tax),
    ("净利润", lambda y: y.income.net_profit),
    ("归母净利润", lambda y: y.income.net_profit_attributable),
    ("经营现金流", lambda y: y.cashflow.operating),
    ("资本开支", lambda y: y.cashflow.capex),
    ("投资现金流", lambda y: y.cashflow.investing),
    ("筹资现金流", lambda y: y.cashflow.financing),
    ("现金净增加", lambda y: y.cashflow.net_change),
    ("期末现金", lambda y: y.balance.cash),
    ("应收账款", lambda y: y.balance.accounts_receivable),
    ("存货", lambda y: y.balance.inventory),
    ("固定资产", lambda y: y.balance.fixed_assets),
    ("有息负债", lambda y: y.balance.interest_bearing_debt),
    ("留存收益", lambda y: y.balance.retained_earnings),
    ("资产总计", lambda y: y.balance.total_assets),
    ("负债合计", lambda y: y.balance.total_liabilities),
    ("权益合计", lambda y: y.balance.total_equity),
    ("平衡差额", lambda y: y.balance.balance_gap),
    ("FCFF", lambda y: y.fcff),
]


def main() -> None:
    forecast = build_forecast(OPENING, DRIVERS, source="forecast_demo")

    print("=" * 88)
    print("三表联动预测")
    print("=" * 88)
    header = f"{'科目':<14}" + "".join(
        f"{year.year:>13}" for year in forecast.years
    )
    print(header)
    print("-" * 88)
    for label, getter in ROWS:
        line = f"{label:<14}" + "".join(f"{getter(year):>13,.2f}" for year in forecast.years)
        print(line)

    print()
    print(
        f"全部年份的最大平衡差额：{forecast.worst_balance_gap:.2e}"
        "（现金由现金流量表推出，所以理论上恒为 0）"
    )

    print()
    print("=" * 88)
    print("把预测结果交给勾稽校验")
    print("=" * 88)
    print(f"{'年份':<8}{'通过':>6}{'告警':>6}{'失败':>6}{'跳过':>6}   说明")
    for snapshot in to_snapshots(forecast):
        report = run_checks(snapshot)
        summary = report.summary
        skipped = ",".join(
            check.check_id for check in report.checks if check.status == "skipped"
        )
        note = "预测里没有非经常性损益，X-03 跳过" if skipped else ""
        print(
            f"{snapshot.fiscal_year:<8}{summary['pass']:>6}{summary['warning']:>6}"
            f"{summary['fail']:>6}{summary['skipped']:>6}   {note}"
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUTPUT_DIR / "projections.json"
    target.write_text(
        json.dumps(
            {
                "source": "forecast_demo",
                "drivers": {
                    "revenue_base": DRIVERS.revenue_base,
                    "base_year": DRIVERS.base_year,
                    "years": DRIVERS.years,
                    "revenue_growth": list(DRIVERS.series("revenue_growth")),
                    "tax_rate": DRIVERS.tax_rate,
                    "dividend_payout_ratio": DRIVERS.dividend_payout_ratio,
                },
                "years": forecast.to_records(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print()
    print(f"已写出 {target}")


if __name__ == "__main__":
    main()
