"""流水线：读配置 → 三表预测 → 落盘 projections.json，全程留痕。

这是第一条端到端的链路：一个 TOML 文件进去，一个 JSON 文件出来，
同时留下一条可追溯的轨迹。

运行：
    cd ~/Desktop/FinAI
    python3 -m pipeline.run

产物：
    outputs/<run_id>/projections.json   数据结果
    logs/<run_id>/trace.jsonl           逐步执行轨迹
    logs/<run_id>/run_manifest.json     本次运行总清单

其中 run_id 是运行时刻的时间戳，例如 20260928T143012，
这样今天跑的结果不会覆盖昨天结果。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from algorithm.finai.forecast import BalanceSheetState, ForecastDrivers, build_forecast

from .config import DEFAULT_CONFIG_PATH, ConfigError, load_case
from .tracing import RunTrace

# 仓库根目录：本文件在 pipeline/ 下，往上退一级就是 FinAI/
REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = REPO_ROOT / "outputs"
CONFIG_PATH = DEFAULT_CONFIG_PATH


def new_run_id() -> str:
    """生成一次运行的标识。用时间戳，保证多次运行互不覆盖。"""
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    """把字典写成 JSON 文件，自动建父目录。

    ensure_ascii=False 让中文原样写出（否则会变成 \\u6c49 这种转义），
    indent=2 让文件能被人和 git diff 读懂。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def stage_forecast(
    drivers: ForecastDrivers,
    opening: BalanceSheetState,
    source: str,
    trace: RunTrace,
):
    """阶段：三表联动预测。

    每个阶段都是一个「薄封装」：只做一件事，把算法模块的函数接进来。
    它同时也负责记录自己的日志 —— 这就是为什么它要多收一个 trace 参数：
    每个阶段自己知道该记什么，主流程不用替它操心。
    """
    started = time.perf_counter()
    result = build_forecast(opening, drivers, source=source)
    trace.record(
        step="forecast",
        tool="algorithm.finai.forecast.build_forecast",
        duration_s=time.perf_counter() - started,
        extra={
            "years": drivers.years,
            "fcff_series": result.fcff_series,
            "worst_balance_gap": result.worst_balance_gap,
        },
    )
    return result


def main() -> int:
    """流水线主函数。返回值是退出码：0 表示成功，1 表示失败。"""
    run_id = new_run_id()
    trace = RunTrace(run_id)

    print(f"运行标识  {run_id}")
    print(f"日志目录  {trace.dir}")

    # 阶段一：读配置。配置错是硬失败 —— 停下，不往下跑。
    started = time.perf_counter()
    try:
        company, drivers, opening = load_case()
    except ConfigError as err:
        # 失败的运行也要留证据：记一笔失败的步骤，然后写清单。
        trace.record(
            step="load_config",
            tool="pipeline.config.load_case",
            status="failed",
            duration_s=time.perf_counter() - started,
            inputs=[CONFIG_PATH],
            extra={"error": str(err)},
        )
        manifest = trace.finish(status="failed")
        print(f"[配置有误，已停止]\n{err}")
        print(f"\n失败记录已写出：{manifest}")
        return 1

    trace.record(
        step="load_config",
        tool="pipeline.config.load_case",
        duration_s=time.perf_counter() - started,
        inputs=[CONFIG_PATH],
    )

    # 阶段二：三表预测
    forecast = stage_forecast(drivers, opening, company.source, trace)

    # 组装产物。assumptions 用 asdict 整个导出，是为了产物「自解释」：
    # 半年后打开这个 json，能直接看到当时用的每一个假设值。
    payload = {
        "run_id": run_id,
        "company": {
            "name": company.name,
            "code": company.code,
            "source": company.source,
        },
        "assumptions": asdict(drivers),
        "opening_balance": asdict(opening),
        "years": forecast.to_records(),
        "summary": {
            "base_year": drivers.base_year,
            "years": drivers.years,
            "worst_balance_gap": forecast.worst_balance_gap,
            "fcff_series": forecast.fcff_series,
        },
    }

    # 阶段三：落盘
    started = time.perf_counter()
    target = write_json(OUTPUT_ROOT / run_id / "projections.json", payload)
    trace.record(
        step="write_projections",
        tool="pipeline.run.write_json",
        duration_s=time.perf_counter() - started,
        outputs=[target],
    )

    manifest = trace.finish()

    # 打印运行摘要，让人一眼看到这次跑出了什么
    first_year = drivers.base_year + 1
    last_year = drivers.base_year + drivers.years
    print(f"公司      {company.name}（{company.code}）")
    print(f"预测期    {first_year} ~ {last_year}，共 {drivers.years} 年")
    print(f"基期平衡差额  {opening.balance_gap:,.6f}")
    print(f"预测期最大平衡差额  {forecast.worst_balance_gap:.2e}")
    print("\nFCFF 逐年：")
    for year in forecast.years:
        print(f"    {year.year}    {year.fcff:>12,.2f}")
    print(f"\n产物      {target}")
    print(f"轨迹      {trace.trace_path}")
    print(f"运行清单  {manifest}")

    if trace.git["dirty"]:
        print(
            "\n注意：工作区有未提交的改动，这次运行无法从任何一个"
            f"提交复现（代码版本 {trace.git['commit_short']} + 本地修改）。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
