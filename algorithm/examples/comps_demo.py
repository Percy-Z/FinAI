"""相对估值演示。

运行（在仓库根目录 FinAI/ 下）：
    python3 algorithm/examples/comps_demo.py

演示三件事：
    1. 三家可比公司的倍数与分布
    2. 每种倍数给出的隐含每股价值
    3. 把 DCF 区间与相对估值区间画在同一张足球场图上，并检查是否重叠

数字与 tests/test_comps.py 的基准样本一致，方便对照手算。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from algorithm.finai.comps import (  # noqa: E402
    CompanyMetrics,
    comps_bands,
    comps_valuation,
    compute_multiples,
    dcf_band,
    football_field,
    write_comps_csv,
    write_implied_csv,
)
from algorithm.finai.dcf import value_dcf  # noqa: E402
from algorithm.finai.sensitivity import Variable, two_way_grid  # noqa: E402
from algorithm.finai.types import ForecastAssumptions, MarketData  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "outputs" / "demo"

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

PEERS = [
    CompanyMetrics(
        code="A",
        name="可比甲",
        price=20.0,
        diluted_shares=5.0,
        net_profit_attributable=10.0,
        book_equity_attributable=50.0,
        revenue=80.0,
        ebit=15.0,
        ebitda=20.0,
        cash=20.0,
        as_of="2026-09-30",
    ),
    CompanyMetrics(
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
        as_of="2026-09-30",
    ),
    CompanyMetrics(
        code="C",
        name="可比丙",
        price=15.0,
        diluted_shares=8.0,
        net_profit_attributable=12.0,
        book_equity_attributable=40.0,
        revenue=120.0,
        ebit=16.0,
        ebitda=20.0,
        as_of="2026-09-30",
    ),
]

# 与相对估值对照用的 DCF 假设
ASSUMPTIONS = ForecastAssumptions(
    revenue_base=100.0,
    revenue_growth=(0.03, 0.03, 0.02, 0.02, 0.02),
    ebit_margin=(0.20,) * 5,
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


def _show(value: float | None) -> str:
    return f"{value:>9.2f}" if value is not None else f"{'不适用':>9}"


def main() -> None:
    result = comps_valuation(TARGET, PEERS)

    print("=" * 88)
    print("一、每家公司的倍数")
    print("=" * 88)
    print(
        f"{'类型':<6}{'代码':<6}{'名称':<10}{'股价':>8}{'市值':>10}"
        f"{'企业价值':>12}{'PE':>10}{'PB':>9}{'EV/EBITDA':>11}"
    )
    for kind, company in [("目标", TARGET)] + [("可比", p) for p in PEERS]:
        values = compute_multiples(company)
        print(
            f"{kind:<6}{company.code:<6}{company.name:<10}"
            f"{company.price:>8.2f}{company.market_cap:>10.0f}"
            f"{company.enterprise_value:>12.0f}"
            f"{_show(values['pe'])}{_show(values['pb'])}{_show(values['ev_ebitda'])}"
        )

    print()
    print("=" * 88)
    print("二、可比公司倍数的分布")
    print("=" * 88)
    print(
        f"{'倍数':<12}{'样本数':>8}{'下四分位':>12}"
        f"{'中位数':>12}{'上四分位':>12}{'均值':>12}"
    )
    for entry in result.stats.values():
        if entry.count == 0:
            print(f"{entry.label:<12}{0:>8}{'—':>12}{'—':>12}{'—':>12}{'—':>12}")
            continue
        print(
            f"{entry.label:<12}{entry.count:>8}{entry.q1:>12.2f}"
            f"{entry.median:>12.2f}{entry.q3:>12.2f}{entry.mean:>12.2f}"
        )

    print()
    print("=" * 88)
    print("三、隐含每股价值")
    print("=" * 88)
    print(f"{'倍数':<12}{'口径':<10}{'可比倍数':>12}{'隐含每股价值':>14}{'相对现价':>12}")
    for item in result.implied:
        if not item.applicable:
            continue
        relative = item.implied_per_share / result.price - 1.0
        print(
            f"{item.label:<12}{item.stat_name:<10}{item.peer_value:>12.2f}"
            f"{item.implied_per_share:>14.2f}{relative:>12.1%}"
        )

    low, high = result.value_range
    print(
        f"\n相对估值区间 [{low:.2f}, {high:.2f}]，中心值 "
        f"{result.central_value:.2f}，隐含回报 {result.implied_return:+.1%}"
    )
    for note in result.warnings:
        print(f"  提示：{note}")

    # DCF 区间取自敏感性网格，两条独立的线放在一起看
    dcf = value_dcf(ASSUMPTIONS, MARKET, 0.10)
    grid = two_way_grid(
        ASSUMPTIONS,
        MARKET,
        0.10,
        Variable.bounds("永续增长率", "terminal_growth", 0.015, 0.035, base=0.025),
        Variable.bounds("WACC", "wacc", 0.09, 0.11, base=0.10),
        points=3,
    )
    dcf_low, dcf_high = grid.value_range

    bands = [dcf_band(dcf, low=dcf_low, high=dcf_high)] + comps_bands(result)
    field = football_field(bands, price=result.price, check_pairs=(("DCF", "PE"),))

    print()
    print("=" * 88)
    print("四、足球场图")
    print("=" * 88)
    width = 40
    span_low = min(field.overall_low, field.price) * 0.95
    span_high = max(field.overall_high, field.price) * 1.02
    for band_item in field.bands:
        start = int((band_item.low - span_low) / (span_high - span_low) * width)
        end = int((band_item.high - span_low) / (span_high - span_low) * width)
        bar = " " * start + "=" * max(end - start, 1)
        print(
            f"{band_item.name:<12}{band_item.low:>8.2f} "
            f"|{bar:<{width}}|{band_item.high:>9.2f}"
        )
    price_pos = int((field.price - span_low) / (span_high - span_low) * width)
    print(f"{'现价':<12}{field.price:>8.2f} |{' ' * price_pos}^")
    for note in field.warnings:
        print(f"  提示：{note}")

    comps_path = write_comps_csv(OUTPUT_DIR / "comps.csv", result)
    implied_path = write_implied_csv(OUTPUT_DIR / "comps_implied.csv", result)
    print()
    print(f"已写出 {comps_path}")
    print(f"已写出 {implied_path}")


if __name__ == "__main__":
    main()
