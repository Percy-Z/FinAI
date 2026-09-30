"""流水线主流程：读配置 → 三表预测 → 资本成本 → DCF 估值 → 落盘。

当前接了四个阶段。每加一个算法模块，就在这条链上再接一段：

    [config] → [forecast] → [wacc] → [valuation] → [落盘]

运行：
    cd ~/Desktop/FinAI
    python3 -m pipeline.run

产物：
    runs/<run_id>/projections.json   三表预测
    runs/<run_id>/valuation.json     DCF 估值结果与 WACC 的完整构成
    logs/<run_id>/trace.jsonl           逐步执行轨迹
    logs/<run_id>/run_manifest.json     本次运行总清单

其中 run_id 是运行时刻的时间戳，例如 20260928T143012，
这样今天跑的结果不会覆盖昨天结果。

注意「三表预测」与「估值」用的是两套假设对象（ForecastDrivers 与
ForecastAssumptions），中间由 adapters 转换。两条路径算出的 FCFF 必须一致，
有回归测试盯着这件事。
"""

from __future__ import annotations

import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from algorithm.finai.forecast import BalanceSheetState, ForecastDrivers, build_forecast
from algorithm.finai.forecast import to_snapshots
from algorithm.finai.dcf import value_dcf
from algorithm.finai.types import ForecastAssumptions, MarketData, WaccInputs
from algorithm.finai.validate import run_checks
from algorithm.finai.wacc import (
    check_wacc_sanity,
    wacc_from_market,
    weights_from_market,
)
from algorithm.output import RunOutputs, export_run, summarize

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
OUTPUT_ROOT = REPO_ROOT / "runs"
CONFIG_PATH = DEFAULT_CONFIG_PATH
MARKET_PATH = DEFAULT_MARKET_PATH


def new_run_id() -> str:
    """生成一次运行的标识。时间戳 + 毫秒，保证多次运行互不覆盖。

    为什么要带毫秒：整套流水线只要 0.1 秒，同一秒内跑两次完全可能。
    而产物落盘用的是输出层的 export_run —— 它遇到「目录已存在且有内容」
    会明确报错（这比静默覆盖好，静默覆盖会让人以为看到的是最新结果）。
    所以编号必须细到毫秒。
    """
    now = datetime.now()
    return now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}"


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


def validation_totals(reports) -> dict[str, int]:
    """把各年的校验结论汇总成四个计数。

    四种结论的含义不同，不能混为一谈：
        pass     该平的平了
        warning  有差异但存在合理解释空间，或触发了信号类检查
        fail     该平的没平，属于数据错误，必须查
        skipped  输入缺失，无法检查 —— 注意这**不等于通过**
    """
    totals = {"pass": 0, "warning": 0, "fail": 0, "skipped": 0}
    for report in reports:
        for status in totals:
            totals[status] += report.summary[status]
    return totals


def stage_validate(forecast, trace: RunTrace) -> list:
    """阶段：勾稽校验。把算出来的三张表反过来验一遍。

    校验的是「报表内部自不自洽」——资产 = 负债 + 权益、现金滚动、
    利润与现金的转化关系、量级是否合理等，一共 11 项。

    ⚠️ 现在校验的是「预测出来的三张表」，用来证明预测内部自洽。
    等抽取层就绪后，年报里的历史数据也要走同一套校验 —— 那才是
    真正意义上的「年报勾稽校验」，也是评分点里「财务勾稽关系」的正主。
    现在这一步是那件事的预演，链路和接口完全一样。
    """
    started = time.perf_counter()
    snapshots = to_snapshots(forecast)
    reports = [run_checks(snapshot) for snapshot in snapshots]
    totals = validation_totals(reports)
    trace.record(
        step="validate",
        tool="algorithm.finai.validate.run_checks",
        duration_s=time.perf_counter() - started,
        extra={"years": len(reports), **totals},
    )
    return reports


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

    # 阶段三：勾稽校验（把刚算出的三张表反过来验一遍）
    reports = stage_validate(forecast, trace)

    # 阶段四：资本成本
    wacc_summary = stage_wacc(drivers, params, market, trace)

    # 阶段五：估值
    result = stage_valuation(
        assumptions, market, wacc_summary["value"], drivers.base_year, trace
    )

    # 落盘：交给算法侧输出层的 export_run。
    #
    # 只传目前拿得到的结果 —— RunOutputs 里为 None 的字段不会生成文件。
    # 所以以后接一个新阶段时，这里只是多传一个参数，不用再写一遍
    # 「组装内容 → 写文件 → 记账」。
    #
    # meta 里塞的是框架层独有的信息：代码版本、输入文件哈希、步骤摘要。
    # 这样产物目录里的清单是自解释的 —— 打开 runs/<编号>/ 就能看到
    # 这批数字是哪版代码、读的哪份配置算出来的。
    started = time.perf_counter()
    artifacts = export_run(
        RunOutputs(forecast=forecast, validation=reports, dcf=result),
        root=OUTPUT_ROOT,
        run_id=run_id,
        meta={
            # ── 框架层独有的信息 ──────────────────────────────────
            # 哪一版代码算的
            "code": trace.git,
            # 读了哪些输入文件（含哈希，可核验）
            "inputs": trace.input_hashes,
            # 每一步的名字、状态、耗时
            "steps": trace.step_summary,
            # ── 这次运行用到的参数快照 ────────────────────────────
            # 输出层的产物文件只放「结果」，不放假设。所以假设、基期数据、
            # WACC 的完整构成都记在这里 —— 报告里要引用假设和 WACC 构成时，
            # 从这一处拿，不用回去翻配置文件（配置文件日后会变）。
            "run_parameters": {
                "company": {
                    "name": company.name,
                    "code": company.code,
                    "source": company.source,
                },
                "assumptions": asdict(drivers),
                "opening_balance": asdict(opening),
                "wacc": wacc_summary,
            },
        },
    )
    trace.record(
        step="write_outputs",
        tool="algorithm.output.export_run",
        duration_s=time.perf_counter() - started,
        outputs=sorted(str(path) for path in artifacts.files.values()),
    )

    manifest = trace.finish()

    # 打印运行摘要，让人一眼看到这次跑出了什么
    first_year = drivers.base_year + 1
    last_year = drivers.base_year + drivers.years
    print(f"公司      {company.name}（{company.code}）")
    print(f"预测期    {first_year} ~ {last_year}，共 {drivers.years} 年")
    print(f"基期平衡差额  {opening.balance_gap:,.6f}")
    print(f"预测期最大平衡差额  {forecast.worst_balance_gap:.2e}")

    print("\n勾稽校验（预测期的三张表）")
    print(f"    {'年份':<8}{'通过':>6}{'告警':>6}{'失败':>6}{'跳过':>6}")
    for report in reports:
        counts = report.summary
        print(
            f"    {report.fiscal_year:<8}{counts['pass']:>6}"
            f"{counts['warning']:>6}{counts['fail']:>6}{counts['skipped']:>6}"
        )
    totals = validation_totals(reports)
    if totals["fail"]:
        print(
            f"    ⚠️ 有 {totals['fail']} 项校验失败 —— 结果照常产出，"
            "但报告里必须说明原因"
        )
    if totals["skipped"]:
        print(
            f"    注：{totals['skipped']} 项跳过（输入缺失，无法检查）—— "
            "跳过不等于通过"
        )

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

    # 产物列表由输出层的 summarize 打印（编号、文件名、字节数）
    print()
    print(summarize(artifacts))
    print(f"轨迹        {trace.trace_path}")
    print(f"运行记录    {manifest}")

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
