"""DCF 演示：跑一遍常数现金流例子，把中间结果打印成表格给人看。

运行（在仓库根目录 FinAI/ 下）：
    python3 algorithm/examples/dcf_demo.py

这个文件和 tests/test_dcf.py 的分工
    tests/test_dcf.py   自动化测试，用断言判断对错，跑完只给 pass / fail
    本文件              演示脚本，把中间结果打印出来，给人看和讲解用

两者的数据源是同一个常数现金流例子，这不是重复，而是同一组数字的两种用途：
测试拿它做断言，演示拿它做展示。之所以选这个例子，是因为它能纯手算验证——
现金流恒定且 g = 0 时，企业价值必然等于 FCFF / WACC = 15 / 10% = 150。

对照纸面计算：FCFF 每年 15，WACC 10%，g = 0
    PV(显式期) = 56.8618，PV(终值) = 93.1382，企业价值 = 150.00
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from algorithm.finai.dcf import value_dcf  # noqa: E402
from algorithm.finai.types import ForecastAssumptions, MarketData  # noqa: E402

ASSUMPTIONS = ForecastAssumptions(
    revenue_base=100.0,
    revenue_growth=(0.0, 0.0, 0.0, 0.0, 0.0),
    ebit_margin=(0.20, 0.20, 0.20, 0.20, 0.20),
    tax_rate=0.25,
    da_pct_revenue=0.05,
    capex_pct_revenue=0.05,
    nwc_pct_revenue=0.10,
    terminal_growth=0.0,
)

MARKET = MarketData(
    price=10.0,
    diluted_shares=10.0,
    interest_bearing_debt=40.0,
    cash=10.0,
    as_of="2026-09-30",
)


def main() -> None:
    result = value_dcf(ASSUMPTIONS, MARKET, wacc=0.10, base_year=2025)

    print("=" * 68)
    print("预测期明细")
    print("=" * 68)
    print(
        f"{'年份':>6}{'收入':>12}{'EBIT':>12}{'FCFF':>12}"
        f"{'折现因子':>12}{'现值':>12}"
    )
    for i, year in enumerate(result.projection.years):
        print(
            f"{year:>6}"
            f"{result.projection.revenue[i]:>12.2f}"
            f"{result.projection.ebit[i]:>12.2f}"
            f"{result.projection.fcff[i]:>12.2f}"
            f"{result.discount_factor[i]:>12.6f}"
            f"{result.pv_fcff[i]:>12.2f}"
        )

    print()
    print("=" * 68)
    print("估值结果")
    print("=" * 68)
    print(f"WACC                     {result.wacc:>14.4%}")
    print(f"显式期现值合计           {sum(result.pv_fcff):>14.2f}")
    print(f"终值                     {result.terminal_value:>14.2f}")
    print(f"终值现值                 {result.pv_terminal:>14.2f}")
    print(f"企业价值                 {result.enterprise_value:>14.2f}")
    print(f"减：净债务               {result.net_debt:>14.2f}")
    print(f"权益价值                 {result.equity_value:>14.2f}")
    print(f"摊薄股数                 {result.diluted_shares:>14.2f}")
    print(f"每股价值                 {result.per_share:>14.2f}")
    print(f"当前股价                 {result.price:>14.2f}")
    print(f"隐含回报                 {result.implied_return:>14.2%}")
    print(f"终值占企业价值           {result.terminal_share_of_ev:>14.2%}")
    print(f"隐含退出倍数             {result.implied_exit_multiple:>14.2f}x")

    if result.diagnostics:
        print()
        print("诊断")
        for note in result.diagnostics:
            print(f"  - {note}")

    print()
    assert abs(result.enterprise_value - 150.0) < 1e-6, "企业价值应为 150.00"
    assert abs(result.per_share - 12.0) < 1e-6, "每股价值应为 12.00"
    print("与企业价值 150.00、每股价值 12.00 的手算结果一致。")


if __name__ == "__main__":
    main()
