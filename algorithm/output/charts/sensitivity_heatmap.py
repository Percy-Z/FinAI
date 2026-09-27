"""图 13：双因素敏感性热力图。

两个假设同时变动时估值的九宫格。颜色越深估值越高，红线是现价的等值线，
回答的问题：在哪些假设组合下，估值会低于现价。

只用 matplotlib，不引 numpy：图表层的约定是「不依赖 matplotlib 之外的东西」，
而这里要做的只是取最值、造一个按格子编号的坐标网格，标准库足够。
"""

from __future__ import annotations

from pathlib import Path

from algorithm.finai.sensitivity import GridResult

from .base import COLORS, new_figure, save, sequential_cmap


def render(
    grid: GridResult,
    path: str | Path,
    title: str = "双因素敏感性",
) -> Path:
    rows = list(grid.row_values)
    cols = list(grid.col_values)
    data = [[float(value) for value in line] for line in grid.cells]
    if not data or not data[0]:
        raise ValueError(
            "网格是空的：先调用 two_way_grid() 拿到 cells，再交给 render()"
        )

    flat = [value for line in data for value in line]
    low, high = min(flat), max(flat)
    spread = high - low
    # 全部格子数值相同时 spread 为 0，色阶和文字颜色都要另外给一个基准，
    # 否则 vmin == vmax，matplotlib 会报 RuntimeWarning 并画出空白图
    vmin, vmax = (low - spread * 0.35, high) if spread > 0 else (low - 1.0, low + 1.0)

    figure, axes = new_figure(1.35 * len(cols) + 4.6, 0.62 * len(rows) + 3.6)
    axes.imshow(
        data,
        origin="lower",
        aspect="auto",
        cmap=sequential_cmap(),
        vmin=vmin,
        vmax=vmax,
        zorder=2,
    )

    for row_index in range(len(rows)):
        for col_index in range(len(cols)):
            value = data[row_index][col_index]
            ratio = (value - low) / spread if spread > 0 else 0.0
            color = "white" if ratio > 0.58 else COLORS["ink"]
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

    # 等值线至少要 2×2 的网格才画得出来，只有一行或一列时 contour 会直接抛
    # TypeError: Input z must be at least a (2, 2) shaped array
    price_in_range = low <= grid.price <= high
    contour_drawn = price_in_range and len(rows) > 1 and len(cols) > 1
    if contour_drawn:
        # 不传 X、Y 时 contour 默认就用格子编号当坐标，和 imshow 的格子中心对齐
        axes.contour(
            data,
            levels=[grid.price],
            colors=[COLORS["accent"]],
            linewidths=1.8,
            zorder=4,
        )

    axes.set_xticks(range(len(cols)))
    axes.set_xticklabels([f"{value:.2%}" for value in cols])
    axes.set_yticks(range(len(rows)))
    axes.set_yticklabels([f"{value:.2%}" for value in rows])
    axes.set_xlabel(grid.col_name)
    axes.set_ylabel(grid.row_name)
    axes.tick_params(colors=COLORS["ink"], labelsize=10, length=0)
    for side in ("top", "right", "left", "bottom"):
        axes.spines[side].set_visible(False)

    if contour_drawn:
        subtitle = f"红线为现价 {grid.price:.2f} 元的等值线"
    elif price_in_range:
        subtitle = f"现价 {grid.price:.2f} 元在网格内，但网格只有单一取值，画不出等值线"
    else:
        subtitle = f"现价 {grid.price:.2f} 元落在全部组合之外"
    axes.set_title(
        f"{title}\n格内为每股价值（元），{subtitle}",
        fontsize=13,
        pad=14,
        color=COLORS["ink"],
    )
    return save(figure, path)
