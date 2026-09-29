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

import math
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    # 只为类型检查器导入。Projection 定义在 dcf.py，而 dcf.py 又要导入本模块，
    # 运行时不导入才不会形成循环导入。
    from .dcf import Projection


def require_finite(value: float | None, label: str) -> float:
    """校验数字是有限值。

    必须单独写这一步，因为 NaN 参与任何比较都是 False：
    `if x <= 0` 拦不住 NaN，`if x > 0` 也拦不住。缺失值从年报抽出来时
    常常是 NaN，所以每个数字入模型前都要过这一关。
    """
    if value is None:
        raise ValueError(f"{label} 不能为空")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} 必须是有限数字，收到 {value!r}")
    return number


def require_positive(value: float | None, label: str) -> float:
    """校验数字是有限的正数。"""
    number = require_finite(value, label)
    if number <= 0:
        raise ValueError(f"{label} 必须为正数，收到 {number}")
    return number


def require_optional_finite(value: float | None, label: str) -> float | None:
    """校验可空数字：要么是 None，要么是有限值。"""
    if value is None:
        return None
    return require_finite(value, label)


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

    def __post_init__(self) -> None:
        for name in (
            "risk_free",
            "beta",
            "equity_risk_premium",
            "cost_of_debt",
            "tax_rate",
            "equity_weight",
            "debt_weight",
        ):
            require_finite(getattr(self, name), name)


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
            self,
            "revenue_growth",
            tuple(require_finite(x, "revenue_growth 的每一项") for x in self.revenue_growth),
        )
        object.__setattr__(
            self,
            "ebit_margin",
            tuple(require_finite(x, "ebit_margin 的每一项") for x in self.ebit_margin),
        )

        require_positive(self.revenue_base, "revenue_base")
        for name in ("da_pct_revenue", "capex_pct_revenue", "nwc_pct_revenue"):
            require_finite(getattr(self, name), name)
        require_finite(self.terminal_growth, "terminal_growth")
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
        if self.exit_multiple is not None:
            require_positive(self.exit_multiple, "exit_multiple")

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
        require_positive(self.price, "price")
        require_positive(self.diluted_shares, "diluted_shares")
        for name in (
            "interest_bearing_debt",
            "cash",
            "minority_interest",
            "preferred_stock",
            "non_operating_assets",
        ):
            require_finite(getattr(self, name), name)

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
    projection: Projection
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
