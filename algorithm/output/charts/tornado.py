"""图 12：龙卷风图（单因素敏感性）。

每个假设在上下界之间变动时，估值会落在哪个区间。按影响幅度从大到小排列，
回答的问题：精力该花在哪个假设上。
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from algorithm.finai.sensitivity import TornadoRow

from .base import COLORS, add_price_line, new_figure, save, style_axes


def render(
    rows: Sequence[TornadoRow],
    price: float,
    path: str | Path,
    title: str = "单因素敏感性（龙卷风图）",
) -> Path:
    if not rows:
        raise ValueError(
            "没有可画的参数：调用 tornado() 时至少传入一个 Variable"
        )
    ordered = list(reversed(list(rows)))
    positions = list(range(len(ordered)))

    figure, axes = new_figure(9.0, 0.55 * len(ordered) + 2.2)

    lows = [min(row.per_share_at_low, row.per_share_at_high) for row in ordered]
    highs = [max(row.per_share_at_low, row.per_share_at_high) for row in ordered]
    span = max(max(highs), price) - min(min(lows), price)
    pad = span * 0.06

    for position, row, low, high in zip(positions, ordered, lows, highs):
        axes.barh(
            position,
            high - low,
            left=low,
            height=0.46,
            color=COLORS["primary_fill"],
            edgecolor=COLORS["primary"],
            linewidth=1.0,
            zorder=3,
        )
        axes.plot(
            [row.per_share_base, row.per_share_base],
            [position - 0.23, position + 0.23],
            color=COLORS["ink"],
            linewidth=1.8,
            zorder=4,
        )
        axes.text(
            low - pad * 0.25,
            position,
            f"{low:.2f}",
            ha="right",
            va="center",
            fontsize=9,
            color=COLORS["ink"],
        )
        axes.text(
            high + pad * 0.25,
            position,
            f"{high:.2f}",
            ha="left",
            va="center",
            fontsize=9,
            color=COLORS["ink"],
        )

    add_price_line(axes, price)
    # 现价直接标在线上方，省掉一个会和横条打架的图例
    top = len(ordered) - 1
    axes.set_ylim(-0.62, top + 0.95)
    axes.text(
        price + span * 0.015,
        top + 0.45,
        f"现价 {price:.2f}",
        ha="left",
        va="center",
        fontsize=10,
        color=COLORS["accent"],
    )
    axes.set_yticks(positions)
    axes.set_yticklabels([row.name for row in ordered])
    axes.set_xlim(min(min(lows), price) - pad * 2.2, max(max(highs), price) + pad * 2.2)
    axes.set_xlabel("每股价值（元）")
    axes.set_title(
        f"{title}\n竖线为基准情景 {rows[0].per_share_base:.2f} 元",
        fontsize=13,
        pad=14,
        color=COLORS["ink"],
    )
    style_axes(axes, grid_axis="x")
    return save(figure, path)
