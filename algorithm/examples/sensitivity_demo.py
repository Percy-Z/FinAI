"""敏感性与反向求解演示。

运行：python3 examples/sensitivity_demo.py

打印龙卷风图、区间归因、双因素网格和反向求解结果，
并把 tornado.csv 与 sensitivity.csv 写到 runs/demo/ 下。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from algorithm.finai.dcf import value_dcf  # noqa: E402
from algorithm.finai.reverse_dcf import reverse_solve_many  # noqa: E402
from algorithm.finai.sensitivity import (  # noqa: E402
    Variable,
    attribute,
    tornado,
    two_way_grid,
    write_grid_csv,
    write_tornado_csv,
)
from algorithm.finai.types import ForecastAssumptions, MarketData  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "outputs" / "demo"

ASSUMPTIONS = ForecastAssumptions(
    revenue_base=100.0,
    revenue_growth=(0.02, 0.02, 0.02, 0.02, 0.02),
    ebit_margin=(0.20, 0.20, 0.20, 0.20, 0.20),
    tax_rate=0.25,
    da_pct_revenue=0.05,
    capex_pct_revenue=0.05,
    nwc_pct_revenue=0.10,
    terminal_growth=0.025,
)

MARKET = MarketData(
    price=10.0,
    diluted_shares=10.0,
    interest_bearing_debt=40.0,
    cash=10.0,
    as_of="2026-09-30",
)

BASE_WACC = 0.10


def main() -> None:
    base = value_dcf(ASSUMPTIONS, MARKET, BASE_WACC)
    print(
        f"基准每股价值 {base.per_share:.2f}，当前股价 {MARKET.price:.2f}，"
        f"隐含回报 {base.implied_return:+.1%}"
    )

    variables = [
        Variable.absolute("WACC", "wacc", base=BASE_WACC, delta=0.01),
        Variable.absolute("永续增长率", "terminal_growth", base=0.025, delta=0.01),
        Variable.absolute("收入增速", "revenue_growth", base=0.02, delta=0.03),
        Variable.absolute("EBIT 利润率", "ebit_margin", base=0.20, delta=0.03),
        Variable.absolute("税率", "tax_rate", base=0.25, delta=0.05),
        Variable.absolute("资本开支占比", "capex_pct_revenue", base=0.05, delta=0.01),
    ]

    rows = tornado(ASSUMPTIONS, MARKET, BASE_WACC, variables)

    print()
    print("=" * 78)
    print("龙卷风图（按影响幅度排序）")
    print("=" * 78)
    print(
        f"{'变量':<14}{'下界':>10}{'上界':>10}"
        f"{'下界价值':>12}{'上界价值':>12}{'幅度':>10}"
    )
    for row in rows:
        print(
            f"{row.name:<14}{row.low:>10.4f}{row.high:>10.4f}"
            f"{row.per_share_at_low:>12.2f}{row.per_share_at_high:>12.2f}"
            f"{row.swing:>10.2f}"
        )

    print()
    print("=" * 78)
    print("区间归因（谁撑开了估值区间）")
    print("=" * 78)
    for attr in attribute(rows):
        bar = "#" * round(attr.share * 40)
        print(f"{attr.name:<14}{attr.share:>8.1%}  {bar}")

    grid = two_way_grid(
        ASSUMPTIONS,
        MARKET,
        BASE_WACC,
        Variable.bounds("永续增长率", "terminal_growth", 0.005, 0.045, base=0.025),
        Variable.bounds("WACC", "wacc", 0.08, 0.12, base=0.10),
        points=5,
    )

    print()
    print("=" * 78)
    print("双因素网格：每股价值（行 = 永续增长率，列 = WACC）")
    print("=" * 78)
    print("g \\ WACC".ljust(12) + "".join(f"{c:>10.2%}" for c in grid.col_values))
    for row_value, line in zip(grid.row_values, grid.cells):
        print(f"{row_value:>10.2%}  " + "".join(f"{value:>10.2f}" for value in line))
    low, high = grid.value_range
    print(f"\n网格内估值区间 [{low:.2f}, {high:.2f}]，现价 {grid.price:.2f}")

    reverse_variables = [
        Variable.bounds("永续增长率", "terminal_growth", -0.10, 0.05, base=0.025),
        Variable.bounds("WACC", "wacc", 0.03, 0.40, base=BASE_WACC),
        Variable.absolute("收入增速", "revenue_growth", base=0.02, delta=0.22),
        Variable.absolute("EBIT 利润率", "ebit_margin", base=0.20, delta=0.15),
    ]
    results = reverse_solve_many(ASSUMPTIONS, MARKET, BASE_WACC, reverse_variables)

    print()
    print("=" * 78)
    print("反向 DCF：要让每股价值等于现价 10.00，各参数得是多少")
    print("=" * 78)
    for result in results:
        print(
            f"{result.name:<14}基准 {result.base_value:>8.4f}   "
            f"市场隐含 {result.implied_value:>8.4f}   "
            f"方向 {result.direction:<7}迭代 {result.iterations} 次"
        )

    tornado_path = write_tornado_csv(OUTPUT_DIR / "tornado.csv", rows)
    grid_path = write_grid_csv(OUTPUT_DIR / "sensitivity.csv", grid)
    print()
    print(f"已写出 {tornado_path}")
    print(f"已写出 {grid_path}")


if __name__ == "__main__":
    main()
