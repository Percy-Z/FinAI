"""三表联动预测。

和 finai.dcf 的区别：dcf 直接从假设跳到自由现金流，中间没有资产负债表，
预测出来的利润表也不需要自己平。这个模块把三张表完整地串起来，
每一步都有对应的会计科目，所以可以用 finai.validate 反过来检查预测结果。

联动顺序（每年重复一次）
    1. 收入      = 上期收入 x (1 + 增长率)
    2. 毛利      = 收入 x 毛利率
    3. 期间费用  = 收入 x 费用率（不含折旧摊销）
    4. 折旧摊销  = 收入 x 折旧摊销率
    5. EBIT      = 毛利 - 期间费用 - 折旧摊销
    6. 利息费用  = 期初有息负债 x 利率
    7. 税前利润  = EBIT - 利息费用
    8. 所得税    = 税前利润 x 税率
    9. 净利润    = 税前利润 - 所得税，再拆成归母与少数股东
   10. 资产负债表：应收、存货、应付按收入比例；固定资产按「期初 + 资本开支 - 折旧」滚动；
       有息负债按还款计划滚动；留存收益按「期初 + 归母净利润 - 分红」滚动
   11. 现金流量表：经营现金流用间接法（净利润 + 折旧 - 营运资本增加）；
       投资现金流 = -资本开支；筹资现金流 = 债务变动 - 分红
   12. 现金 = 期初现金 + 经营 + 投资 + 筹资，回填到资产负债表

为什么现金要最后算
    现金是模型的「塞子」。其他科目都由假设驱动，现金由现金流量表推出来，
    这样资产负债表自动平衡，不需要事后调平。

平衡的充要条件
    资产 - 负债 - 权益的变动量 = 其他非流动资产变动 - 其他非流动负债变动。
    这两项在本模型里保持期初水平不变，所以差额恒为零。
    如果以后要让它们随收入增长，两边必须同幅增长，否则表会不平。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .statements import (
    BalanceSheet,
    CashFlowStatement,
    IncomeStatement,
    ReportSnapshot,
)


def _as_tuple(value: Sequence[float] | float, years: int, name: str) -> tuple[float, ...]:
    """把标量广播成序列，或者校验序列长度。"""
    if isinstance(value, (int, float)):
        return tuple([float(value)] * years)
    values = tuple(float(item) for item in value)
    if len(values) != years:
        raise ValueError(f"{name} 的长度为 {len(values)}，应为 {years}")
    return values


@dataclass(frozen=True)
class BalanceSheetState:
    """某一期末的资产负债表科目。金额单位为元。"""

    cash: float
    accounts_receivable: float
    inventory: float
    fixed_assets: float
    other_non_current_assets: float
    accounts_payable: float
    interest_bearing_debt: float
    other_non_current_liabilities: float
    share_capital: float
    retained_earnings: float
    minority_equity: float = 0.0

    @property
    def current_assets(self) -> float:
        return self.cash + self.accounts_receivable + self.inventory

    @property
    def total_assets(self) -> float:
        return self.current_assets + self.fixed_assets + self.other_non_current_assets

    @property
    def total_liabilities(self) -> float:
        return (
            self.accounts_payable
            + self.interest_bearing_debt
            + self.other_non_current_liabilities
        )

    @property
    def total_equity(self) -> float:
        return self.share_capital + self.retained_earnings + self.minority_equity

    @property
    def equity_attributable(self) -> float:
        return self.share_capital + self.retained_earnings

    @property
    def net_debt(self) -> float:
        return self.interest_bearing_debt - self.cash

    @property
    def working_capital(self) -> float:
        """经营性营运资本 = 应收 + 存货 - 应付。"""
        return self.accounts_receivable + self.inventory - self.accounts_payable

    @property
    def balance_gap(self) -> float:
        """资产 - 负债 - 权益。正常应该恒为 0。"""
        return self.total_assets - self.total_liabilities - self.total_equity


@dataclass(frozen=True)
class IncomeStatementState:
    revenue: float
    cost_of_sales: float
    gross_profit: float
    operating_expenses: float
    da: float
    ebitda: float
    ebit: float
    interest_expense: float
    pretax_profit: float
    income_tax: float
    net_profit: float
    minority_profit: float
    net_profit_attributable: float


@dataclass(frozen=True)
class CashFlowState:
    net_profit: float
    da: float
    delta_receivable: float
    delta_inventory: float
    delta_payable: float
    operating: float
    capex: float
    investing: float
    debt_change: float
    dividends: float
    financing: float
    net_change: float
    cash_begin: float
    cash_end: float

    @property
    def delta_nwc(self) -> float:
        """营运资本增加额。正数表示占用现金。"""
        return self.delta_receivable + self.delta_inventory - self.delta_payable


@dataclass(frozen=True)
class ForecastDrivers:
    """三表预测的驱动假设。

    预测期由 years 显式指定，因为假设全用标量时无法从数据推断长度。
    任何序列字段的长度都必须等于 years，标量会自动广播到每一年。
    """

    revenue_base: float
    base_year: int
    years: int
    revenue_growth: Sequence[float] | float
    gross_margin: Sequence[float] | float
    opex_pct_revenue: Sequence[float] | float
    da_pct_revenue: Sequence[float] | float
    capex_pct_revenue: Sequence[float] | float
    ar_pct_revenue: Sequence[float] | float
    inventory_pct_revenue: Sequence[float] | float
    ap_pct_revenue: Sequence[float] | float
    interest_rate_on_debt: float
    tax_rate: float
    dividend_payout_ratio: float
    net_debt_repayment: Sequence[float] | float = 0.0
    minority_share_of_profit: float = 0.0

    def __post_init__(self) -> None:
        if self.years < 1:
            raise ValueError("预测期至少 1 年")
        if self.revenue_base <= 0:
            raise ValueError("revenue_base 必须为正数")
        if not 0.0 <= self.tax_rate <= 0.6:
            raise ValueError(f"tax_rate={self.tax_rate} 超出合理区间")
        if not 0.0 <= self.dividend_payout_ratio <= 1.0:
            raise ValueError("dividend_payout_ratio 必须落在 [0, 1]")
        if not 0.0 <= self.minority_share_of_profit < 1.0:
            raise ValueError("minority_share_of_profit 必须落在 [0, 1)")
        if self.interest_rate_on_debt < 0:
            raise ValueError("interest_rate_on_debt 不能为负")

        for name in (
            "revenue_growth",
            "gross_margin",
            "opex_pct_revenue",
            "da_pct_revenue",
            "capex_pct_revenue",
            "ar_pct_revenue",
            "inventory_pct_revenue",
            "ap_pct_revenue",
            "net_debt_repayment",
        ):
            object.__setattr__(
                self, name, _as_tuple(getattr(self, name), self.years, name)
            )


@dataclass(frozen=True)
class ForecastYear:
    year: int
    tax_rate: float
    income: IncomeStatementState
    balance: BalanceSheetState
    cashflow: CashFlowState

    @property
    def fcff(self) -> float:
        """企业自由现金流 = EBIT x (1 - 税率) + 折旧摊销 - 资本开支 - 营运资本增加。

        用的是驱动假设里的边际税率，与利润表口径一致。
        """
        nopat = self.income.ebit * (1.0 - self.tax_rate)
        return (
            nopat
            + self.income.da
            - self.cashflow.capex
            - self.cashflow.delta_nwc
        )


@dataclass
class ThreeStatementForecast:
    opening: BalanceSheetState
    drivers: ForecastDrivers
    years: list[ForecastYear] = field(default_factory=list)
    source: str = ""

    @property
    def balance_gaps(self) -> list[float]:
        """每年「资产 - 负债 - 权益」的差额。"""
        return [year.balance.balance_gap for year in self.years]

    @property
    def worst_balance_gap(self) -> float:
        gaps = [abs(gap) for gap in self.balance_gaps]
        gaps.append(abs(self.opening.balance_gap))
        return max(gaps)

    @property
    def fcff_series(self) -> list[float]:
        return [year.fcff for year in self.years]

    def year(self, fiscal_year: int) -> ForecastYear:
        for item in self.years:
            if item.year == fiscal_year:
                return item
        raise KeyError(f"预测期内没有 {fiscal_year} 年")

    def to_records(self) -> list[dict]:
        """展开成逐年记录，供 projections.json 使用。"""
        records: list[dict] = []
        for item in self.years:
            records.append(
                {
                    "year": item.year,
                    "income": {
                        "revenue": item.income.revenue,
                        "cost_of_sales": item.income.cost_of_sales,
                        "gross_profit": item.income.gross_profit,
                        "operating_expenses": item.income.operating_expenses,
                        "da": item.income.da,
                        "ebitda": item.income.ebitda,
                        "ebit": item.income.ebit,
                        "interest_expense": item.income.interest_expense,
                        "pretax_profit": item.income.pretax_profit,
                        "income_tax": item.income.income_tax,
                        "net_profit": item.income.net_profit,
                        "net_profit_attributable": item.income.net_profit_attributable,
                    },
                    "balance": {
                        "cash": item.balance.cash,
                        "accounts_receivable": item.balance.accounts_receivable,
                        "inventory": item.balance.inventory,
                        "fixed_assets": item.balance.fixed_assets,
                        "other_non_current_assets": item.balance.other_non_current_assets,
                        "accounts_payable": item.balance.accounts_payable,
                        "interest_bearing_debt": item.balance.interest_bearing_debt,
                        "other_non_current_liabilities": item.balance.other_non_current_liabilities,
                        "share_capital": item.balance.share_capital,
                        "retained_earnings": item.balance.retained_earnings,
                        "minority_equity": item.balance.minority_equity,
                        "total_assets": item.balance.total_assets,
                        "total_liabilities": item.balance.total_liabilities,
                        "total_equity": item.balance.total_equity,
                        "balance_gap": item.balance.balance_gap,
                    },
                    "cashflow": {
                        "operating": item.cashflow.operating,
                        "investing": item.cashflow.investing,
                        "financing": item.cashflow.financing,
                        "net_change": item.cashflow.net_change,
                        "cash_begin": item.cashflow.cash_begin,
                        "cash_end": item.cashflow.cash_end,
                        "capex": item.cashflow.capex,
                        "dividends": item.cashflow.dividends,
                        "delta_nwc": item.cashflow.delta_nwc,
                    },
                    "fcff": item.fcff,
                }
            )
        return records


def build_forecast(
    opening: BalanceSheetState,
    drivers: ForecastDrivers,
    source: str = "forecast",
) -> ThreeStatementForecast:
    """按驱动假设推出一套完整的三表预测。"""
    forecast = ThreeStatementForecast(
        opening=opening, drivers=drivers, source=source
    )

    tolerance = 1e-6
    if abs(opening.balance_gap) > tolerance:
        raise ValueError(
            f"基期资产负债表本身不平，差额 {opening.balance_gap:,.2f}，"
            "先修正基期数据再预测"
        )

    previous_balance = opening
    revenue = drivers.revenue_base

    for index in range(drivers.years):
        year_number = drivers.base_year + index + 1
        revenue = revenue * (1.0 + drivers.revenue_growth[index])

        # 利润表
        gross_profit = revenue * drivers.gross_margin[index]
        cost_of_sales = revenue - gross_profit
        operating_expenses = revenue * drivers.opex_pct_revenue[index]
        da = revenue * drivers.da_pct_revenue[index]
        ebitda = gross_profit - operating_expenses
        ebit = ebitda - da

        # 利息按期初债务计算，避免与利润表形成循环引用
        interest_expense = previous_balance.interest_bearing_debt * (
            drivers.interest_rate_on_debt
        )
        pretax_profit = ebit - interest_expense
        income_tax = pretax_profit * drivers.tax_rate
        net_profit = pretax_profit - income_tax
        minority_profit = net_profit * drivers.minority_share_of_profit
        net_profit_attributable = net_profit - minority_profit

        income = IncomeStatementState(
            revenue=revenue,
            cost_of_sales=cost_of_sales,
            gross_profit=gross_profit,
            operating_expenses=operating_expenses,
            da=da,
            ebitda=ebitda,
            ebit=ebit,
            interest_expense=interest_expense,
            pretax_profit=pretax_profit,
            income_tax=income_tax,
            net_profit=net_profit,
            minority_profit=minority_profit,
            net_profit_attributable=net_profit_attributable,
        )

        # 资产负债表里由假设驱动的科目
        accounts_receivable = revenue * drivers.ar_pct_revenue[index]
        inventory = revenue * drivers.inventory_pct_revenue[index]
        accounts_payable = revenue * drivers.ap_pct_revenue[index]
        fixed_assets = (
            previous_balance.fixed_assets
            + revenue * drivers.capex_pct_revenue[index]
            - da
        )
        interest_bearing_debt = (
            previous_balance.interest_bearing_debt
            - drivers.net_debt_repayment[index]
        )
        dividends = net_profit_attributable * drivers.dividend_payout_ratio
        retained_earnings = (
            previous_balance.retained_earnings
            + net_profit_attributable
            - dividends
        )
        minority_equity = previous_balance.minority_equity + minority_profit

        # 现金流量表（间接法）
        delta_receivable = accounts_receivable - previous_balance.accounts_receivable
        delta_inventory = inventory - previous_balance.inventory
        delta_payable = accounts_payable - previous_balance.accounts_payable
        operating = (
            net_profit + da - delta_receivable - delta_inventory + delta_payable
        )
        capex = revenue * drivers.capex_pct_revenue[index]
        investing = -capex
        debt_change = interest_bearing_debt - previous_balance.interest_bearing_debt
        financing = debt_change - dividends
        net_change = operating + investing + financing
        cash_end = previous_balance.cash + net_change

        cashflow = CashFlowState(
            net_profit=net_profit,
            da=da,
            delta_receivable=delta_receivable,
            delta_inventory=delta_inventory,
            delta_payable=delta_payable,
            operating=operating,
            capex=capex,
            investing=investing,
            debt_change=debt_change,
            dividends=dividends,
            financing=financing,
            net_change=net_change,
            cash_begin=previous_balance.cash,
            cash_end=cash_end,
        )

        balance = BalanceSheetState(
            cash=cash_end,
            accounts_receivable=accounts_receivable,
            inventory=inventory,
            fixed_assets=fixed_assets,
            other_non_current_assets=previous_balance.other_non_current_assets,
            accounts_payable=accounts_payable,
            interest_bearing_debt=interest_bearing_debt,
            other_non_current_liabilities=(
                previous_balance.other_non_current_liabilities
            ),
            share_capital=previous_balance.share_capital,
            retained_earnings=retained_earnings,
            minority_equity=minority_equity,
        )

        forecast.years.append(
            ForecastYear(
                year=year_number,
                tax_rate=drivers.tax_rate,
                income=income,
                balance=balance,
                cashflow=cashflow,
            )
        )

        previous_balance = balance

    return forecast


def to_summary_balance(state: BalanceSheetState) -> BalanceSheet:
    """转成 finai.validate 用的摘要资产负债表。"""
    return BalanceSheet(
        total_assets=state.total_assets,
        total_liabilities=state.total_liabilities,
        total_equity=state.total_equity,
        equity_attributable=state.equity_attributable,
        minority_equity=state.minority_equity,
        retained_earnings=state.retained_earnings,
        cash_and_equivalents=state.cash,
        accounts_receivable=state.accounts_receivable,
        inventory=state.inventory,
    )


def to_summary_income(
    state: IncomeStatementState, dividends: float | None = None
) -> IncomeStatement:
    """转成 finai.validate 用的摘要利润表。

    dividends 必须传进来，否则未分配利润滚动检查看不到分红，
    会把「期初 + 归母净利润」和实际留存收益的差额误判成数据问题。
    本模型不单独建模提取盈余公积，所以显式写 0。
    """
    return IncomeStatement(
        revenue=state.revenue,
        net_profit=state.net_profit,
        net_profit_attributable=state.net_profit_attributable,
        operating_profit=state.ebit,
        income_tax=state.income_tax,
        interest_expense=state.interest_expense,
        dividends_declared=dividends,
        reserve_appropriation=0.0,
    )


def to_summary_cashflow(state: CashFlowState) -> CashFlowStatement:
    return CashFlowStatement(
        operating=state.operating,
        investing=state.investing,
        financing=state.financing,
        cash_begin=state.cash_begin,
        cash_end=state.cash_end,
    )


def to_snapshots(forecast: ThreeStatementForecast) -> list[ReportSnapshot]:
    """把预测结果转成 finai.validate 能直接吃的快照。

    第 0 年（基期）没有完整的利润表，只提供营业收入，
    用于给第一年的应收账款背离检查提供上期基数。
    """
    snapshots: list[ReportSnapshot] = []
    previous_state = forecast.opening
    previous_income = IncomeStatement(
        revenue=forecast.drivers.revenue_base,
        net_profit=0.0,  # 基期利润表不在预测范围内，这里只用收入
    )

    for year in forecast.years:
        snapshots.append(
            ReportSnapshot(
                fiscal_year=year.year,
                balance=to_summary_balance(year.balance),
                income=to_summary_income(year.income, year.cashflow.dividends),
                cashflow=to_summary_cashflow(year.cashflow),
                balance_begin=to_summary_balance(previous_state),
                income_prior=previous_income,
                source=forecast.source,
            )
        )
        previous_state = year.balance
        previous_income = to_summary_income(year.income, year.cashflow.dividends)

    return snapshots
