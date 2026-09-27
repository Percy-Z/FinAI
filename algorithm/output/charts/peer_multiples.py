"""图 14：可比公司倍数对比。

每个倍数一张小图，横条是目标公司与各家可比公司的倍数，虚线是可比公司的中位数。
回答的问题：目标公司相对可比公司是贵还是便宜。
"""

from __future__ import annotations

from pathlib import Path

from algorithm.finai.comps import MULTIPLE_LABELS, CompsResult, compute_multiples

from .base import COLORS, new_figure, save, style_axes

# 按这个顺序出图，没有数据的倍数自动跳过
PREFERRED_ORDER = ("pe", "pb", "ev_ebitda", "ev_ebit", "ps")
MAX_PANELS = 3


def render(
    comps: CompsResult,
    path: str | Path,
    title: str = "可比公司倍数对比",
) -> Path:
    names = [
        name
        for name in PREFERRED_ORDER
        if name in comps.stats and comps.stats[name].count > 0
    ][:MAX_PANELS]
    if not names:
        raise ValueError("没有任何可用的倍数，画不出这张图")

    target_values = compute_multiples(comps.target)
    labels = ["目标"] + [peer.code for peer in comps.peers]
    values_by_company = [target_values] + [
        comps.peer_multiples[peer.code] for peer in comps.peers
    ]

    figure, axes_list = new_figure(
        4.1 * len(names) + 0.6, 0.52 * len(labels) + 2.6, panels=len(names)
    )
    if len(names) == 1:
        axes_list = [axes_list]

    for axes, name in zip(axes_list, names):
        entry = comps.stats[name]
        positions = list(range(len(labels)))
        plotted_values = [values.get(name) for values in values_by_company]

        for position, value in zip(positions, plotted_values):
            if value is None:
                continue
            is_target = position == 0
            axes.barh(
                position,
                value,
                height=0.52,
                color=COLORS["primary_fill"] if is_target else COLORS["neutral_fill"],
                edgecolor=COLORS["primary"] if is_target else COLORS["muted"],
                linewidth=1.1,
                zorder=3,
            )
            axes.text(
                value * 1.02,
                position,
                f"{value:.1f}",
                ha="left",
                va="center",
                fontsize=9,
                color=COLORS["ink"],
            )

        axes.axvline(
            entry.median,
            color=COLORS["accent"],
            linestyle="--",
            linewidth=1.3,
            zorder=4,
        )
        axes.text(
            entry.median,
            -0.78,
            f"中位数 {entry.median:.1f}",
            ha="center",
            va="bottom",
            fontsize=9,
            color=COLORS["accent"],
        )

        axes.set_yticks(positions)
        axes.set_yticklabels(labels)
        axes.invert_yaxis()
        axes.set_xlim(0, max(entry.maximum, entry.median) * 1.28)
        axes.set_title(MULTIPLE_LABELS.get(name, name.upper()), fontsize=12, pad=10, color=COLORS["ink"])
        style_axes(axes, grid_axis="x")

    figure.suptitle(title, fontsize=13, color=COLORS["ink"], y=1.0)
    return save(figure, path)
