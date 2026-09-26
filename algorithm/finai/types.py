"""引擎的输入输出契约。

单位约定（全项目统一，中途不要换）：
    金额    元
    股数    股
    比率    小数，0.12 表示 12%

所有假设类都是不可变（frozen）的，目的是让「改一个假设再算一遍」变成
dataclasses.replace(...)，而不是就地修改对象。这是敏感性分析和反向求解
能够复用同一个估值函数的前提。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Sequence


@dataclass(frozen=True)
class WaccInputs:
    """资本成本的输入参数。每个字段都应该能追溯到来源和日期。"""

    risk_free: float
    beta: float
    equity_risk_premium: float
    cost_of_debt: float
    tax_rate: float
    equity_weight: float
    debt_weight: float


@dataclass(frozen=True)
class ForecastAssumptions:
    """经营假设：驱动未来现金流的那一组数字。

    预测期长度由 revenue_growth 的长度决定，ebit_margin 必须等长。
    折旧、资本开支、营运资本都用「占当年收入的比例」表示。
    """

    revenue_base: float
    revenue_growth: Sequence[float]
    ebit_margin: Sequence[float]
    tax_rate: float
    da_pct_revenue: float
    capex_pct_revenue: float
    nwc_pct_revenue: float
    terminal_method: str = "perpetuity"
    terminal_growth: float = 0.0
    exit_multiple: float | None = None
    mid_year: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "revenue_growth", tuple(float(x) for x in self.revenue_growth)
        )
        object.__setattr__(
            self, "ebit_margin", tuple(float(x) for x in self.ebit_margin)
        )

        if self.revenue_base <= 0:
            raise ValueError("revenue_base 必须为正数（单位：元）")
        if len(self.revenue_growth) == 0:
            raise ValueError("revenue_growth 不能为空，预测期至少 1 年")
        if len(self.ebit_margin) != len(self.revenue_growth):
            raise ValueError(
                f"ebit_margin 的长度（{len(self.ebit_margin)}）"
                f"必须与 revenue_growth（{len(self.revenue_growth)}）一致"
            )
        if not 0.0 <= self.tax_rate <= 0.6:
            raise ValueError(f"tax_rate={self.tax_rate} 超出合理区间 [0, 0.6]")
        if self.terminal_method not in ("perpetuity", "exit_multiple"):
            raise ValueError(
                "terminal_method 只能是 'perpetuity' 或 'exit_multiple'，"
                f"收到 {self.terminal_method!r}"
            )
        if self.terminal_method == "exit_multiple" and self.exit_multiple is None:
            raise ValueError("使用退出倍数法时必须提供 exit_multiple")

    @property
    def years(self) -> int:
        """显式预测期长度。"""
        return len(self.revenue_growth)


@dataclass(frozen=True)
class MarketData:
    """估值桥所需的负债表与市场数据。

    注意：interest_bearing_debt 是付息债务总额，不是净债务；
    diluted_shares 是摊薄股数，不是基本股数。
    """

    price: float
    diluted_shares: float
    interest_bearing_debt: float
    cash: float
    minority_interest: float = 0.0
    preferred_stock: float = 0.0
    non_operating_assets: float = 0.0
    as_of: str = ""

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError("price 必须为正数")
        if self.diluted_shares <= 0:
            raise ValueError("diluted_shares 必须为正数")

    @property
    def net_debt(self) -> float:
        """净债务 = 付息债务 - 现金。为负说明是净现金公司。"""
        return self.interest_bearing_debt - self.cash

    @property
    def market_cap(self) -> float:
        return self.price * self.diluted_shares


@dataclass
class DCFResult:
    """估值结果。字段名与 outputs/<run_id>/valuation.json 保持一致。"""

    wacc: float
    projection: "Projection"
    discount_factor: list[float]
    pv_fcff: list[float]
    terminal_value: float
    pv_terminal: float
    enterprise_value: float
    net_debt: float
    equity_value: float
    diluted_shares: float
    per_share: float
    price: float
    implied_return: float
    terminal_share_of_ev: float
    implied_exit_multiple: float
    diagnostics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典，供 valuation.json 与报告使用。"""
        data = asdict(self)
        data["projection"] = asdict(self.projection)
        return data
