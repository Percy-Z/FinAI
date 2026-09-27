"""图表层的公共样式。

所有图表都从这里取配色、字体和保存方法，保证十几张图看起来是一套东西。
样式规则：白底、去掉上右边框、只在数值轴画浅网格、重要数值直接标在图上、
参考线用统一的红色虚线。
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

DPI = 180

COLORS = {
    "ink": "#1b2c42",  # 正文与坐标轴文字
    "muted": "#6b7785",  # 次要标注
    "grid": "#dfe3e8",  # 网格与轴线
    "primary": "#2f6b45",  # 主色（深绿）
    "primary_fill": "#cfe3d5",
    "accent": "#b5442f",  # 参考线（现价）
    "accent_fill": "#e8c9c0",
    "warning": "#a86a2a",
    "warning_fill": "#f6e3c8",
    "danger": "#a33a2a",
    "danger_fill": "#f0cfc9",
    "neutral_fill": "#e8ebef",
    "series": ["#2f6b45", "#3f5f85", "#8a6d1f", "#5d4786", "#a86a2a", "#b5442f"],
}

CJK_FONT_CANDIDATES = (
    "PingFang SC",
    "Hiragino Sans GB",
    "Heiti SC",
    "STHeiti",
    "Arial Unicode MS",
    "Microsoft YaHei",
    "SimHei",
)

_font_ready = False
_font_name: str | None = None


def setup_font() -> str | None:
    """挑一个可用的中文字体，只做一次。返回选中的字体名，没有则返回 None。"""
    global _font_ready, _font_name
    if _font_ready:
        return _font_name

    available = {font.name for font in font_manager.fontManager.ttflist}
    for candidate in CJK_FONT_CANDIDATES:
        if candidate in available:
            plt.rcParams["font.family"] = candidate
            _font_name = candidate
            break

    plt.rcParams["axes.unicode_minus"] = False
    _font_ready = True
    return _font_name


def new_figure(width: float, height: float, panels: int = 1):
    """新建一张图，返回 (figure, axes)。

    panels 为 1 时 axes 是单个坐标轴，大于 1 时是坐标轴数组。
    """
    setup_font()
    if panels <= 1:
        figure, axes = plt.subplots(figsize=(width, height))
    else:
        figure, axes = plt.subplots(1, panels, figsize=(width, height))
    return figure, axes


def style_axes(axes, grid_axis: str | None = "x") -> None:
    """统一坐标轴样式：去掉上右边框，加浅网格，文字用墨色。"""
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axes.spines[side].set_color(COLORS["grid"])

    if grid_axis:
        axes.grid(axis=grid_axis, linestyle=":", color=COLORS["grid"], linewidth=0.8)
        axes.set_axisbelow(True)

    axes.tick_params(colors=COLORS["muted"], labelsize=10)
    for label in list(axes.get_xticklabels()) + list(axes.get_yticklabels()):
        label.set_color(COLORS["ink"])


def add_price_line(axes, price: float, label: str | None = None) -> None:
    """画现价参考线，全项目统一红色虚线。

    label 传字符串才会进图例；传 None 表示只在图上画线，由调用方自己标文字。
    """
    axes.axvline(
        price,
        color=COLORS["accent"],
        linestyle="--",
        linewidth=1.4,
        zorder=5,
        label=label,
    )


def sequential_cmap(name: str = "finai_green") -> LinearSegmentedColormap:
    """从近白到深绿的顺序色阶，用于热力图。"""
    return LinearSegmentedColormap.from_list(
        name, ["#f6faf7", "#d5e8db", "#8fbf9f", "#2f6b45"]
    )


def save(figure, path: str | Path) -> Path:
    """保存并关闭。自动建目录，统一 180 dpi 与白底。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 有总标题时留出顶部空间，否则标题会被裁掉
    if figure._suptitle is not None:
        figure.tight_layout(rect=(0.0, 0.0, 1.0, 0.94))
    else:
        figure.tight_layout()
    figure.savefig(target, dpi=DPI, facecolor="white")
    plt.close(figure)
    return target


def spread_end_labels(values: "list[float]", span: float, gap_ratio: float = 0.05):
    """把靠得太近的端点标签上下错开，返回调整后的位置。

    折线图在最后一个点上标数值时，几条线常常挤在一起，直接标会叠成一团。
    这里按数值排序逐个推开，保证相邻标签至少隔开 span 的 gap_ratio；
    最后整体平移回原来的重心，避免所有标签一起往上偏、离数据点越来越远。
    """
    if not values:
        return []
    min_gap = max(span, 1e-9) * gap_ratio
    order = sorted(range(len(values)), key=lambda index: values[index])
    adjusted = list(values)
    for position in range(1, len(order)):
        previous, current = order[position - 1], order[position]
        if adjusted[current] - adjusted[previous] < min_gap:
            adjusted[current] = adjusted[previous] + min_gap

    shift = (sum(values) - sum(adjusted)) / len(values)
    return [value + shift for value in adjusted]
