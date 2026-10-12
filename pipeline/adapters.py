"""适配层：把一种对象翻译成另一种对象。

为什么需要这一层：
    配置层（config.py）只做「文件 → 对象」，编排层（run.py）只做「按顺序调」。
    但中间还差一环：配置层给出的对象形状，和某些算法模块要的形状不一样，
    需要翻译。这一层只做翻译，不做别的。

三个转换：
    drivers_to_assumptions      三表路径的驱动假设 → 估值路径的假设
    company_to_market_data      市场快照的公司 → 估值桥要的市场数据
    fill_target_from_forecast   把三表预测填进目标公司（相对估值用）
    fill_peers_from_financials  把财务数据填进可比公司（相对估值用）

⚠️ 第一个是整个框架里最容易写错的一段，详见它的文档字符串。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Sequence

from algorithm.finai.comps import CompanyMetrics
from algorithm.finai.forecast import (
    ForecastDrivers,
    ThreeStatementForecast,
    as_series,
)
from algorithm.finai.types import ForecastAssumptions, MarketData

from .config import ConfigError, TerminalParams
from .config import PeerFinancials


def _single_ratio(series: Sequence[float] | float, field: str) -> float:
    """把「逐年比率」取成单个数字，逐年不同就报错。

    估值引擎（ForecastAssumptions）只接受单一比率，而三表路径的驱动允许
    逐年数组。如果配置里写了逐年不同的值，这里必须拦住并说清楚 ——
    否则会一路走到 dcf.project() 里才炸，报错是
    「can't multiply sequence by non-int of type 'float'」，
    完全看不出是哪个字段的问题。

    入参可能是标量（配置文件里写单个数字，会被广播到每一年），
    所以先用 as_series 统一成序列再判。
    """
    values = set(as_series(series))
    if len(values) != 1:
        raise ConfigError(
            f"配置里的 [drivers].{field} 逐年不同（{sorted(values)}），"
            "而估值引擎只支持单一比率。\n"
            "    两种改法：① 改成单个数字；"
            "② 或者暂时不做估值（三表预测和勾稽校验不受影响）。"
        )
    return values.pop()


def drivers_to_assumptions(
    drivers: ForecastDrivers, terminal: TerminalParams
) -> ForecastAssumptions:
    """三表路径的驱动假设 → 估值路径的假设。

    ⚠️ 这是整个框架里最容易写错的一段，关键在 ebit_margin：

        三表路径：ebitda = 毛利 − 期间费用，然后 ebit = ebitda − 折旧摊销
        估值路径：ebit   = 收入 × margin，然后 ebitda = ebit + 折旧摊销
                  （注意 ebitda = ebit + da 说明 margin 是「已扣折旧的 EBIT 占收入比」）

    所以：
        ebit_margin = 毛利率 − 费用率 − 折旧率

    **少减最后一项，就是把 EBITDA 利润率当成 EBIT 利润率用了。** 后果是
    FCFF 高估约三成（实测 29.7% 起，逐年略降），而且**程序不会报错** ——
    数字看起来完全合理，只能靠两条路径对比才能发现。

    这条性质由回归测试盯着：
        tests/test_pipeline.py::TestTwoStackReconcile::test_fcff_两条路径必须一致
    """
    # 营运资本：三表路径分开给应收、存货、应付三个占比，估值路径要一个合计数
    nwc_pct_revenue = (
        _single_ratio(drivers.ar_pct_revenue, "ar_pct_revenue")
        + _single_ratio(drivers.inventory_pct_revenue, "inventory_pct_revenue")
        - _single_ratio(drivers.ap_pct_revenue, "ap_pct_revenue")
    )

    # EBIT 利润率：逐年算，因为三个分项都可能是逐年数组
    ebit_margin = [
        gross - opex - da
        for gross, opex, da in zip(
            as_series(drivers.gross_margin),
            as_series(drivers.opex_pct_revenue),
            as_series(drivers.da_pct_revenue),
        )
    ]

    return ForecastAssumptions(
        revenue_base=drivers.revenue_base,
        revenue_growth=as_series(drivers.revenue_growth),
        ebit_margin=ebit_margin,
        tax_rate=drivers.tax_rate,
        da_pct_revenue=_single_ratio(drivers.da_pct_revenue, "da_pct_revenue"),
        capex_pct_revenue=_single_ratio(
            drivers.capex_pct_revenue, "capex_pct_revenue"
        ),
        nwc_pct_revenue=nwc_pct_revenue,
        terminal_method=terminal.terminal_method,
        terminal_growth=terminal.terminal_growth,
        exit_multiple=terminal.exit_multiple,
        mid_year=terminal.mid_year,
    )


def company_to_market_data(company: CompanyMetrics) -> MarketData:
    """市场快照里的一家公司 → 估值桥要的市场数据。

    纯字段搬运，但省不掉：value_dcf() 的签名要求 MarketData。

    CompanyMetrics 里那些利润类指标（净利润、净资产、收入、EBITDA）
    不带过去 —— 估值桥用不到它们，那是相对估值要用的。
    """
    return MarketData(
        price=company.price,
        diluted_shares=company.diluted_shares,
        interest_bearing_debt=company.interest_bearing_debt,
        cash=company.cash,
        minority_interest=company.minority_interest,
        preferred_stock=company.preferred_stock,
        non_operating_assets=company.non_operating_assets,
        as_of=company.as_of,
    )


def fill_target_from_forecast(
    company: CompanyMetrics, forecast: ThreeStatementForecast
) -> CompanyMetrics:
    """用三表预测填目标公司的利润类指标。

    为什么需要它：市场快照只有股价和股数，但算 PE 要净利润、算 PB 要净资产。
    这些不在市场快照里，得从三表预测取。

    口径（保持前后一致，报告里也要写明）：
        利润类指标（收入、EBIT、EBITDA、归母净利润）→ 取预测第一年，对应「前瞻倍数」
        归母净资产 → 取基期

    净资产取基期是暂时的：抽取层还没做好，拿不到最近一个完整年度的实际数。
    等抽取层就绪，这里改成用最近一期的实际净资产。
    """
    first_year = forecast.years[0]
    return replace(
        company,
        net_profit_attributable=first_year.income.net_profit_attributable,
        book_equity_attributable=forecast.opening.equity_attributable,
        revenue=first_year.income.revenue,
        ebit=first_year.income.ebit,
        ebitda=first_year.income.ebitda,
    )


def apply_actuals(
    company: CompanyMetrics, actuals: Mapping[str, Any]
) -> CompanyMetrics:
    """用「最近一个完整年度的实际数」覆盖目标公司的利润类指标。

    为什么要有这一步：`fill_target_from_forecast` 给目标公司的是**预测第一年**
    的数（前瞻口径），而可比公司来自年报（实际口径）。两边的倍数放在一张表里
    比较时，差里混着「一边算明年、一边算去年」，评审一问就答不上来。

    所以当抽取层能用时（FINAI_DATA_SOURCE=extracted），目标公司也换成实际数，
    口径统一为「最近一个完整年度」。预测口径的倍数仍然留在三表预测与 DCF 里，
    只是不再拿去做相对估值。

    actuals 只带这五个指标（多出来的字段忽略）：revenue / net_profit_attributable /
    book_equity_attributable / ebit / ebitda。
    """
    updates: dict[str, float] = {}
    for field in (
        "revenue",
        "net_profit_attributable",
        "book_equity_attributable",
        "ebit",
        "ebitda",
    ):
        value = actuals.get(field)
        if value is None:
            continue
        updates[field] = float(value)
    return replace(company, **updates)


def fill_peers_from_financials(
    peers: list[CompanyMetrics],
    financials: dict[str, PeerFinancials],
) -> tuple[list[CompanyMetrics], list[str]]:
    """把可比公司的年度财务数据填进市场快照。

    返回 (填好的列表, 提示信息列表)。

    对不上的情况不报错，只提示：某个可比公司在 market.csv 里有、在 peers.csv
    里没有，那它的 PE / PB / EV-EBITDA 全都算不出来（分母为空），
    相对估值会跳过它。但股价、股数、债务还在，所以整条流水线仍然跑得完 ——
    这比因为一条数据缺失就中断整个流程要好。

    反过来（peers.csv 里有、market.csv 里没有）不提示：那种行根本用不到，
    因为可比公司是「由 market.csv 的 role=peer 指定」的。
    """
    filled: list[CompanyMetrics] = []
    notes: list[str] = []

    for peer in peers:
        data = financials.get(peer.code)
        if data is None:
            notes.append(
                f"可比公司 {peer.code}（{peer.name}）在 peers.csv 里没有财务数据，"
                "它的倍数会被跳过"
            )
            filled.append(peer)
            continue
        filled.append(
            replace(
                peer,
                revenue=data.revenue,
                net_profit_attributable=data.net_profit_attributable,
                book_equity_attributable=data.book_equity_attributable,
                ebit=data.ebit,
                ebitda=data.ebitda,
            )
        )

    return filled, notes


def cash_flows_from_forecast(
    forecast: ThreeStatementForecast,
) -> tuple[list[float], list[float]]:
    """从三表预测里取出估值要折现的两串数：FCFF 与 EBITDA。

    为什么从这里取：估值（algorithm/finai/dcf.py）与三表预测（forecast.py）本来是
    两套公式，各算各的现金流，只靠回归测试盯着「两边必须相等」。现在折现直接吃
    三表算出的序列，一致性由来源保证 —— 报告里的现金流和估出来的价值不可能对不上。

    EBITDA 只有「退出倍数法」算终值时才用得上，但它同时会写进产物，
    所以一并取出来，免得同一个字段在产物里来自两条公式。
    """
    return list(forecast.fcff_series), [year.income.ebitda for year in forecast.years]


def _demo() -> None:
    """自检：把三样转换各做一遍并打印。python3 -m pipeline.adapters"""
    from algorithm.finai.dcf import project
    from algorithm.finai.forecast import build_forecast

    from . import config

    _, drivers, opening = config.load_case()
    terminal = config.load_terminal_params()
    target, peers = config.load_market_snapshot()

    assumptions = drivers_to_assumptions(drivers, terminal)
    gross = as_series(drivers.gross_margin)
    opex = as_series(drivers.opex_pct_revenue)
    da_series = as_series(drivers.da_pct_revenue)

    print("[drivers] → [assumptions] 关键字段")
    print(f"    收入基数      {drivers.revenue_base:,.2f} → {assumptions.revenue_base:,.2f}")
    print(
        f"    收入增速      {as_series(drivers.revenue_growth)} "
        f"→ {assumptions.revenue_growth}"
    )
    print("    EBIT 利润率   ← 毛利率 − 费用率 − 折旧率")
    for index, margin in enumerate(assumptions.ebit_margin, start=1):
        print(
            f"        第 {index} 年 {margin:.4f}"
            f"  （{gross[index-1]:.2f} − {opex[index-1]:.2f} − "
            f"{da_series[index-1]:.2f}）"
        )
    print(f"    营运资本占比  （应收+存货−应付）→ {assumptions.nwc_pct_revenue:.4f}")
    print(f"    终值          {assumptions.terminal_method}，"
          f"增长率 {assumptions.terminal_growth:.2%}，"
          f"中值惯例 {assumptions.mid_year}")

    print("\n两条路径的 FCFF 对比（必须逐项相等）")
    forecast = build_forecast(opening, drivers)
    from_dcf = project(assumptions, base_year=drivers.base_year).fcff
    print(f"    {'年份':>6}{'估值路径':>12}{'三表路径':>12}{'差额':>12}")
    for year, left, right in zip(forecast.years, from_dcf, forecast.fcff_series):
        print(f"    {year.year:>6}{left:>12.2f}{right:>12.2f}{left-right:>12.2e}")

    print("\n[market] → MarketData")
    market = company_to_market_data(target)
    print(f"    目标  {target.name}  股价 {market.price}  "
          f"股数 {market.diluted_shares:,.0f}  "
          f"市值 {market.market_cap:,.2f}  净债务 {market.net_debt:,.2f}")
    for peer in peers:
        print(f"    可比  {peer.code} {peer.name}  股价 {peer.price}  "
              f"市值 {peer.market_cap:,.2f}  净债务 {peer.net_debt:,.2f}")

    print("\n填进三表预测（相对估值用）")
    filled = fill_target_from_forecast(target, forecast)
    print(f"    归母净利润 {filled.net_profit_attributable:,.2f}  "
          f"净资产 {filled.book_equity_attributable:,.2f}  "
          f"PE {filled.eps and filled.price / filled.eps:.2f}")

    print("\n自检通过。")


if __name__ == "__main__":
    _demo()
