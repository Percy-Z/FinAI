"""图 11：估值区间（足球场图）。

把每种估值方法的区间画成一条横条，再标一条现价参考线。
回答的问题：不同方法给出的结论差多少、现价落在区间内还是区间外。
"""

from __future__ import annotations

from pathlib import Path

from algorithm.finai.comps import FootballField

from .base import COLORS, add_price_line, new_figure, save, style_axes


def render(field: FootballField, path: str | Path, title: str = "估值区间") -> Path:
    if not field.bands:
        raise ValueError(
            "没有任何估值区间可画：先确认至少有一种估值方法算出了结果"
        )
    bands = list(reversed(field.bands))
    positions = list(range(len(bands)))

    figure, axes = new_figure(9.0, 0.62 * len(bands) + 2.1)
    span = max(band.high for band in bands) - min(band.low for band in bands)
    narrow = 0.02 * span if span > 0 else 0.0

    for position, band in zip(positions, bands):
        axes.barh(
            position,
            max(band.high - band.low, span * 0.0008),
            left=band.low,
            height=0.44,
            color=COLORS["primary_fill"],
            edgecolor=COLORS["primary"],
            linewidth=1.1,
            zorder=3,
        )
        axes.plot(
            [band.central, band.central],
            [position - 0.22, position + 0.22],
            color=COLORS["primary"],
            linewidth=2.0,
            zorder=4,
        )
        if band.high - band.low < narrow:
            axes.text(
                band.central,
                position + 0.32,
                f"{band.low:.2f}",
                ha="center",
                va="bottom",
                fontsize=9,
                color=COLORS["ink"],
            )
        else:
            axes.text(
                band.low,
                position + 0.32,
                f"{band.low:.2f}",
                ha="left",
                va="bottom",
                fontsize=9,
                color=COLORS["ink"],
            )
            axes.text(
                band.high,
                position + 0.32,
                f"{band.high:.2f}",
                ha="right",
                va="bottom",
                fontsize=9,
                color=COLORS["ink"],
            )

    add_price_line(axes, field.price, label=f"现价 {field.price:.2f}")
    axes.set_yticks(positions)
    axes.set_yticklabels([band.name for band in bands])
    axes.set_xlabel("每股价值（元）")
    axes.set_title(title, fontsize=13, pad=14, color=COLORS["ink"])
    axes.legend(loc="lower right", frameon=False, fontsize=10)
    style_axes(axes, grid_axis="x")
    return save(figure, path)
