"""流水线主流程：读配置 → 三表预测 → 勾稽校验 → 资本成本 → DCF 估值
→ 敏感性分析 → 反向求解 → 相对估值 → 足球场图 → 落盘。

九个阶段。每加一个算法模块，就在这条链上再接一段：

    [config] → [forecast] → [validate] → [wacc] → [valuation]
             → [sensitivity] → [reverse] → [comps] → [football_field]
             → [落盘]

运行：
    cd ~/Desktop/FinAI
    python3 -m pipeline.run

产物（编号由输出层固定，见 algorithm/output/README.md）：
    runs/<run_id>/01_projections.json      三表预测
    runs/<run_id>/02_validation_report.json 勾稽校验（逐年）
    runs/<run_id>/03_valuation.json        DCF 估值与诊断
    runs/<run_id>/04_tornado.csv           单因素敏感性排序
    runs/<run_id>/05_sensitivity.csv       双因素网格
    runs/<run_id>/06_reverse_dcf.json      反解出的市场隐含假设
    runs/<run_id>/07_comps.csv             可比公司倍数表
    runs/<run_id>/08_comps_implied.csv     隐含每股价值
    runs/<run_id>/09_football_field.json   各方法估值区间
    runs/<run_id>/10_run_manifest.json     本次运行的清单与哈希
    runs/<run_id>/charts/*.png             11~16 号图表
    logs/<run_id>/trace.jsonl              逐步执行轨迹
    logs/<run_id>/run_manifest.json        本次运行总清单

其中 run_id 是运行时刻的时间戳，例如 20260928T143012，
这样今天跑的结果不会覆盖昨天结果。

注意「三表预测」与「估值」用的是两套假设对象（ForecastDrivers 与
ForecastAssumptions），中间由 adapters 转换。两条路径算出的 FCFF 必须一致，
有回归测试盯着这件事。
"""

from __future__ import annotations

import time
import contextlib
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from algorithm.finai.comps import (
    CompanyMetrics,
    CompsResult,
    FootballField,
    comps_bands,
    comps_valuation,
    dcf_band,
    football_field,
)
from algorithm.finai.dcf import DCFResult, value_dcf
from algorithm.finai.forecast import (
    BalanceSheetState,
    ForecastDrivers,
    ThreeStatementForecast,
    build_forecast,
    to_snapshots,
)
from algorithm.finai.reverse_dcf import (
    DEFAULT_BOUNDS,
    ReverseResult,
    reverse_solve_many,
)
from algorithm.finai.sensitivity import (
    GridResult,
    TornadoRow,
    Variable,
    tornado,
    two_way_grid,
)
from algorithm.finai.types import ForecastAssumptions, MarketData, WaccInputs
from algorithm.finai.validate import ValidationReport, run_checks
from algorithm.finai.wacc import (
    check_wacc_sanity,
    wacc_from_market,
    weights_from_market,
)
from algorithm.output import (
    NarrativeRequest,
    RunOutputs,
    build_fact_sheet,
    export_run,
    summarize,
    write_narrative,
)

from .adapters import (
    company_to_market_data,
    drivers_to_assumptions,
    fill_peers_from_financials,
    fill_target_from_forecast,
)
from .config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_MARKET_PATH,
    DEFAULT_PEER_PATH,
    ConfigError,
    SensitivityParams,
    WaccParams,
    load_case,
    load_evidence,
    load_market_snapshot,
    load_peer_financials,
    load_report_params,
    load_sensitivity_params,
    load_terminal_params,
    load_wacc_params,
)
from .console import safe_console
from .tracing import RunTrace

# 仓库根目录：本文件在 pipeline/ 下，往上退一级就是 FinAI/
REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = REPO_ROOT / "runs"
CONFIG_PATH = DEFAULT_CONFIG_PATH
MARKET_PATH = DEFAULT_MARKET_PATH
PEER_PATH = DEFAULT_PEER_PATH


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
) -> ThreeStatementForecast:
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


def validation_totals(reports: Sequence[ValidationReport]) -> dict[str, int]:
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


def stage_validate(
    forecast: ThreeStatementForecast, trace: RunTrace
) -> list[ValidationReport]:
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
) -> DCFResult:
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


def sensitivity_variables(
    assumptions: ForecastAssumptions, wacc: float, params: SensitivityParams
) -> list[Variable]:
    """构造龙卷风图要考察的参数列表。

    每个参数的基准值都取自当前生效的假设，浮动幅度来自 [sensitivity]。
    两者分开：基准值只有一个来源（[drivers] / [wacc] / [terminal]），
    幅度是另一个判断，写在配置里以便报告引用。

    ⚠️ 两个逐年字段（收入增速、EBIT 利润率）的基准取第一年的值，而且
    改这两个参数时，apply_variable 是**把整条路径拉平成一个值**再算，
    不是只改第一年。这是估值引擎的广播语义，报告里要写明这一点，
    否则「收入增速 ±3pp」这句话会产生歧义。
    """
    return [
        Variable.absolute("WACC", "wacc", base=wacc, delta=params.wacc_delta),
        Variable.absolute(
            "永续增长率",
            "terminal_growth",
            base=assumptions.terminal_growth,
            delta=params.terminal_growth_delta,
        ),
        Variable.absolute(
            "收入增速",
            "revenue_growth",
            base=assumptions.revenue_growth[0],
            delta=params.revenue_growth_delta,
        ),
        Variable.absolute(
            "EBIT 利润率",
            "ebit_margin",
            base=assumptions.ebit_margin[0],
            delta=params.ebit_margin_delta,
        ),
        Variable.absolute(
            "税率",
            "tax_rate",
            base=assumptions.tax_rate,
            delta=params.tax_rate_delta,
        ),
    ]


def stage_sensitivity(
    assumptions: ForecastAssumptions,
    market: MarketData,
    wacc: float,
    params: SensitivityParams,
    trace: RunTrace,
) -> tuple[list[TornadoRow], GridResult]:
    """阶段：敏感性分析。龙卷风图 + 双因素网格。

    网格固定选「永续增长率 × WACC」，因为这两个参数共同决定终值那部分价值，
    是「这个估值里有多少是赌最后一年」这个问题的标准问法。
    想换成「收入增速 × EBIT 利润率」（回答「价值在增长还是在经营杠杆」）
    只需要改下面两行。
    """
    started = time.perf_counter()

    variables = sensitivity_variables(assumptions, wacc, params)
    rows = tornado(assumptions, market, wacc, variables)

    row_variable = Variable.absolute(
        "永续增长率",
        "terminal_growth",
        base=assumptions.terminal_growth,
        delta=params.terminal_growth_delta,
    )
    col_variable = Variable.absolute(
        "WACC", "wacc", base=wacc, delta=params.wacc_delta
    )
    grid = two_way_grid(
        assumptions, market, wacc, row_variable, col_variable, points=params.grid_points
    )

    low, high = grid.value_range
    trace.record(
        step="sensitivity",
        tool="algorithm.finai.sensitivity.tornado + two_way_grid",
        duration_s=time.perf_counter() - started,
        extra={
            "most_sensitive": rows[0].name if rows else None,
            "swings": {row.name: row.swing for row in rows},
            "grid": f"{grid.row_name} × {grid.col_name}",
            "grid_shape": [len(grid.row_values), len(grid.col_values)],
            "grid_range": [low, high],
        },
    )
    return rows, grid


def reverse_variables(
    assumptions: ForecastAssumptions, wacc: float
) -> list[Variable]:
    """构造要反解的四个参数。

    搜索区间直接用算法模块给的 DEFAULT_BOUNDS，不另设一套 ——
    反解区间如果和模块默认值不一致，同一个问题会有两个答案。
    """
    specs = [
        ("永续增长率", "terminal_growth", assumptions.terminal_growth),
        ("WACC", "wacc", wacc),
        ("收入增速", "revenue_growth", assumptions.revenue_growth[0]),
        ("EBIT 利润率", "ebit_margin", assumptions.ebit_margin[0]),
    ]
    return [
        Variable.bounds(name, field, *DEFAULT_BOUNDS[field], base=base)
        for name, field, base in specs
    ]


def stage_reverse(
    assumptions: ForecastAssumptions,
    market: MarketData,
    wacc: float,
    trace: RunTrace,
) -> tuple[list[ReverseResult], list[str]]:
    """阶段：反向求解。反解「要让每股价值等于现价，各参数得是多少」。

    单个参数解不出来不中断整个流水线：目标价可能落在任何合理区间之外
    （比如股价高到连 50% 的年增速都撑不住）。这时记一条提示、跳过这个
    参数，其余三个照常给结果。整条流水线因为一个参数无解就全废，
    比少一个参数糟糕得多。
    """
    started = time.perf_counter()
    results: list[ReverseResult] = []
    notes: list[str] = []

    for variable in reverse_variables(assumptions, wacc):
        try:
            solved = reverse_solve_many(
                assumptions, market, wacc, [variable]
            )
        except ValueError as err:
            notes.append(f"{variable.name} 无法反解：{err}")
            continue
        results.extend(solved)

    trace.record(
        step="reverse_dcf",
        tool="algorithm.finai.reverse_dcf.reverse_solve_many",
        duration_s=time.perf_counter() - started,
        extra={
            "solved": {
                item.field: {
                    "implied": item.implied_value,
                    "base": item.base_value,
                    "direction": item.direction,
                }
                for item in results
            },
            "skipped": notes,
        },
    )
    return results, notes


def stage_comps(
    target: CompanyMetrics,
    peers: Sequence[CompanyMetrics],
    trace: RunTrace,
) -> CompsResult:
    """阶段：相对估值。

    传进来的 target 与 peers 必须是「财务指标已经填好」的对象：
    目标公司的利润指标来自三表预测（前瞻口径），可比公司来自 peers.csv。
    这一步本身不再补数据，只负责算倍数 —— 补数据是适配层的活。
    """
    started = time.perf_counter()
    result = comps_valuation(target, peers)

    usable = {name: stat.median for name, stat in result.stats.items() if stat.count}
    central = result.central_value if result.medians else None
    trace.record(
        step="comps",
        tool="algorithm.finai.comps.comps_valuation",
        duration_s=time.perf_counter() - started,
        extra={
            "peers": [peer.code for peer in result.peers],
            "median_multiples": usable,
            "central_value": central,
            "implied_return": result.implied_return if central is not None else None,
            "warnings": result.warnings,
        },
    )
    return result


def stage_football_field(
    dcf: DCFResult,
    comps: CompsResult,
    grid: GridResult,
    price: float,
    trace: RunTrace,
) -> FootballField:
    """阶段：足球场图。把各方法的估值区间画在同一张图上。

    DCF 那条横条不是单点，而是取自敏感性网格的上下界 ——
    否则一条线跟另一个点比大小，看不出「区间有没有重叠」这件事。

    重叠检查只做「DCF 对各个可比倍数」。可比倍数之间不互相检查：
    它们本来就是同一类方法，彼此矛盾是常态，不是发现。
    """
    started = time.perf_counter()

    low, high = grid.value_range
    peer_bands = comps_bands(comps)
    bands = [dcf_band(dcf, low=low, high=high)]
    bands.extend(peer_bands)

    field = football_field(
        bands,
        price=price,
        check_pairs=[("DCF", band.name) for band in peer_bands],
    )
    trace.record(
        step="football_field",
        tool="algorithm.finai.comps.football_field",
        duration_s=time.perf_counter() - started,
        extra={
            "bands": {
                band.name: [band.low, band.central, band.high]
                for band in field.bands
            },
            "overall": [field.overall_low, field.overall_high],
            "warnings": field.warnings,
        },
    )
    return field


def run_pipeline(run_id: str | None = None) -> tuple[str, Path]:
    """以编程方式跑一遍流水线，返回 (运行编号, 产物目录)。

    与 main() 的区别有三点，都是为了被别的程序调用：
        1. 不往 stdout 打印 —— stdio 传输的 MCP 服务器里 stdout 是协议通道，
           混进运行摘要把客户端的 JSON 解析搞坏（这里重定向到 stderr）。
        2. 不吞异常 —— 失败了让调用方决定怎么处理。
        3. 返回产物目录，调用方不用自己拼路径。
    """
    identifier = run_id or new_run_id()
    trace = RunTrace(identifier)
    with contextlib.redirect_stdout(sys.stderr):
        _run_once(identifier, trace)
    return identifier, OUTPUT_ROOT / identifier


def stage_narrative(
    outputs: RunOutputs,
    request: NarrativeRequest,
    run_parameters: dict[str, Any],
    trace: RunTrace,
):
    """阶段：叙述生成。把算好的结果交给大模型写成报告正文。

    这个阶段和别的阶段有两处不同：

    一、它的失败是正常情形。没配 API key、网络不通、接口报错、返回不是
        合法 JSON —— 这些都不该中断流水线。所以它永不抛异常，
        只返回一个可能没有内容的 NarrativeResult，由报告层决定怎么呈现。

    二、它不可复现。同样的输入两次跑出来的文字不会完全一样。所以把模型名、
        提示词版本、温度、提示词与响应的哈希、token 用量、耗时全部记进
        日志与产物清单 —— 报告正文没法复现，但「是哪一版写出来的」必须可查。

    事实表（facts）在这里构造并传给大模型。它同时也是回来后做数字校验的
    对照表：报告里凡是不在事实表上的数字，都会被挑出来。
    """
    started = time.perf_counter()
    facts = build_fact_sheet(outputs, context=run_parameters)
    result = write_narrative(facts, request, log_dir=trace.dir)
    trace.record(
        step="narrative",
        tool="algorithm.output.narrative.write_narrative",
        model=result.model,
        prompt_version=result.prompt_version,
        duration_s=time.perf_counter() - started,
        extra={
            "ok": result.ok,
            "sections": sorted(result.sections),
            "notes": result.notes,
            "number_warnings": result.number_warnings[:10],
            "usage": result.usage,
            "prompt_sha256": result.prompt_sha256[:16],
            "response_sha256": result.response_sha256[:16],
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
    sensitivity_params = load_sensitivity_params()
    target, peers = load_market_snapshot()
    peer_financials = load_peer_financials()
    report_params = load_report_params()
    evidence = load_evidence()
    trace.record(
        step="load_config",
        tool="pipeline.config",
        duration_s=time.perf_counter() - started,
        inputs=[CONFIG_PATH, MARKET_PATH, PEER_PATH],
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

    # 阶段六：敏感性分析
    tornado_rows, grid = stage_sensitivity(
        assumptions, market, wacc_summary["value"], sensitivity_params, trace
    )

    # 阶段七：反向求解
    reverse_results, reverse_notes = stage_reverse(
        assumptions, market, wacc_summary["value"], trace
    )

    # 阶段八：相对估值
    #
    # 先把财务指标填进去 —— 这一步不能省：
    #   目标公司  利润类指标取自三表预测第一年（前瞻口径），净资产取基期
    #   可比公司  取自 peers.csv（最近一个完整年度）
    # 市场快照里只有股价股数债务，没有这些，PE / PB / EV-EBITDA 都算不出来。
    filled_target = fill_target_from_forecast(target, forecast)
    filled_peers, peer_notes = fill_peers_from_financials(peers, peer_financials)
    comps_result = stage_comps(filled_target, filled_peers, trace)

    # 阶段九：足球场图
    field = stage_football_field(result, comps_result, grid, market.price, trace)

    # 一次运行的完整产出。后面叙述阶段与落盘阶段都用这一份。
    run_outputs = RunOutputs(
        forecast=forecast,
        validation=reports,
        dcf=result,
        tornado=tornado_rows,
        grid=grid,
        reverse=reverse_results,
        comps=comps_result,
        football_field=field,
    )

    # 清单里的参数快照。叙述阶段要拿它构造事实表，落盘阶段要写进清单，
    # 所以在这里先组装好，两处共用同一份。
    run_parameters = {
        "company": {
            "name": company.name,
            "code": company.code,
            "source": company.source,
        },
        "assumptions": asdict(drivers),
        "opening_balance": asdict(opening),
        "wacc": wacc_summary,
        "sensitivity": asdict(sensitivity_params),
        # 假设依据台账：通知把「假设依据」列为考察点，报告第 4 节要引用它
        "evidence": evidence,
        # 数据缺口与反解失败都记进清单 —— 缺什么比有什么更需要在报告里说明
        "notes": {
            "peers": peer_notes,
            "reverse_skipped": reverse_notes,
        },
    }

    # 阶段十：叙述生成（调用大模型写报告正文）
    narrative = stage_narrative(
        run_outputs, report_params, run_parameters, trace
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
        run_outputs,
        root=OUTPUT_ROOT,
        run_id=run_id,
        narrative=narrative,
        report_meta={"as_of": market.as_of},
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
            # 叙述阶段也要用同一份，所以在上面先组装好了。
            "run_parameters": run_parameters,
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

    print("\n敏感性分析（龙卷风图按影响幅度排序，取前三）")
    for row in tornado_rows[:3]:
        print(
            f"    {row.name:<12}幅度 {row.swing:>7.2f} 元   "
            f"（{row.low:>7.4f} → {row.per_share_at_low:>6.2f}，"
            f"{row.high:>7.4f} → {row.per_share_at_high:>6.2f}）"
        )
    grid_low, grid_high = grid.value_range
    print(
        f"    双因素网格 {grid.row_name} × {grid.col_name}："
        f"估值区间 [{grid_low:,.2f}, {grid_high:,.2f}]"
    )

    print("\n反向 DCF（要让每股价值等于现价，参数得是多少）")
    for item in reverse_results:
        print(
            f"    {item.name:<12}基准 {item.base_value:>8.4f}   "
            f"市场隐含 {item.implied_value:>8.4f}   方向 {item.direction}"
        )
    for note in reverse_notes:
        print(f"    ⚠️ {note}")

    print("\n相对估值（可比公司倍数）")
    for stat in comps_result.stats.values():
        if stat.count == 0:
            print(f"    {stat.label:<12}没有可用样本，跳过")
            continue
        print(
            f"    {stat.label:<12}中位数 {stat.median:>7.2f}   "
            f"（{stat.count} 家，四分位 {stat.q1:.2f} ~ {stat.q3:.2f}）"
        )
    if comps_result.medians:
        comps_low, comps_high = comps_result.value_range
        print(
            f"    隐含区间 [{comps_low:,.2f}, {comps_high:,.2f}]，"
            f"中心值 {comps_result.central_value:,.2f}，"
            f"隐含回报 {comps_result.implied_return:+.2%}"
        )
    for note in peer_notes:
        print(f"    ⚠️ {note}")
    for note in comps_result.warnings:
        print(f"    ⚠️ {note}")

    print("\n足球场图（各方法的估值区间）")
    for band in field.bands:
        print(
            f"    {band.name:<12}[{band.low:>7.2f}, {band.high:>7.2f}]"
            f"   中位 {band.central:>7.2f}"
        )
    print(
        f"    {'总区间':<12}[{field.overall_low:>7.2f}, "
        f"{field.overall_high:>7.2f}]   现价 {field.price:.2f}"
    )
    for note in field.warnings:
        print(f"    ⚠️ {note}")

    print("\n报告叙述（大模型）")
    if narrative.ok:
        print(
            f"    模型 {narrative.model}，写了 {narrative.section_count} 个章节，"
            f"耗时 {narrative.elapsed_s:.1f} 秒"
        )
        usage = narrative.usage or {}
        if usage:
            print(
                f"    token 用量：输入 {usage.get('prompt_tokens', '—')} / "
                f"输出 {usage.get('completion_tokens', '—')}"
            )
        if narrative.number_warnings:
            print(
                f"    ⚠️ 数字校验发现 {len(narrative.number_warnings)} 处"
                "事实表之外的数字："
            )
            for note in narrative.number_warnings[:5]:
                print(f"        {note}")
        else:
            print("    数字校验：未发现事实表之外的数字")
    else:
        print("    未生成叙述（出数据版本报告）：" + "；".join(narrative.notes))

    # 产物列表由输出层的 summarize 打印（编号、文件名、字节数）
    print()
    # 时间构成：想优化耗时的时候，先看时间花在哪 ——
    # 不打印这个的话，「跑得慢」只能靠猜。
    slowest = sorted(
        (step for step in trace.step_summary if step.get("duration_s")),
        key=lambda item: item["duration_s"],
        reverse=True,
    )[:3]
    total = sum(step.get("duration_s") or 0 for step in trace.step_summary)
    if slowest:
        detail = "，".join(
            f"{step['step']} {step['duration_s']:.1f}s" for step in slowest
        )
        print(f"耗时构成  总 {total:.1f}s（前三：{detail}）")
    print(summarize(artifacts))
    print(f"轨迹        {trace.trace_path}")
    print(f"运行记录    {manifest}")

    # 缺图要吼一声。降级本身是对的（不该因为画不了图就中断），
    # 但「悄悄交付一份没有图的报告」是错的 —— 报告是交付物，没图就是不合格。
    if any("matplotlib" in item for item in artifacts.manifest.get("warnings", [])):
        print(
            "\n⚠️ 本次没有生成任何图表（当前解释器找不到 matplotlib），"
            "所以报告里也不会有图。"
        )
        print(
            "   报告没有图就不是合格交付物。换一个装了 matplotlib 的解释器重跑："
            "make run PYTHON=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
        )
        print("   想确认产物是否齐全，跑 make check。")

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
    safe_console()
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
