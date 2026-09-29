"""图表层：把结构化的估值结果渲染成图。

一个文件对应一张图，文件名就是图的编号和名字：

    football_field.py       → 11_football_field.png      估值区间
    tornado.py              → 12_tornado.png             单因素敏感性
    sensitivity_heatmap.py  → 13_sensitivity_heatmap.png 双因素敏感性
    peer_multiples.py       → 14_peer_multiples.png      可比公司倍数
    forecast_trend.py       → 15_forecast_trend.png      三表预测趋势
    validation_summary.py   → 16_validation_summary.png  勾稽校验总览

base.py 放公共样式，所有图共用一套配色和字体。

matplotlib 没装时本包仍然可以导入，只是 render_all() 返回空字典。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 只为类型检查，避免和 reporting 形成运行时的循环导入
    from algorithm.output.reporting import RunOutputs

HAS_MATPLOTLIB = importlib.util.find_spec("matplotlib") is not None

# 图表编号接在数据文件 01 到 10 之后，保证整个运行目录里编号唯一
CHART_FILES: dict[str, tuple[str, str]] = {
    "football_field": ("11", "football_field.png"),
    "tornado": ("12", "tornado.png"),
    "sensitivity": ("13", "sensitivity_heatmap.png"),
    "peer_multiples": ("14", "peer_multiples.png"),
    "forecast_trend": ("15", "forecast_trend.png"),
    "validation_summary": ("16", "validation_summary.png"),
}

def chart_filename(name: str) -> str:
    """返回带编号的图表文件名。"""
    if name not in CHART_FILES:
        raise KeyError(f"未知的图表 {name!r}，可选：{sorted(CHART_FILES)}")
    number, base = CHART_FILES[name]
    return f"{number}_{base}"


# 各图表模块都在顶部导入 matplotlib，所以只能在确认装好之后再导它们。
# render_all 在两个分支里都要有定义，否则类型检查器会说「可能未绑定」。
if HAS_MATPLOTLIB:
    from .football_field import render as render_football_field
    from .forecast_trend import render as render_forecast_trend
    from .peer_multiples import render as render_peer_multiples
    from .sensitivity_heatmap import render as render_sensitivity_heatmap
    from .tornado import render as render_tornado
    from .validation_summary import render as render_validation_summary

    def render_all(outputs: "RunOutputs", directory: str | Path) -> dict[str, Path]:
        """按 outputs 里有哪些结果渲染对应的图，返回 图名 -> 路径。"""
        target_dir = Path(directory)
        produced: dict[str, Path] = {}

        def target(name: str) -> Path:
            return target_dir / chart_filename(name)

        if outputs.football_field is not None:
            produced["football_field"] = render_football_field(
                outputs.football_field, target("football_field")
            )
        if outputs.tornado:
            price = (
                outputs.football_field.price if outputs.football_field else 0.0
            )
            produced["tornado"] = render_tornado(
                outputs.tornado, price, target("tornado")
            )
        if outputs.grid is not None:
            produced["sensitivity"] = render_sensitivity_heatmap(
                outputs.grid, target("sensitivity")
            )
        if outputs.comps is not None:
            produced["peer_multiples"] = render_peer_multiples(
                outputs.comps, target("peer_multiples")
            )
        if outputs.forecast is not None:
            produced["forecast_trend"] = render_forecast_trend(
                outputs.forecast, target("forecast_trend")
            )
        if outputs.validation:
            produced["validation_summary"] = render_validation_summary(
                outputs.validation, target("validation_summary")
            )
        return produced

else:

    def render_all(outputs: "RunOutputs", directory: str | Path) -> dict[str, Path]:
        """没装 matplotlib 时不出图，直接返回空字典。"""
        return {}


__all__ = [
    "CHART_FILES",
    "HAS_MATPLOTLIB",
    "chart_filename",
    "render_all",
]
