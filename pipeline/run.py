"""流水线主流程：读配置 → 三表预测 → 资本成本 → DCF 估值 → 落盘。

当前接了四个阶段。每加一个算法模块，就在这条链上再接一段：

    [config] → [forecast] → [wacc] → [valuation] → [落盘]

运行：
    cd ~/Desktop/FinAI
    python3 -m pipeline.run

产物：
    outputs/<run_id>/projections.json   三表预测
    outputs/<run_id>/valuation.json     DCF 估值结果与 WACC 的完整构成
    logs/<run_id>/trace.jsonl           逐步执行轨迹
    logs/<run_id>/run_manifest.json     本次运行总清单

其中 run_id 是运行时刻的时间戳，例如 20260928T143012，
这样今天跑的结果不会覆盖昨天结果。

注意「三表预测」与「估值」用的是两套假设对象（ForecastDrivers 与
ForecastAssumptions），中间由 adapters 转换。两条路径算出的 FCFF 必须一致，
有回归测试盯着这件事。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from algorithm.finai.forecast import BalanceSheetState, ForecastDrivers, build_forecast
from algorithm.finai.dcf import value_dcf
from algorithm.finai.types import ForecastAssumptions, MarketData, WaccInputs
from algorithm.finai.wacc import (
    check_wacc_sanity,
    wacc_from_market,
    weights_from_market,
)

from .adapters import company_to_market_data, drivers_to_assumptions
from .config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_MARKET_PATH,
    ConfigError,
    WaccParams,
    load_case,
    load_market_snapshot,
    load_terminal_params,
    load_wacc_params,
)
from .tracing import RunTrace

# 仓库根目录：本文件在 pipeline/ 下，往上退一级就是 FinAI/
REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = REPO_ROOT / "outputs"
CONFIG_PATH = DEFAULT_CONFIG_PATH
MARKET_PATH = DEFAULT_MARKET_PATH


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


def stage_wacc(
    drivers: ForecastDrivers,
    params: WaccParams,
    market: MarketData,
    trace: RunTrace,
) -> dict[str, Any]:
    """阶段：资本成本（WACC）。

    两个数刻意不从配置读，都在这里推导或引用：
        权重    由市值与有息负债算出来（weights_from_market）
        税率    与三表预测共用同一个值（drivers.tax_rate）

    返回一个字典而不是单个数字：报告里要列出 WACC 的完整构成
    （四个输入参数 + 税率 + 两个权重），这些数得跟着产物一起落盘。
    """
    started = time.perf_counter()

    equity_weight, debt_weight = weights_from_market(
        market.market_cap, market.interest_bearing_debt
    )
    wacc = wacc_from_market(
        risk_free=params.risk_free,
        beta=params.beta,
        equity_risk_premium=params.equity_risk_premium,
        cost_of_debt=params.cost_of_debt,
        tax_rate=drivers.tax_rate,
        market_cap=market.market_cap,
        debt_value=market.interest_bearing_debt,
    )
    warnings = check_wacc_sanity(
        WaccInputs(
            risk_free=params.risk_free,
            beta=params.beta,
            equity_risk_premium=params.equity_risk_premium,
            cost_of_debt=params.cost_of_debt,
            tax_rate=drivers.tax_rate,
            equity_weight=equity_weight,
            debt_weight=debt_weight,
        )
    )

    summary = {
        "value": wacc,
        "risk_free": params.risk_free,
        "beta": params.beta,
        "equity_risk_premium": params.equity_risk_premium,
        "cost_of_debt": params.cost_of_debt,
        "tax_rate": drivers.tax_rate,
        "equity_weight": equity_weight,
        "debt_weight": debt_weight,
        "sanity_warnings": warnings,
    }
    trace.record(
        step="wacc",
        tool="algorithm.finai.wacc.wacc_from_market",
        duration_s=time.perf_counter() - started,
        extra={"wacc": wacc, "sanity_warnings": warnings},
    )
    return summary


def stage_valuation(
    assumptions: ForecastAssumptions,
    market: MarketData,
    wacc: float,
    base_year: int,
    trace: RunTrace,
):
    """阶段：DCF 估值。

    base_year 要单独传进来：估值路径的假设集（ForecastAssumptions）里
    没有这个字段。它只影响产物里年份的标注（写成 1..5 还是 2026..2030），
    不影响任何数值 —— 但报告要读年份，所以还是得给对。
    """
    started = time.perf_counter()
    result = value_dcf(assumptions, market, wacc, base_year=base_year)
    trace.record(
        step="valuation",
        tool="algorithm.finai.dcf.value_dcf",
        duration_s=time.perf_counter() - started,
        extra={
            "enterprise_value": result.enterprise_value,
            "equity_value": result.equity_value,
            "per_share": result.per_share,
            "implied_return": result.implied_return,
            "diagnostics": result.diagnostics,
        },
    )
    return result


def _record_failure(trace: RunTrace, error: Exception) -> Path:
    """失败的运行也要留证据：记一笔失败，再写清单。

    通知要求「完整记录…结果生成情况」—— 失败的运行也是运行。
    之前只有「读配置」阶段的失败被处理，其它阶段抛异常会直接丢掉清单，
    那是不对的。

    把两个输入文件也记进去：这样失败的清单里能查到「当时读的是哪份配置、
    哈希是多少」，而不是只有一句错误信息。
    """
    trace.record(
        step="failed",
        tool=type(error).__name__,
        status="failed",
        inputs=[CONFIG_PATH, MARKET_PATH],
        extra={"error": str(error)},
    )
    return trace.finish(status="failed")


def _run_once(run_id: str, trace: RunTrace) -> int:
    """跑一遍完整流水线。任何异常都往上抛，由 main 统一处理。"""
    # 阶段一：读配置
    started = time.perf_counter()
    company, drivers, opening = load_case()
    terminal = load_terminal_params()
    params = load_wacc_params()
    target, peers = load_market_snapshot()  # peers 留给后面的相对估值阶段
    trace.record(
        step="load_config",
        tool="pipeline.config",
        duration_s=time.perf_counter() - started,
        inputs=[CONFIG_PATH, MARKET_PATH],
    )

    # 适配：把三表路径的假设翻译成估值路径的假设。
    # 这一步不产出文件，只是形状转换，所以不单独记日志 ——
    # 但转换结果会完整写进 valuation.json，一样可追溯。
    assumptions = drivers_to_assumptions(drivers, terminal)
    market = company_to_market_data(target)

    # 阶段二：三表预测
    forecast = stage_forecast(drivers, opening, company.source, trace)

    # 阶段三：资本成本
    wacc_summary = stage_wacc(drivers, params, market, trace)

    # 阶段四：估值
    result = stage_valuation(
        assumptions, market, wacc_summary["value"], drivers.base_year, trace
    )

    run_dir = OUTPUT_ROOT / run_id
    common = {
        "run_id": run_id,
        "company": {
            "name": company.name,
            "code": company.code,
            "source": company.source,
        },
    }

    # 产物一：三表预测。假设用 asdict 整个导出，是为了产物「自解释」——
    # 半年后打开这个 json，能直接看到当时用的每一个假设值。
    forecast_payload = {
        **common,
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
    started = time.perf_counter()
    forecast_path = write_json(run_dir / "projections.json", forecast_payload)
    trace.record(
        step="write_projections",
        tool="pipeline.run.write_json",
        duration_s=time.perf_counter() - started,
        outputs=[forecast_path],
    )

    # 产物二：估值结果，附 WACC 的完整构成（报告里要逐行列出来）
    valuation_payload = {
        **common,
        "assumptions": asdict(assumptions),
        "wacc": wacc_summary,
        "valuation": result.to_dict(),
    }
    started = time.perf_counter()
    valuation_path = write_json(run_dir / "valuation.json", valuation_payload)
    trace.record(
        step="write_valuation",
        tool="pipeline.run.write_json",
        duration_s=time.perf_counter() - started,
        outputs=[valuation_path],
    )

    manifest = trace.finish()

    # 打印运行摘要，让人一眼看到这次跑出了什么
    first_year = drivers.base_year + 1
    last_year = drivers.base_year + drivers.years
    print(f"公司      {company.name}（{company.code}）")
    print(f"预测期    {first_year} ~ {last_year}，共 {drivers.years} 年")
    print(f"基期平衡差额  {opening.balance_gap:,.6f}")
    print(f"预测期最大平衡差额  {forecast.worst_balance_gap:.2e}")

    print("\nWACC 的构成")
    print(f"    无风险利率  {wacc_summary['risk_free']:>9.4%}")
    print(f"    贝塔        {wacc_summary['beta']:>9.2f}")
    print(f"    风险溢价    {wacc_summary['equity_risk_premium']:>9.4%}")
    print(f"    债务成本    {wacc_summary['cost_of_debt']:>9.4%}")
    print(f"    税率        {wacc_summary['tax_rate']:>9.4%}   （与三表预测共用）")
    print(f"    股权权重    {wacc_summary['equity_weight']:>9.4%}   （由市值推导）")
    print(f"    债务权重    {wacc_summary['debt_weight']:>9.4%}   （由债务推导）")
    print(f"    {'WACC':<10}{wacc_summary['value']:>9.4%}")
    for note in wacc_summary["sanity_warnings"]:
        print(f"    ⚠️ {note}")

    print("\n估值结果")
    print(f"    企业价值      {result.enterprise_value:>16,.2f}")
    print(f"    减：净债务    {result.net_debt:>16,.2f}")
    print(f"    权益价值      {result.equity_value:>16,.2f}")
    print(f"    摊薄股数      {result.diluted_shares:>16,.2f}")
    print(f"    每股价值      {result.per_share:>16,.2f}")
    print(f"    现价          {result.price:>16,.2f}")
    print(f"    隐含回报      {result.implied_return:>16.2%}")
    print(f"    终值占企业价值 {result.terminal_share_of_ev:>15.2%}")
    print(f"    隐含退出倍数  {result.implied_exit_multiple:>16.2f}x")
    for note in result.diagnostics:
        print(f"    ⚠️ {note}")

    print("\nFCFF 逐年：" + "   ".join(
        f"{year.year} {year.fcff:,.2f}" for year in forecast.years
    ))

    print(f"\n产物      {forecast_path}")
    print(f"          {valuation_path}")
    print(f"轨迹      {trace.trace_path}")
    print(f"运行清单  {manifest}")

    if trace.git["dirty"]:
        print(
            "\n注意：工作区有未提交的改动，这次运行无法从任何一个"
            f"提交复现（代码版本 {trace.git['commit_short']} + 本地修改）。"
        )
    return 0


def main() -> int:
    """流水线入口：开一次运行、跑流程、处理失败。

    失败分两类，处理方式不同：
        ConfigError  人填的数据或配置有问题 → 打印一句人话，返回退出码 1
        其它异常     代码有 bug            → 同样留下失败记录，但把完整堆栈抛出去

    两类都必须在日志里留下「这次运行失败过」的证据。
    """
    run_id = new_run_id()
    trace = RunTrace(run_id)
    print(f"运行标识  {run_id}")
    print(f"日志目录  {trace.dir}")

    try:
        return _run_once(run_id, trace)
    except ConfigError as err:
        manifest = _record_failure(trace, err)
        print(f"\n[配置或数据有误，已停止]\n{err}")
        print(f"\n失败记录已写出：{manifest}")
        return 1
    except Exception as err:
        manifest = _record_failure(trace, err)
        print(f"\n[运行中断] 失败记录已写出：{manifest}")
        print("下面是完整堆栈 —— 这属于代码问题，不是配置问题：")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
