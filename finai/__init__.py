"""FinAI 估值引擎。

目前包含：
    finai.wacc  资本成本（CAPM、债务成本、WACC）
    finai.dcf   现金流折现（FCFF 预测、终值、估值桥）

后续模块：敏感性分析、反向求解、勾稽校验、文档抽取。
"""

from .dcf import Projection, enterprise_value, project, value_dcf
from .types import DCFResult, ForecastAssumptions, MarketData, WaccInputs
from .wacc import (
    after_tax_cost_of_debt,
    check_wacc_sanity,
    compute_wacc,
    cost_of_equity,
    wacc_from_market,
    weights_from_market,
)

__all__ = [
    "DCFResult",
    "ForecastAssumptions",
    "MarketData",
    "Projection",
    "WaccInputs",
    "after_tax_cost_of_debt",
    "check_wacc_sanity",
    "compute_wacc",
    "cost_of_equity",
    "enterprise_value",
    "project",
    "value_dcf",
    "wacc_from_market",
    "weights_from_market",
]
