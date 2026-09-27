"""图 16：勾稽校验总览。

行是检查项，列是年份，格子颜色表示结论。一眼看出哪一年哪一项出了问题。
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import numpy as np
from matplotlib.patches import Patch, Rectangle

from algorithm.finai.validate import ValidationReport

from .base import COLORS, new_figure, save

STATUS_ORDER = ("pass", "warning", "fail", "skipped")
STATUS_LABEL = {"pass": "通", "warning": "警", "fail": "失", "skipped": "跳", "absent": ""}
STATUS_FILL = {
    "pass": COLORS["primary_fill"],
    "warning": COLORS["warning_fill"],
    "fail": COLORS["danger_fill"],
    "skipped": COLORS["neutral_fill"],
    # 这一列根本没有这项检查，画成空白，和「输入缺失」区分开
    "absent": "none",
}
STATUS_EDGE = {
    "pass": COLORS["primary"],
    "warning": COLORS["warning"],
    "fail": COLORS["danger"],
    "skipped": COLORS["muted"],
    "absent": COLORS["grid"],
}


def render(
    reports: Sequence[ValidationReport],
    path: str | Path,
    title: str = "勾稽校验总览",
) -> Path:
    ordered = sorted(reports, key=lambda item: item.fiscal_year)
    years = [report.fiscal_year for report in ordered]
    if not ordered:
        raise ValueError("没有任何校验报告可画：先跑一次 run_checks")

    check_ids: list[str] = []
    for report in ordered:
        for check in report.checks:
            if check.check_id not in check_ids:
                check_ids.append(check.check_id)

    table: list[list[str]] = []
    for check_id in check_ids:
        line: list[str] = []
        for report in ordered:
            status = "absent"
            for check in report.checks:
                if check.check_id == check_id:
                    status = check.status
                    break
            line.append(status)
        table.append(line)

    figure, axes = new_figure(1.05 * len(years) + 4.8, 0.48 * len(check_ids) + 3.2)
    axes.set_xlim(-0.5, len(years) - 0.5)
    axes.set_ylim(-0.5, len(check_ids) - 0.5)
    axes.invert_yaxis()

    for row_index, line in enumerate(table):
        for col_index, status in enumerate(line):
            axes.add_patch(
                Rectangle(
                    (col_index - 0.46, row_index - 0.42),
                    0.92,
                    0.84,
                    facecolor=STATUS_FILL[status],
                    edgecolor=STATUS_EDGE[status],
                    linewidth=1.0,
                    zorder=2,
                )
            )
            axes.text(
                col_index,
                row_index,
                STATUS_LABEL[status],
                ha="center",
                va="center",
                fontsize=11,
                color=COLORS["ink"],
                zorder=3,
            )

    axes.set_xticks(range(len(years)))
    axes.set_xticklabels([str(year) for year in years])
    axes.set_yticks(range(len(check_ids)))
    axes.set_yticklabels(check_ids)
    axes.tick_params(colors=COLORS["ink"], labelsize=10, length=0)
    for side in ("top", "right", "left", "bottom"):
        axes.spines[side].set_visible(False)

    counts = {"pass": 0, "warning": 0, "fail": 0, "skipped": 0, "absent": 0}
    for line in table:
        for status in line:
            counts[status] += 1

    handles = [
        Patch(
            facecolor=STATUS_FILL[status],
            edgecolor=STATUS_EDGE[status],
            linewidth=1.0,
            label=label,
        )
        for status, label in (
            ("pass", "通过"),
            ("warning", "告警"),
            ("fail", "失败"),
            ("skipped", "跳过"),
        )
    ]
    axes.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.12),
        ncol=4,
        frameon=False,
        fontsize=10,
    )
    axes.set_title(
        f"{title}\n"
        f"共 {len(check_ids)} 项检查 × {len(years)} 年："
        f"通过 {counts['pass']}，告警 {counts['warning']}，"
        f"失败 {counts['fail']}，跳过 {counts['skipped']}",
        fontsize=13,
        pad=14,
        color=COLORS["ink"],
    )
    return save(figure, path)
