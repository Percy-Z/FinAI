"""FinAI 估值引擎。

目前包含：
    finai.wacc  资本成本（CAPM、债务成本、WACC）
    finai.dcf   现金流折现（FCFF 预测、终值、估值桥）

后续模块：敏感性分析、反向求解、勾稽校验、文档抽取。
"""

from .dcf import Projection, enterprise_value, project, value_dcf
from .reverse_dcf import (
    ReverseResult,
    effective_bounds,
    reverse_solve,
    reverse_solve_many,
)
from .sensitivity import (
    AttributionRow,
    GridResult,
    TornadoRow,
    Variable,
    apply_variable,
    attribute,
    tornado,
    two_way_grid,
    write_grid_csv,
    write_tornado_csv,
)
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
    "AttributionRow",
    "DCFResult",
    "ForecastAssumptions",
    "GridResult",
    "MarketData",
    "Projection",
    "ReverseResult",
    "TornadoRow",
    "Variable",
    "WaccInputs",
    "after_tax_cost_of_debt",
    "apply_variable",
    "attribute",
    "check_wacc_sanity",
    "compute_wacc",
    "cost_of_equity",
    "effective_bounds",
    "enterprise_value",
    "project",
    "reverse_solve",
    "reverse_solve_many",
    "tornado",
    "two_way_grid",
    "value_dcf",
    "wacc_from_market",
    "weights_from_market",
    "write_grid_csv",
    "write_tornado_csv",
]
