"""图 15：三表联动预测趋势。

左图是盈利与现金流，右图是现金与债务。
回答的问题：未来五年的经营和偿债能力怎么走。
"""

from __future__ import annotations

from pathlib import Path

from algorithm.finai.forecast import ThreeStatementForecast

from .base import COLORS, SERIES_COLORS, new_figure, save, spread_end_labels, style_axes


def render(
    forecast: ThreeStatementForecast,
    path: str | Path,
    title: str = "三表联动预测",
    unit: str = "元",
) -> Path:
    years = [year.year for year in forecast.years]
    income = [year.income for year in forecast.years]
    balance = [year.balance for year in forecast.years]

    figure, (left, right) = new_figure(12.4, 4.4, panels=2)

    series = [
        ("营业收入", [item.revenue for item in income], SERIES_COLORS[0]),
        ("EBIT", [item.ebit for item in income], SERIES_COLORS[1]),
        ("归母净利润", [item.net_profit_attributable for item in income], SERIES_COLORS[2]),
        ("FCFF", [year.fcff for year in forecast.years], SERIES_COLORS[3]),
    ]

    end_values = [values[-1] for _, values, _ in series]
    span = max(max(values) for _, values, _ in series)
    label_positions = spread_end_labels(end_values, span)

    for (label, values, color), label_y in zip(series, label_positions):
        left.plot(
            years,
            values,
            marker="o",
            markersize=4,
            linewidth=1.8,
            color=color,
            label=label,
            zorder=3,
        )
        left.text(
            years[-1],
            label_y,
            f" {values[-1]:,.0f}",
            ha="left",
            va="center",
            fontsize=9,
            color=color,
        )

    left.set_xticks(years)
    left.set_ylabel(f"金额（{unit}）")
    left.set_title("盈利与现金流", fontsize=12, pad=10, color=COLORS["ink"])
    left.legend(
        frameon=False,
        fontsize=9,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.12),
        ncol=4,
    )
    left.margins(x=0.12)
    style_axes(left, grid_axis="y")

    width = 0.36
    left_positions = [year - width / 2 for year in years]
    right_positions = [year + width / 2 for year in years]
    right.bar(
        left_positions,
        [item.cash for item in balance],
        width=width,
        color=COLORS["primary_fill"],
        edgecolor=COLORS["primary"],
        linewidth=1.0,
        label="期末现金",
        zorder=3,
    )
    right.bar(
        right_positions,
        [item.interest_bearing_debt for item in balance],
        width=width,
        color=COLORS["neutral_fill"],
        edgecolor=COLORS["muted"],
        linewidth=1.0,
        label="有息负债",
        zorder=3,
    )
    right.plot(
        years,
        [item.net_debt for item in balance],
        marker="o",
        markersize=4,
        linewidth=1.6,
        color=COLORS["accent"],
        label="净债务",
        zorder=4,
    )
    right.axhline(0, color=COLORS["grid"], linewidth=1.0, zorder=2)
    right.set_xticks(years)
    right.set_ylabel(f"金额（{unit}）")
    right.set_title("现金与债务", fontsize=12, pad=10, color=COLORS["ink"])
    right.legend(
        frameon=False,
        fontsize=9,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.12),
        ncol=3,
    )
    style_axes(right, grid_axis="y")

    figure.suptitle(
        f"{title} · 最大平衡差额 {forecast.worst_balance_gap:.1e}",
        fontsize=13,
        color=COLORS["ink"],
    )
    return save(figure, path)
