"""现金流折现：FCFF 预测、折现、终值、估值桥。

计算链
    收入        Revenue_t = Revenue_(t-1) x (1 + g_t)
    息税前利润  EBIT_t    = Revenue_t x EBIT 利润率_t
    税后经营利润 NOPAT    = EBIT x (1 - 税率)
    自由现金流  FCFF_t    = NOPAT + 折旧摊销 - 资本开支 - 营运资本增加
    折现因子    DF_t      = 1 / (1 + WACC)^t   （中值惯例为 ^(t-0.5)）
    终值        永续增长法 TV = FCFF_n x (1 + g) / (WACC - g)
                退出倍数法 TV = EBITDA_n x 退出倍数
    企业价值    EV        = Σ PV(FCFF_t) + PV(TV)
    权益价值    Equity    = EV - 付息债务 + 现金 - 少数股东权益 - 优先股 + 非经营性资产
    每股价值    per_share = Equity / 摊薄股数

口径纪律：无杠杆现金流（FCFF）配 WACC 折现到企业价值；有杠杆现金流配股权成本
折现到权益价值。两者混用是最常见的严重错误。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from .types import DCFResult, ForecastAssumptions, MarketData

# 终值占企业价值的诊断阈值，来自机构建模惯例
TERMINAL_SHARE_TOO_HIGH = 0.80
TERMINAL_SHARE_TOO_LOW = 0.40

# 折现用的现金流（外部传入，现在来自三表预测）与「按假设重算的明细」之间
# 允许的最大偏差，单位元。两者本来应当相等，超过这个数就说明两套口径开始分叉,
# 估值结果会把它写进 diagnostics，提醒报告里交代折现取的是哪一套。
FCFF_RECONCILE_TOLERANCE = 1.0


@dataclass
class Projection:
    """预测期的逐年明细。中间结果全部保留，便于回溯和导出 Excel。"""

    years: list[int] = field(default_factory=list)
    revenue: list[float] = field(default_factory=list)
    ebit: list[float] = field(default_factory=list)
    da: list[float] = field(default_factory=list)
    ebitda: list[float] = field(default_factory=list)
    capex: list[float] = field(default_factory=list)
    nwc: list[float] = field(default_factory=list)
    delta_nwc: list[float] = field(default_factory=list)
    nopat: list[float] = field(default_factory=list)
    fcff: list[float] = field(default_factory=list)


def project(
    assumptions: ForecastAssumptions, base_year: int | None = None
) -> Projection:
    """把经营假设展开成逐年明细。

    营运资本按「占收入比」建模，因此营运资本增加额等于本期减上期，第一期的上期值
    取自基期收入。收入不变时增加额为 0，这个性质在测试里会验证。
    """
    p = Projection()

    revenue = assumptions.revenue_base
    nwc_previous = assumptions.revenue_base * assumptions.nwc_pct_revenue

    for i, (growth, margin) in enumerate(
        zip(assumptions.revenue_growth, assumptions.ebit_margin), start=1
    ):
        revenue = revenue * (1.0 + growth)

        ebit = revenue * margin
        da = revenue * assumptions.da_pct_revenue
        capex = revenue * assumptions.capex_pct_revenue
        nwc = revenue * assumptions.nwc_pct_revenue
        delta_nwc = nwc - nwc_previous
        nopat = ebit * (1.0 - assumptions.tax_rate)
        fcff = nopat + da - capex - delta_nwc

        p.years.append(base_year + i if base_year is not None else i)
        p.revenue.append(revenue)
        p.ebit.append(ebit)
        p.da.append(da)
        p.ebitda.append(ebit + da)
        p.capex.append(capex)
        p.nwc.append(nwc)
        p.delta_nwc.append(delta_nwc)
        p.nopat.append(nopat)
        p.fcff.append(fcff)

        nwc_previous = nwc

    return p


def discount_factors(wacc: float, years: int, mid_year: bool = False) -> list[float]:
    """折现因子序列。中值惯例假设现金流在年中发生，折现期数减 0.5。"""
    if wacc <= 0:
        raise ValueError(f"WACC 必须为正数，收到 {wacc}")
    if years < 1:
        raise ValueError("预测期至少 1 年")
    offset = 0.5 if mid_year else 0.0
    return [1.0 / (1.0 + wacc) ** (t - offset) for t in range(1, years + 1)]


def terminal_value_perpetuity(fcff_last: float, wacc: float, growth: float) -> float:
    """永续增长法终值：TV = FCFF_n x (1 + g) / (WACC - g)。"""
    if growth >= wacc:
        raise ValueError(
            f"永续增长率 {growth:.4%} 必须小于 WACC {wacc:.4%}，否则终值不收敛"
        )
    return fcff_last * (1.0 + growth) / (wacc - growth)


def terminal_value_exit_multiple(ebitda_last: float, multiple: float) -> float:
    """退出倍数法终值：TV = EBITDA_n x 退出倍数。"""
    if multiple <= 0:
        raise ValueError(f"退出倍数必须为正数，收到 {multiple}")
    return ebitda_last * multiple


def value_dcf(
    assumptions: ForecastAssumptions,
    market: MarketData,
    wacc: float,
    base_year: int | None = None,
    *,
    fcff_series: Sequence[float] | None = None,
    ebitda_series: Sequence[float] | None = None,
) -> DCFResult:
    """主入口：给定经营假设、市场数据和折现率，返回完整估值结果。

    fcff_series / ebitda_series 是「外面已经算好的现金流」（可选）。流水线传的是
    三表预测（forecast.build_forecast）算出来的那一串 —— 折现用的现金流与报表
    出自同一处，估值模块不再自己按单值比率重算一遍：

        以前  估值模块另有一套公式，与三表预测靠回归测试盯着「两边必须相等」。
              两套公式只要有一处改动漏了另一处，报告里的现金流和估出来的价值就对不上。
        现在  估值吃三表算出的序列，一致性由来源保证，而不是靠纪律保证。

    不传时（敏感性分析、反向求解这类要反复重算的场景）仍然按假设现推 —— 那些
    场景问的就是「假设变了会怎样」，必须能跟着假设重算。
    """
    if wacc <= 0:
        raise ValueError(f"WACC 必须为正数，收到 {wacc}")

    p = project(assumptions, base_year=base_year)
    notes: list[str] = []
    if fcff_series is not None:
        values = [float(value) for value in fcff_series]
        if len(values) != assumptions.years:
            raise ValueError(
                f"传入的现金流有 {len(values)} 年，假设里是 {assumptions.years} 年，两者必须一致"
            )
        drift = max(
            (abs(left - right) for left, right in zip(values, p.fcff)), default=0.0
        )
        if drift > FCFF_RECONCILE_TOLERANCE:
            notes.append(
                f"折现用的是外部传入的现金流；本模块按假设重算的明细与它最大差 "
                f"{drift:,.2f} 元，两套口径已经开始分叉 —— "
                "报告里要说明折现取的是哪一套"
            )
        p.fcff = values
    if ebitda_series is not None:
        values = [float(value) for value in ebitda_series]
        if len(values) != assumptions.years:
            raise ValueError(
                f"传入的 EBITDA 有 {len(values)} 年，假设里是 {assumptions.years} 年，两者必须一致"
            )
        # EBITDA 只用于退出倍数法终值与「隐含退出倍数」，一并吃外部的序列，
        # 免得同一个数在产物里来自两条公式。
        p.ebitda = values

    df = discount_factors(wacc, assumptions.years, mid_year=assumptions.mid_year)
    pv_fcff = [cf * factor for cf, factor in zip(p.fcff, df)]
    pv_explicit = sum(pv_fcff)

    terminal = _terminal_value(assumptions, p, wacc)
    pv_terminal = terminal * df[-1]
    enterprise_value = pv_explicit + pv_terminal

    equity_value = (
        enterprise_value
        - market.interest_bearing_debt
        + market.cash
        - market.minority_interest
        - market.preferred_stock
        + market.non_operating_assets
    )
    per_share = equity_value / market.diluted_shares
    implied_return = per_share / market.price - 1.0
    terminal_share = pv_terminal / enterprise_value if enterprise_value else float("nan")
    # 隐含退出倍数：终值相当于多少倍 EBITDA
    implied_multiple = terminal / p.ebitda[-1] if p.ebitda[-1] else float("nan")

    diagnostics = _diagnose(
        terminal_share=terminal_share,
        implied_return=implied_return,
        implied_multiple=implied_multiple,
        equity_value=equity_value,
        terminal_value=terminal,
        terminal_method=assumptions.terminal_method,
    )
    diagnostics.extend(notes)

    return DCFResult(
        wacc=wacc,
        projection=p,
        discount_factor=df,
        pv_fcff=pv_fcff,
        terminal_value=terminal,
        pv_terminal=pv_terminal,
        enterprise_value=enterprise_value,
        net_debt=market.net_debt,
        equity_value=equity_value,
        diluted_shares=market.diluted_shares,
        per_share=per_share,
        price=market.price,
        implied_return=implied_return,
        terminal_share_of_ev=terminal_share,
        implied_exit_multiple=implied_multiple,
        diagnostics=diagnostics,
    )


def enterprise_value(assumptions: ForecastAssumptions, wacc: float) -> float:
    """只要企业价值时用这个更轻的入口。

    敏感性分析、反向求解都是在反复调用它：改一个假设、重算一次。
    """
    p = project(assumptions)
    df = discount_factors(wacc, assumptions.years, mid_year=assumptions.mid_year)
    pv_explicit = sum(cf * factor for cf, factor in zip(p.fcff, df))
    return pv_explicit + _terminal_value(assumptions, p, wacc) * df[-1]


def _terminal_value(
    assumptions: ForecastAssumptions, p: Projection, wacc: float
) -> float:
    """按假设指定的方法计算终值。"""
    if assumptions.terminal_method == "perpetuity":
        return terminal_value_perpetuity(
            p.fcff[-1], wacc, assumptions.terminal_growth
        )
    if assumptions.exit_multiple is None:
        raise ValueError("使用退出倍数法时必须提供 exit_multiple")
    return terminal_value_exit_multiple(p.ebitda[-1], assumptions.exit_multiple)


def _diagnose(
    terminal_share: float,
    implied_return: float,
    implied_multiple: float,
    equity_value: float,
    terminal_value: float,
    terminal_method: str,
) -> list[str]:
    """把估值结果的可疑之处写下来，交给报告和答辩使用。"""
    notes: list[str] = []

    if not math.isfinite(terminal_share):
        notes.append(
            "企业价值为零，终值占比无法计算；先检查预测期现金流是不是全为零"
        )
    elif terminal_share > TERMINAL_SHARE_TOO_HIGH:
        notes.append(
            f"终值占企业价值的 {terminal_share:.1%}，超过 "
            f"{TERMINAL_SHARE_TOO_HIGH:.0%}：显式预测期可能过短，"
            "正确做法是延长预测期，而不是调低增长率"
        )
    elif terminal_share < TERMINAL_SHARE_TOO_LOW:
        notes.append(
            f"终值占企业价值的 {terminal_share:.1%}，低于 "
            f"{TERMINAL_SHARE_TOO_LOW:.0%}：终值假设可能过于保守，检查是否可信"
        )

    if equity_value < 0:
        notes.append("权益价值为负：净债务超过企业价值，检查债务口径与预测假设")

    if math.isfinite(terminal_value) and terminal_value < 0:
        notes.append(
            f"终值为负（{terminal_value:,.0f}）：末年现金流或 EBITDA 为负时，"
            "永续增长法和退出倍数法都不适用，换回正常盈利年份或改用清算价值"
        )

    if abs(implied_return) < 0.05:
        notes.append(
            f"隐含回报仅 {implied_return:+.1%}，与当前股价的差距不足 5%，安全边际有限"
        )

    # 末年 EBITDA 为零或负数时这个倍数没有意义，不能印出「nanx」这种字样
    if (
        terminal_method == "perpetuity"
        and math.isfinite(implied_multiple)
        and implied_multiple > 0
    ):
        notes.append(
            f"隐含退出倍数为 {implied_multiple:.1f}x，"
            "应与公司自身历史估值和可比公司对照后再下结论"
        )
    elif terminal_method == "perpetuity":
        notes.append(
            "末年 EBITDA 为零或为负，无法反算隐含退出倍数；"
            "永续增长法在这种情形下不适用"
        )

    return notes
