"""财务报表的数据结构。

按年报披露的原始结构建模：利润表与资产负债表各披露本期和上期两列，
现金流量表给出期初与期末现金。这样的结构让勾稽检查有据可依，
不需要把「上期数」单独塞进某个字段。

金额单位一律为元，与 finai.types 的约定一致。
缺失的数据用 None 表示，检查函数遇到 None 会返回 skipped 而不是猜。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BalanceSheet:
    """资产负债表。期末数放这里，年初数用 ReportSnapshot.balance_begin 传。"""

    total_assets: float
    total_liabilities: float
    total_equity: float
    equity_attributable: float | None = None  # 归属于母公司所有者权益
    minority_equity: float | None = None
    retained_earnings: float | None = None  # 未分配利润
    cash_and_equivalents: float | None = None  # 货币资金
    accounts_receivable: float | None = None  # 应收账款（含应收票据时可合并）
    inventory: float | None = None


@dataclass(frozen=True)
class IncomeStatement:
    """利润表。

    dividends_declared 与 reserve_appropriation 属于利润分配事项，年报通常与
    利润表同处披露，因此放在这里供未分配利润滚动检查使用。
    """

    revenue: float
    net_profit: float
    net_profit_attributable: float | None = None  # 归属于母公司股东的净利润
    operating_profit: float | None = None
    income_tax: float | None = None
    interest_expense: float | None = None
    non_recurring_gains: float | None = None  # 非经常性损益（税后）
    dividends_declared: float | None = None  # 宣告分配的现金股利
    reserve_appropriation: float | None = None  # 提取盈余公积


@dataclass(frozen=True)
class CashFlowStatement:
    """现金流量表。三段净额都必须为正负号齐全的净额。"""

    operating: float
    investing: float
    financing: float
    cash_begin: float | None = None  # 期初现金及现金等价物余额
    cash_end: float | None = None  # 期末现金及现金等价物余额
    fx_effect: float = 0.0  # 汇率变动对现金的影响


@dataclass(frozen=True)
class ReportSnapshot:
    """一份年报所披露的当期与上期数据。

    balance_begin 对应资产负债表的上期期末列，
    income_prior 对应利润表的上期金额列，
    cashflow 里的 cash_begin 对应现金流量表的期初余额。
    """

    fiscal_year: int
    balance: BalanceSheet
    income: IncomeStatement
    cashflow: CashFlowStatement
    balance_begin: BalanceSheet | None = None
    income_prior: IncomeStatement | None = None
    source: str = ""  # 年报文件名，用于报告里标注证据出处
