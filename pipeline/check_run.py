"""产物验收：检查一次运行是不是真的产出齐全。

为什么需要这一层
----------------
图表依赖 matplotlib。装不上时输出层的策略是「跳过图表并记一条警告」，
这个降级本身是合理的 —— 不该因为画不了图就让整条流水线失败。

但它有个危险的后果：会**悄悄**产出一份没有图的报告。而报告是交付物，
没有图就是不合格。

这件事真实发生过：一次 `make verify` 跑出来的报告里一张图都没有，
而 verify 仍然打印「全部检查通过」。问题不在流水线，在验收 ——
它只检查了「命令有没有报错」，没检查「产物对不对」。

所以这一层做三件事：
    1. 产物清单完整性：数据文件 01-10、报告 17/18、图表 11-16 一个不能少
    2. 报告自洽性：报告里引用的每一张图都真的存在，而且六张图都引用了
    3. 把清单里的 warnings 摆到台面上 —— 警告不该藏在 JSON 里没人看见

用法（通常在 `make verify` 的最后一步）：
    python3 -m pipeline.check_run              # 检查最新一次运行
    python3 -m pipeline.check_run 20261002T161721948
    python3 -m pipeline.check_run --allow-missing-charts   # 明确接受无图
"""

from __future__ import annotations

import json
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .console import safe_console

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = REPO_ROOT / "runs"

# 一次「完整」的运行应该有哪些文件。少一个都算不合格。
REQUIRED_DATA_FILES = tuple(
    f"{index:02d}_{name}"
    for index, name in enumerate(
        (
            "projections.json",
            "validation_report.json",
            "valuation.json",
            "tornado.csv",
            "sensitivity.csv",
            "reverse_dcf.json",
            "comps.csv",
            "comps_implied.csv",
            "football_field.json",
            "run_manifest.json",
        ),
        start=1,
    )
)
REQUIRED_REPORTS = ("17_report.md", "18_report.html")
# PDF 需要 reportlab，属于「装了就该有」：装了却没生成算不合格，
# 没装则只记一条说明。判断依据是当前解释器能不能 import reportlab。
OPTIONAL_REPORT_PDF = "19_report.pdf"
REQUIRED_CHARTS = tuple(
    f"{index:02d}_{name}"
    for index, name in enumerate(
        (
            "football_field.png",
            "tornado.png",
            "sensitivity_heatmap.png",
            "peer_multiples.png",
            "forecast_trend.png",
            "validation_summary.png",
        ),
        start=11,
    )
)


@dataclass
class CheckOutcome:
    """验收结论。errors 非空就是不合格。"""

    directory: Path
    errors: list[str]
    warnings: list[str]
    notes: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


def newest_run(root: Path = RUNS_ROOT) -> Path:
    """取最近一次运行。按目录名排序 —— 目录名就是时间戳，字典序等于时间序。"""
    if not root.exists():
        raise FileNotFoundError(f"{root} 不存在，还没有跑过流水线")
    candidates = sorted(path for path in root.iterdir() if path.is_dir())
    if not candidates:
        raise FileNotFoundError(f"{root} 里没有任何一次运行")
    return candidates[-1]


def check_run(
    directory: Path, require_charts: bool = True
) -> CheckOutcome:
    """检查一次运行的产物。"""
    errors: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []

    # ── 1. 数据文件 ──────────────────────────────────────────────────
    for name in REQUIRED_DATA_FILES:
        path = directory / name
        if not path.exists():
            errors.append(f"缺数据文件：{name}")
        elif path.stat().st_size == 0:
            errors.append(f"数据文件是空的：{name}")

    # ── 2. 报告 ──────────────────────────────────────────────────────
    for name in REQUIRED_REPORTS:
        path = directory / name
        if not path.exists():
            errors.append(f"缺报告：{name}")
        elif path.stat().st_size == 0:
            errors.append(f"报告是空的：{name}")

    # ── 3. 图表 ──────────────────────────────────────────────────────
    charts_dir = directory / "charts"
    present_charts: list[str] = []
    for name in REQUIRED_CHARTS:
        path = charts_dir / name
        if path.exists() and path.stat().st_size > 0:
            present_charts.append(name)
        elif require_charts:
            errors.append(f"缺图表：charts/{name}")
        else:
            notes.append(f"缺图表（已声明可以没有）：charts/{name}")

    # ── 4. 报告里的图表引用 ──────────────────────────────────────────
    report_path = directory / "17_report.md"
    if report_path.exists():
        report = report_path.read_text(encoding="utf-8")
        referenced = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", report)
        for relative in referenced:
            if not (directory / relative).exists():
                errors.append(f"报告引用了不存在的图：{relative}")
        if require_charts:
            missing_in_report = [
                name
                for name in REQUIRED_CHARTS
                if name not in " ".join(referenced)
            ]
            if missing_in_report:
                errors.append(
                    "报告没有引用这些图："
                    + "、".join(missing_in_report)
                    + "（图生成了但没进报告，等于没有）"
                )
        # 报告应有八个以上章节，防止渲染中途出错只写了一半
        headings = re.findall(r"^## ", report, flags=re.MULTILINE)
        if len(headings) < 10:
            errors.append(f"报告章节偏少：只有 {len(headings)} 个二级标题（应有 10 个）")

    # ── 5. 清单里的警告 ──────────────────────────────────────────────
    # ── 4.5 PDF 报告 ─────────────────────────────────────────────────
    pdf_exist = (directory / OPTIONAL_REPORT_PDF).exists()
    try:
        # 只探测有没有，不导入 —— 导入了才算用得上，用不上就会被判成无用导入
        has_reportlab = importlib.util.find_spec("reportlab") is not None
    except ImportError:
        has_reportlab = False
    if not has_reportlab:
        if pdf_exist:
            notes.append(f"有 {OPTIONAL_REPORT_PDF}（装了 reportlab 才会生成）")
    else:
        if pdf_exist:
            notes.append("报告三份齐：Markdown / HTML / PDF")
        else:
            errors.append(
                f"缺 {OPTIONAL_REPORT_PDF}：当前解释器装了 reportlab 却没生成 PDF，"
                "可能是渲染报错"
            )

    manifest_path = directory / "10_run_manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            errors.append(f"运行清单不是合法 JSON：{err}")
        else:
            warnings.extend(manifest.get("warnings") or [])
            narrative = (manifest.get("inputs") or {}).get("narrative") or {}
            if not narrative.get("ok"):
                notes.append(
                    "本次没有大模型叙述（"
                    + "；".join(narrative.get("notes") or ["未调用"])
                    + "）—— 报告是数据版本"
                )
            elif narrative.get("number_warnings"):
                warnings.append(
                    f"数字校验有 {len(narrative['number_warnings'])} 处待核，"
                    "见报告附录 10.3"
                )
            if present_charts:
                notes.append(f"图表 {len(present_charts)}/{len(REQUIRED_CHARTS)} 张")

    return CheckOutcome(
        directory=directory, errors=errors, warnings=warnings, notes=notes
    )


def main(argv: list[str] | None = None) -> int:
    safe_console()
    args = list(argv if argv is not None else sys.argv[1:])
    require_charts = "--allow-missing-charts" not in args
    args = [a for a in args if not a.startswith("--")]

    directory = Path(args[0]) if args else newest_run()
    if not directory.is_absolute():
        directory = RUNS_ROOT / directory

    print(f"验收运行目录  {directory}")
    outcome = check_run(directory, require_charts=require_charts)

    for note in outcome.notes:
        print(f"  说明  {note}")
    for warning in outcome.warnings:
        print(f"  警告  {warning}")
    for error in outcome.errors:
        print(f"  不合格  {error}")

    if outcome.ok:
        print("产物验收通过。")
        return 0
    print(f"\n产物验收不通过：{len(outcome.errors)} 个问题。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
