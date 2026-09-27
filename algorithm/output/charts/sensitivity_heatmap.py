"""图 13：双因素敏感性热力图。

两个假设同时变动时估值的九宫格。颜色越深估值越高，红线是现价的等值线，
回答的问题：在哪些假设组合下，估值会低于现价。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from algorithm.finai.sensitivity import GridResult

from .base import COLORS, new_figure, save, sequential_cmap


def render(
    grid: GridResult,
    path: str | Path,
    title: str = "双因素敏感性",
) -> Path:
    data = np.array(grid.cells, dtype=float)
    rows = list(grid.row_values)
    cols = list(grid.col_values)

    figure, axes = new_figure(1.35 * len(cols) + 4.6, 0.62 * len(rows) + 3.6)
    axes.imshow(
        data,
        origin="lower",
        aspect="auto",
        cmap=sequential_cmap(),
        vmin=data.min() - (data.max() - data.min()) * 0.35,
        vmax=data.max(),
        zorder=2,
    )

    low, high = data.min(), data.max()
    spread = high - low if high > low else 1.0
    for row_index in range(len(rows)):
        for col_index in range(len(cols)):
            value = data[row_index][col_index]
            color = "white" if (value - low) / spread > 0.58 else COLORS["ink"]
            axes.text(
                col_index,
                row_index,
                f"{value:.2f}",
                ha="center",
                va="center",
                fontsize=10,
                color=color,
                zorder=3,
            )

    contour_drawn = False
    if low <= grid.price <= high:
        grid_x, grid_y = np.meshgrid(range(len(cols)), range(len(rows)))
        axes.contour(
            grid_x,
            grid_y,
            data,
            levels=[grid.price],
            colors=[COLORS["accent"]],
            linewidths=1.8,
            zorder=4,
        )
        contour_drawn = True

    axes.set_xticks(range(len(cols)))
    axes.set_xticklabels([f"{value:.2%}" for value in cols])
    axes.set_yticks(range(len(rows)))
    axes.set_yticklabels([f"{value:.2%}" for value in rows])
    axes.set_xlabel(grid.col_name)
    axes.set_ylabel(grid.row_name)
    axes.tick_params(colors=COLORS["ink"], labelsize=10, length=0)
    for side in ("top", "right", "left", "bottom"):
        axes.spines[side].set_visible(False)

    subtitle = (
        f"红线为现价 {grid.price:.2f} 元的等值线"
        if contour_drawn
        else f"现价 {grid.price:.2f} 元落在全部组合之外"
    )
    axes.set_title(
        f"{title}\n格内为每股价值（元），{subtitle}",
        fontsize=13,
        pad=14,
        color=COLORS["ink"],
    )
    return save(figure, path)
