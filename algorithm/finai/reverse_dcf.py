"""反向 DCF：给定当前股价，反解市场隐含的假设。

正向估值算出每股价值再和股价比，只能说明贵还是便宜。
反向求解反过来问：要让每股价值正好等于现价，增长率、利润率或折现率得是多少。
得到的数字可以和公司自身历史对照，构成一个具体且可证伪的判断。

求解方法：把问题写成 f(x) = 每股价值(x) - 目标价 = 0，再用二分法在给定区间内逼近。
估值对这几个参数都是单调的，二分法稳定收敛，而且不需要装 scipy。
"""

from __future__ import annotations

from dataclasses import dataclass

from .dcf import value_dcf
from .sensitivity import Variable, apply_variable
from .types import ForecastAssumptions, MarketData

# 各字段的默认搜索区间。求解前会裁剪到数学上合法的范围内。
DEFAULT_BOUNDS: dict[str, tuple[float, float]] = {
    "wacc": (0.01, 0.40),
    # 下界允许到 -10%：股价很低时，市场隐含的可能是永续衰退，
    # 求解器要能把这个结论说出来，而不是直接报「无解」
    "terminal_growth": (-0.10, 0.06),
    "revenue_growth": (-0.30, 0.50),
    "ebit_margin": (0.0, 0.80),
    "tax_rate": (0.0, 0.60),
    "da_pct_revenue": (0.0, 0.30),
    "capex_pct_revenue": (0.0, 0.30),
    "nwc_pct_revenue": (-0.20, 0.60),
    "exit_multiple": (3.0, 30.0),
}


@dataclass
class ReverseResult:
    """反向求解的结果。"""

    name: str
    field: str
    base_value: float
    implied_value: float
    target_per_share: float
    achieved_per_share: float
    iterations: int
    lower_bound: float
    upper_bound: float

    @property
    def direction(self) -> str:
        """市场隐含的假设相对基准是更高还是更低。"""
        if abs(self.implied_value - self.base_value) < 1e-12:
            return "same"
        return "higher" if self.implied_value > self.base_value else "lower"

    @property
    def residual(self) -> float:
        """解出来之后，每股价值与目标价还差多少。"""
        return self.achieved_per_share - self.target_per_share

    def describe(self) -> str:
        gap = self.implied_value - self.base_value
        return (
            f"{self.name}：市场隐含 {self.implied_value:.4f}，"
            f"基准 {self.base_value:.4f}，差 {gap:+.4f}"
        )


def effective_bounds(
    variable: Variable,
    wacc: float,
    bounds: tuple[float, float] | None = None,
    growth_floor: float | None = None,
) -> tuple[float, float]:
    """确定搜索区间，并裁剪到数学上合法的范围。

    两处必须裁剪：
        1. 永续增长率必须小于 WACC，否则终值不收敛；
        2. WACC 必须为正，而且在永续增长法下必须高于永续增长率，
           否则终值不收敛。growth_floor 就是用来传这个约束的。
    """
    if bounds is None:
        bounds = DEFAULT_BOUNDS.get(variable.field)
    if bounds is None:
        span = max(
            abs(variable.high - variable.low), abs(variable.base) * 0.5, 1e-6
        )
        bounds = (variable.base - span * 3, variable.base + span * 3)

    lower, upper = bounds

    if variable.field == "terminal_growth":
        upper = min(upper, wacc - 1e-6)
    if variable.field == "wacc":
        lower = max(lower, 1e-4)
        if growth_floor is not None:
            lower = max(lower, growth_floor + 1e-6)
        upper = max(upper, lower + 1e-4)

    if lower >= upper:
        raise ValueError(
            f"{variable.name}：裁剪后的搜索区间无效 [{lower}, {upper}]，"
            "请显式传入 bounds"
        )
    return lower, upper


def reverse_solve(
    assumptions: ForecastAssumptions,
    market: MarketData,
    wacc: float,
    variable: Variable,
    target_per_share: float | None = None,
    bounds: tuple[float, float] | None = None,
    tolerance: float = 1e-9,
    max_iter: int = 200,
) -> ReverseResult:
    """反解出让每股价值等于目标价的参数值。

    target_per_share 省略时用当前股价，也就是标准的反向 DCF。
    """
    target = market.price if target_per_share is None else float(target_per_share)
    # 永续增长法下，折现率必须高于永续增长率，否则终值不收敛
    growth_floor = (
        assumptions.terminal_growth
        if assumptions.terminal_method == "perpetuity"
        else None
    )
    lower, upper = effective_bounds(
        variable, wacc, bounds, growth_floor=growth_floor
    )

    def objective(value: float) -> float:
        modified, modified_wacc = apply_variable(assumptions, wacc, variable, value)
        return value_dcf(modified, market, modified_wacc).per_share - target

    f_lower = objective(lower)
    f_upper = objective(upper)

    if f_lower * f_upper > 0:
        raise ValueError(
            f"{variable.name}：在区间 [{lower:.4f}, {upper:.4f}] 内无法达到目标价 "
            f"{target:.4f}。区间两端的每股价值分别为 "
            f"{f_lower + target:.4f} 和 {f_upper + target:.4f}。"
            "请扩大 bounds 或检查假设。"
        )

    a, b = lower, upper
    fa = f_lower
    iterations = 0
    for iterations in range(1, max_iter + 1):
        mid = (a + b) / 2.0
        fm = objective(mid)
        # tolerance 衡量的是「每股价值与目标价还差多少」，不是区间宽度。
        # 区间退到浮点精度极限时也必须停下，否则会一直空转。
        if abs(fm) < tolerance or b - a < 1e-14:
            return _build_result(
                variable, mid, target, fm, iterations, lower, upper
            )
        if fa * fm < 0:
            b = mid
        else:
            a, fa = mid, fm

    mid = (a + b) / 2.0
    return _build_result(
        variable, mid, target, objective(mid), iterations, lower, upper
    )


def _build_result(
    variable: Variable,
    implied: float,
    target: float,
    residual: float,
    iterations: int,
    lower: float,
    upper: float,
) -> ReverseResult:
    return ReverseResult(
        name=variable.name,
        field=variable.field,
        base_value=variable.base,
        implied_value=implied,
        target_per_share=target,
        achieved_per_share=target + residual,
        iterations=iterations,
        lower_bound=lower,
        upper_bound=upper,
    )


def reverse_solve_many(
    assumptions: ForecastAssumptions,
    market: MarketData,
    wacc: float,
    variables: list[Variable],
    target_per_share: float | None = None,
) -> list[ReverseResult]:
    """一次反解多个参数。每个参数单独求解，其余保持基准值。"""
    return [
        reverse_solve(
            assumptions,
            market,
            wacc,
            variable,
            target_per_share=target_per_share,
        )
        for variable in variables
    ]
