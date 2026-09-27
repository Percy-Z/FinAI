"""端到端运行：假设进，文件出。

运行（在仓库根目录 FinAI/ 下）：
    python3 algorithm/examples/pipeline_demo.py

把算法层和输出层接起来，一次跑完：
    三表联动预测 → 勾稽校验 → 资本成本 → DCF 估值
    → 敏感性分析 → 反向求解 → 相对估值 → 足球场图
最后全部落到 outputs/<运行编号>/ 目录里，并写出带 sha256 的运行清单。

说明：目标公司的利润类指标用的是预测第一年的口径（前瞻倍数），
资产负债表用的是基期数，因为文档抽取层还没做，拿不到最近一个完整年度的实际数。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from algorithm.finai.comps import (  # noqa: E402
    CompanyMetrics,
    comps_bands,
    comps_valuation,
    dcf_band,
    football_field,
)
from algorithm.finai.dcf import value_dcf  # noqa: E402
from algorithm.finai.forecast import (  # noqa: E402
    BalanceSheetState,
    ForecastDrivers,
    build_forecast,
    to_snapshots,
)
from algorithm.output import (  # noqa: E402
    RunOutputs,
    export_run,
    summarize,
)
from algorithm.finai.reverse_dcf import reverse_solve_many  # noqa: E402
from algorithm.finai.sensitivity import (  # noqa: E402
    Variable,
    attribute,
    tornado,
    two_way_grid,
)
from algorithm.finai.types import ForecastAssumptions, MarketData  # noqa: E402
from algorithm.finai.validate import run_checks  # noqa: E402
from algorithm.finai.wacc import (  # noqa: E402
    check_wacc_sanity,
    wacc_from_market,
    weights_from_market,
)
from algorithm.finai.types import WaccInputs  # noqa: E402

# ---------------------------------------------------------------- 基期与假设

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

# 与三表预测对齐的 DCF 假设：毛利率 − 费用率 − 折旧率 = EBIT 利润率
ASSUMPTIONS = ForecastAssumptions(
    revenue_base=400.0,
    revenue_growth=(0.10, 0.09, 0.08, 0.07, 0.06),
    ebit_margin=(0.20,) * 5,
    tax_rate=0.25,
    da_pct_revenue=0.05,
    capex_pct_revenue=0.06,
    nwc_pct_revenue=0.15,
    terminal_growth=0.025,
)

MARKET = MarketData(
    price=10.0,
    diluted_shares=100.0,
    interest_bearing_debt=100.0,
    cash=55.0,
    as_of="2026-09-30",
)

# ---------------------------------------------------------------- 可比公司

PEERS = [
    CompanyMetrics(
        code="A", name="可比甲", price=21.0, diluted_shares=10.0,
        net_profit_attributable=15.0, book_equity_attributable=52.5,
        revenue=150.0, ebit=20.0, ebitda=25.0,
        interest_bearing_debt=5.0, cash=3.0, as_of="2026-09-30",
    ),
    CompanyMetrics(
        code="B", name="可比乙", price=15.0, diluted_shares=20.0,
        net_profit_attributable=20.0, book_equity_attributable=75.0,
        revenue=200.0, ebit=28.0, ebitda=35.0,
        interest_bearing_debt=10.0, cash=5.0, as_of="2026-09-30",
    ),
    CompanyMetrics(
        code="C", name="可比丙", price=30.0, diluted_shares=8.0,
        net_profit_attributable=18.0, book_equity_attributable=60.0,
        revenue=180.0, ebit=24.0, ebitda=30.0,
        interest_bearing_debt=5.0, cash=15.0, as_of="2026-09-30",
    ),
]


def main() -> None:
    # 1. 资本成本
    equity_weight, debt_weight = weights_from_market(
        MARKET.market_cap, MARKET.interest_bearing_debt
    )
    wacc_inputs = WaccInputs(
        risk_free=0.0235,
        beta=1.20,
        equity_risk_premium=0.065,
        cost_of_debt=0.035,
        tax_rate=0.25,
        equity_weight=equity_weight,
        debt_weight=debt_weight,
    )
    wacc = wacc_from_market(
        risk_free=wacc_inputs.risk_free,
        beta=wacc_inputs.beta,
        equity_risk_premium=wacc_inputs.equity_risk_premium,
        cost_of_debt=wacc_inputs.cost_of_debt,
        tax_rate=wacc_inputs.tax_rate,
        market_cap=MARKET.market_cap,
        debt_value=MARKET.interest_bearing_debt,
    )
    print("=" * 80)
    print("① 资本成本")
    print("=" * 80)
    print(f"权益权重 {equity_weight:.4f}　债务权重 {debt_weight:.4f}")
    print(f"WACC {wacc:.4%}")
    for warning in check_wacc_sanity(wacc_inputs):
        print(f"  提示：{warning}")

    # 2. 三表联动预测
    forecast = build_forecast(OPENING, DRIVERS, source="pipeline_demo")
    print()
    print("=" * 80)
    print("② 三表联动预测")
    print("=" * 80)
    for year in forecast.years:
        print(
            f"{year.year}  收入 {year.income.revenue:>9,.2f}"
            f"  EBIT {year.income.ebit:>8,.2f}"
            f"  归母净利润 {year.income.net_profit_attributable:>8,.2f}"
            f"  期末现金 {year.balance.cash:>9,.2f}"
            f"  平衡差额 {year.balance.balance_gap:>8.1e}"
        )

    # 3. 勾稽校验（把预测结果反过来验一遍）
    snapshots = to_snapshots(forecast)
    reports = [run_checks(snapshot) for snapshot in snapshots]
    print()
    print("=" * 80)
    print("③ 勾稽校验")
    print("=" * 80)
    for report in reports:
        summary = report.summary
        print(
            f"{report.fiscal_year}  通过 {summary['pass']}"
            f"  告警 {summary['warning']}  失败 {summary['fail']}"
            f"  跳过 {summary['skipped']}"
        )

    # 4. DCF 估值
    dcf = value_dcf(ASSUMPTIONS, MARKET, wacc)
    print()
    print("=" * 80)
    print("④ DCF 估值")
    print("=" * 80)
    print(f"企业价值 {dcf.enterprise_value:,.2f}　权益价值 {dcf.equity_value:,.2f}")
    print(
        f"每股价值 {dcf.per_share:.2f}　现价 {dcf.price:.2f}"
        f"　隐含回报 {dcf.implied_return:+.1%}"
    )
    print(f"终值占企业价值 {dcf.terminal_share_of_ev:.1%}　隐含退出倍数 {dcf.implied_exit_multiple:.2f}x")
    for note in dcf.diagnostics:
        print(f"  提示：{note}")

    # 5. 敏感性分析
    variables = [
        Variable.absolute("WACC", "wacc", base=wacc, delta=0.01),
        Variable.absolute("永续增长率", "terminal_growth", base=0.025, delta=0.01),
        Variable.absolute("收入增速", "revenue_growth", base=0.08, delta=0.03),
        Variable.absolute("EBIT 利润率", "ebit_margin", base=0.20, delta=0.03),
        Variable.absolute("资本开支占比", "capex_pct_revenue", base=0.06, delta=0.02),
    ]
    rows = tornado(ASSUMPTIONS, MARKET, wacc, variables)
    grid = two_way_grid(
        ASSUMPTIONS,
        MARKET,
        wacc,
        Variable.bounds("永续增长率", "terminal_growth", 0.015, 0.035, base=0.025),
        Variable.bounds("WACC", "wacc", wacc - 0.01, wacc + 0.01, base=wacc),
        points=3,
    )
    print()
    print("=" * 80)
    print("⑤ 敏感性分析")
    print("=" * 80)
    for row in rows:
        print(f"{row.name:<12} 幅度 {row.swing:>6.2f} 元/股")
    print("区间归因：" + "　".join(f"{a.name} {a.share:.1%}" for a in attribute(rows)))
    grid_low, grid_high = grid.value_range
    print(f"网格内每股价值区间 [{grid_low:.2f}, {grid_high:.2f}]")

    # 6. 反向求解
    reverse = reverse_solve_many(
        ASSUMPTIONS,
        MARKET,
        wacc,
        [
            Variable.bounds("永续增长率", "terminal_growth", -0.10, 0.05, base=0.025),
            Variable.bounds("WACC", "wacc", 0.03, 0.40, base=wacc),
            Variable.absolute("收入增速", "revenue_growth", base=0.08, delta=0.20),
        ],
    )
    print()
    print("=" * 80)
    print("⑥ 反向求解：要让每股价值等于现价，各参数得是多少")
    print("=" * 80)
    for item in reverse:
        print(
            f"{item.name:<12} 基准 {item.base_value:>8.4f}"
            f"　市场隐含 {item.implied_value:>8.4f}　方向 {item.direction}"
        )

    # 7. 相对估值
    target = CompanyMetrics(
        code="T",
        name="目标公司",
        price=MARKET.price,
        diluted_shares=MARKET.diluted_shares,
        net_profit_attributable=forecast.years[0].income.net_profit_attributable,
        book_equity_attributable=OPENING.equity_attributable,
        revenue=forecast.years[0].income.revenue,
        ebit=forecast.years[0].income.ebit,
        ebitda=forecast.years[0].income.ebitda,
        interest_bearing_debt=MARKET.interest_bearing_debt,
        cash=MARKET.cash,
        as_of=MARKET.as_of,
    )
    comps = comps_valuation(target, PEERS)
    print()
    print("=" * 80)
    print("⑦ 相对估值")
    print("=" * 80)
    for entry in comps.stats.values():
        if entry.count:
            print(
                f"{entry.label:<12} 中位数 {entry.median:>7.2f}"
                f"　区间 [{entry.q1:.2f}, {entry.q3:.2f}]　样本 {entry.count}"
            )
    for item in comps.medians:
        print(f"{item.label:<12} 隐含每股价值 {item.implied_per_share:>6.2f}")
    for warning in comps.warnings:
        print(f"  提示：{warning}")

    # 8. 足球场图
    field = football_field(
        [dcf_band(dcf, low=grid_low, high=grid_high)] + comps_bands(comps),
        price=MARKET.price,
        check_pairs=(("DCF", "PE"),),
    )
    print()
    print("=" * 80)
    print("⑧ 足球场图")
    print("=" * 80)
    for band in field.bands:
        print(f"{band.name:<12} [{band.low:>6.2f}, {band.high:>6.2f}]　中心 {band.central:>6.2f}")
    print(f"现价 {field.price:.2f}")
    for warning in field.warnings:
        print(f"  提示：{warning}")

    # 9. 落盘
    artifacts = export_run(
        RunOutputs(
            dcf=dcf,
            football_field=field,
            comps=comps,
            tornado=rows,
            grid=grid,
            reverse=reverse,
            validation=reports,
            forecast=forecast,
        ),
        meta={
            "target": target.code,
            "peers": [peer.code for peer in PEERS],
            "price": MARKET.price,
            "wacc": round(wacc, 6),
            "base_year": DRIVERS.base_year,
            "years": DRIVERS.years,
        },
    )
    print()
    print("=" * 80)
    print("⑨ 输出")
    print("=" * 80)
    print(summarize(artifacts))


if __name__ == "__main__":
    main()
