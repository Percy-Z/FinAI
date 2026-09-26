"""资本成本：CAPM、债务成本、WACC。

公式
    股权成本      Re    = Rf + beta x ERP
    税后债务成本  Rd_a  = Rd x (1 - t)
    加权平均      WACC  = E/(D+E) x Re + D/(D+E) x Rd_a

三条口径纪律（做错会被评委当场问住）：
    1. 权重用市值，不用账面价值。
    2. 债务用付息债务总额，不要用净债务；现金出现在估值桥里，不进权重。
    3. 每个参数都要能说出天期、窗口和日期，写进假设台账的 source 字段。
"""

from __future__ import annotations

from .types import WaccInputs


def cost_of_equity(risk_free: float, beta: float, equity_risk_premium: float) -> float:
    """CAPM：Re = Rf + beta x ERP。"""
    return risk_free + beta * equity_risk_premium


def after_tax_cost_of_debt(cost_of_debt: float, tax_rate: float) -> float:
    """税后债务成本。利息可以税前扣除，所以乘 (1 - 税率)。"""
    return cost_of_debt * (1.0 - tax_rate)


def weights_from_market(equity_value: float, debt_value: float) -> tuple[float, float]:
    """按市值计算权益与债务权重，返回 (equity_weight, debt_weight)。

    净现金公司（debt_value=0）的债务权重为 0，此时 WACC 等于股权成本。
    """
    if equity_value < 0 or debt_value < 0:
        raise ValueError("权益市值与债务市值都不能为负")
    total = equity_value + debt_value
    if total <= 0:
        raise ValueError("权益市值与债务市值之和必须为正")
    return equity_value / total, debt_value / total


def compute_wacc(inputs: WaccInputs) -> float:
    """根据 CAPM 参数与资本结构计算 WACC。"""
    re = cost_of_equity(inputs.risk_free, inputs.beta, inputs.equity_risk_premium)
    rd = after_tax_cost_of_debt(inputs.cost_of_debt, inputs.tax_rate)
    return inputs.equity_weight * re + inputs.debt_weight * rd


def wacc_from_market(
    risk_free: float,
    beta: float,
    equity_risk_premium: float,
    cost_of_debt: float,
    tax_rate: float,
    market_cap: float,
    debt_value: float,
) -> float:
    """便捷函数：直接给权益市值与债务市值，内部算权重再算 WACC。"""
    equity_weight, debt_weight = weights_from_market(market_cap, debt_value)
    inputs = WaccInputs(
        risk_free=risk_free,
        beta=beta,
        equity_risk_premium=equity_risk_premium,
        cost_of_debt=cost_of_debt,
        tax_rate=tax_rate,
        equity_weight=equity_weight,
        debt_weight=debt_weight,
    )
    return compute_wacc(inputs)


def check_wacc_sanity(inputs: WaccInputs) -> list[str]:
    """对 WACC 参数做合理性检查，返回警告列表（空列表表示没发现问题）。

    这些检查不阻止计算，只是把可疑之处记下来，写进报告的诊断段。
    """
    warnings: list[str] = []

    total_weight = inputs.equity_weight + inputs.debt_weight
    if abs(total_weight - 1.0) > 1e-6:
        warnings.append(f"权益与债务权重之和为 {total_weight:.4f}，应为 1.0")
    if not 0.0 <= inputs.risk_free <= 0.15:
        warnings.append(f"无风险利率 {inputs.risk_free:.4%} 超出合理区间 [0, 15%]")
    if not 0.1 <= inputs.beta <= 3.0:
        warnings.append(f"beta {inputs.beta:.2f} 超出合理区间 [0.1, 3.0]")
    if not 0.02 <= inputs.equity_risk_premium <= 0.12:
        warnings.append(
            f"股权风险溢价 {inputs.equity_risk_premium:.4%} 超出合理区间 [2%, 12%]"
        )
    if inputs.cost_of_debt < inputs.risk_free:
        warnings.append(
            f"债务成本 {inputs.cost_of_debt:.4%} 低于无风险利率 "
            f"{inputs.risk_free:.4%}，检查是否用了税后利率或口径有误"
        )
    if inputs.debt_weight > 0.7:
        warnings.append(f"债务权重 {inputs.debt_weight:.1%} 偏高，检查资本结构")

    return warnings
