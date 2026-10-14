"""一次「取数 + 估值」任务的编排：工作台点一下，这个进程把重活干完。

为什么是一个独立进程（而不是在工作台里直接跑）
    配置层的路径（假设文件、行情表、可比公司、流水线输入）都是**导入时**读环境变量的
    （`DEFAULT_CONFIG_PATH = _env_path("FINAI_ASSUMPTIONS", ...)`）。工作台是长期运行的
    进程，在同一个进程里换一家公司就得改那批模块级默认值 —— 那是被几百个测试盯着的代码。
    所以：工作台起一个带环境变量的子进程，重活在这个子进程里做，它照旧把进度写进
    `logs/<编号>/trace.jsonl`，界面按现有方式读（前端本来就不硬编码阶段名）。

两个子命令
    plan   解析公司 → 下年报 → 抽取 → 汇总 → 取价 → 生成假设草稿（不估值）
    run    读确认过的草稿 → 写 assumptions.toml（覆盖前备份）→ 跑流水线

    plan 与 run 之间夹着「人确认假设」这一步 —— 那是整个设计里最要紧的一处停顿：
    资本开支口径这类判断会让结论从 +76 元翻到 −10 元，不能由程序替人做。

用法
    python -m pipeline.job plan --company-dir data/companies/000661 --target-code 000661
    python -m pipeline.job run  --company-dir data/companies/000661
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .tracing import RunTrace

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_SOURCE_EXTRACTED = "extracted"

# 环境变量名在这里写成本地常量，**不从 .config 导入** —— 见 _load_gen 的说明：
# 一旦在模块级导入 .config，它的路径默认值就在我们能设环境变量之前定死了。
COMPANY_DIR_ENV = "FINAI_COMPANY_DIR"
DATA_SOURCE_ENV = "FINAI_DATA_SOURCE"
TARGET_ACTUALS_ENV = "FINAI_TARGET_ACTUALS"
ASSUMPTIONS_ENV = "FINAI_ASSUMPTIONS"
MARKET_ENV = "FINAI_MARKET_CSV"
PEERS_ENV = "FINAI_PEERS_CSV"
PIPELINE_INPUT_ENV = "FINAI_PIPELINE_INPUT"
TARGETS_ENV = "FINAI_TARGETS_CSV"

DRAFT_NAME = "assumptions.draft.json"
ASSUMPTIONS_NAME = "assumptions.toml"


class JobError(RuntimeError):
    """任务失败。消息要能直接讲给使用者听，并给出下一步。"""


def new_job_id() -> str:
    """任务编号。与流水线的运行编号同格式（时间戳 + 毫秒），界面对齐进度时好认。"""
    now = datetime.now()
    return now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}"


# ---------------------------------------------------------------------------
# 取数层是仓库里的兄弟包，延迟导入（工作台只能跑估值时不该被它拖累）
# ---------------------------------------------------------------------------


def _ingest():
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from finai_web import adapter, company, downloader, extract, market
    from finai_web import quotes, resolve
    from finai_web import targets as targets_mod

    return {
        "adapter": adapter,
        "company": company,
        "downloader": downloader,
        "extract": extract,
        "market": market,
        "quotes": quotes,
        "resolve": resolve,
        "targets": targets_mod,
    }


def company_env(company_dir: str | Path) -> dict[str, str]:
    """这次任务要带的环境变量：整套输入都指向该公司目录。"""
    base = Path(company_dir).expanduser().resolve()
    return {
        COMPANY_DIR_ENV: str(base),
        ASSUMPTIONS_ENV: str(base / ASSUMPTIONS_NAME),
        MARKET_ENV: str(base / "market.csv"),
        PEERS_ENV: str(base / "peers.csv"),
        PIPELINE_INPUT_ENV: str(base / "extracted" / "pipeline_input.json"),
        TARGETS_ENV: str(base / "targets.csv"),
        DATA_SOURCE_ENV: DATA_SOURCE_EXTRACTED,
    }


def _load_gen(company_dir: Path) -> Any:
    """设好环境变量**之后**才导入配置层。

    踩过的坑（2026-10-15，真跑贵州茅台时撞上）：`pipeline/config.py` 的路径是
    **导入时**求值的。job.py 如果在模块级就 import 它，`os.environ.update` 就来不及了 ——
    估值那一步会去读 `data/extracted/pipeline_input.json`，报「数据源是 extracted
    但抽取结果不存在」。工作台起的子进程恰好因为父进程先把环境传进来而躲过这一枪，
    手工敲 `python -m pipeline.job run` 则必踩。
    """
    os.environ.update(company_env(company_dir))
    from . import assumptions_gen as gen

    return gen


# ---------------------------------------------------------------------------
# 计时 + 记录一步
# ---------------------------------------------------------------------------


def _step(
    trace: RunTrace,
    step: str,
    tool: str,
    action: Callable[[], Any],
    *,
    extra: Mapping[str, Any] | None = None,
) -> Any:
    """跑一步并把结果写进轨迹。失败也记 —— 界面才能说清卡在哪一步。"""
    started = time.time()
    try:
        result = action()
    except Exception as err:  # noqa: BLE001 - 失败要留痕再往上抛
        trace.record(
            step=step,
            tool=tool,
            status="failed",
            duration_s=round(time.time() - started, 3),
            extra={**(dict(extra or {})), "error": f"{type(err).__name__}: {err}"},
        )
        raise
    produced: list[Path] = []
    if isinstance(result, Path):
        produced = [result]
    elif isinstance(result, (list, tuple)):
        produced = [Path(item) for item in result if isinstance(item, (str, Path))]
    trace.record(
        step=step,
        tool=tool,
        duration_s=round(time.time() - started, 3),
        outputs=produced,
        extra=dict(extra or {}),
    )
    return result


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def _pick_candidate(found: Sequence[Any], wanted: str, module: Mapping[str, Any]) -> Any:
    """从候选里定一家。优先代码完全相等，其次「只有一家」才敢自动选。"""
    alive = module["resolve"].active(found)
    if not alive:
        raise JobError(module["resolve"].hint_for_empty(wanted))
    exact = [item for item in alive if item.code == wanted.strip()]
    if exact:
        return exact[0]
    if len(alive) == 1:
        return alive[0]
    listing = "、".join(f"{item.name}({item.code})" for item in alive[:8])
    raise JobError(
        f"「{wanted}」匹配到多家公司，请用代码指定其中一家：{listing}"
    )


def build_profile(
    company_dir: str | Path,
    *,
    target_keyword: str = "",
    target_code: str = "",
    peer_keywords: Sequence[str] = (),
    transport: Any = None,
) -> Any:
    """定标：目标公司 + 可比公司 → CompanyProfile（还没落盘）。"""
    module = _ingest()
    company = module["company"]
    resolve = module["resolve"]

    wanted_target = target_code.strip() or target_keyword.strip()
    if not wanted_target:
        raise JobError("没有指定目标公司（--target-code 或 --target）")
    found = resolve.search(wanted_target, transport=transport)
    target = _pick_candidate(found, wanted_target, module)

    # 目标公司默认下最近两份年报：当年那份给基期资产负债表，
    # 上一份给利润表 —— 「盈利能力归一到上一个正常年份」要靠它。
    latest = _latest_disclosure_year(target, module, transport)
    target_ref = company.CompanyRef.from_candidate(
        target, years=company.default_years(latest, 2)
    )

    peers = []
    for keyword in peer_keywords:
        text = str(keyword).strip()
        if not text:
            continue
        if text == target.code:
            continue  # 可比公司里不能有目标公司自己
        found_peer = resolve.search(text, transport=transport)
        peer = _pick_candidate(found_peer, text, module)
        if any(item.code == peer.code for item in peers):
            continue
        peers.append(
            company.CompanyRef.from_candidate(
                peer, years=company.default_years(latest, 1)
            )
        )

    return company.CompanyProfile(
        target=target_ref,
        peers=peers,
        resolved_from=target_keyword.strip() or target_code.strip(),
        resolved_at=company.now_iso(),
    )


def _latest_disclosure_year(target: Any, module: Mapping[str, Any], transport: Any) -> int:
    """这家公司最近一份年报的**披露年份**（定标清单里 years 一直用的就是这个口径）。"""
    cninfo = __import__("finai_web.cninfo", fromlist=["cninfo"])
    items = cninfo.latest_annual_reports(
        code=target.code,
        column=target.cninfo_column,
        org_id=target.org_id,
        transport=transport,
    )
    picked = cninfo.pick_latest(items)
    if picked is None:
        raise JobError(
            f"{target.name or target.code} 在巨潮没有检索到年报正文 —— "
            "可能是新上市公司，或公告类别不在「年度报告」里"
        )
    year = cninfo.disclosure_year(picked)
    if year is None:
        raise JobError(f"认不出「{picked.title}」的披露年份，请手工指定年份")
    return year


def build_market_rows(
    profile: Any,
    balances: Mapping[str, Mapping[str, float]],
    prices: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """行情表的行：价格来自行情源，报表侧字段来自年报抽取结果。

    为什么后四个字段不取自行情源：股本、有息负债、现金、少数股东权益都在年报里，
    抽取层已经给出了。行情表里真正只有终端能给的就剩价格这一列 ——
    `market.py` 顶上那段「只能人工导出」的说明，说的其实是整张表的来源。

    可比公司也要填这几列（相对估值要用它们的市值与企业价值），
    所以传进来的是「每家公司一盘账」，不是只有目标公司那一份。
    """
    rows: list[dict[str, Any]] = []
    for ref in profile.all_refs:
        quote = prices[ref.code]
        opening = balances.get(ref.code) or {}
        shares = opening.get("share_capital")
        debt = opening.get("interest_bearing_debt")
        cash = opening.get("cash")
        minority = opening.get("minority_equity")
        rows.append(
            {
                "code": ref.code,
                "name": ref.name,
                "role": "target" if ref.code == profile.target.code else "peer",
                "price": f"{quote.price:.2f}",
                "diluted_shares": "" if shares is None else int(shares),
                "interest_bearing_debt": f"{float(debt or 0):.2f}",
                "cash": f"{float(cash or 0):.2f}",
                "minority_interest": f"{float(minority or 0):.2f}",
                "preferred_stock": 0,
                "non_operating_assets": 0,
                "as_of": quote.as_of,
            }
        )
    return rows


def plan(
    company_dir: str | Path,
    *,
    target_keyword: str = "",
    target_code: str = "",
    peer_keywords: Sequence[str] = (),
    as_of: str = "",
    job_id: str | None = None,
    transport: Any = None,
    force: bool = False,
) -> dict[str, Any]:
    """取数一把梭：定标 → 下载 → 抽取 → 汇总 → 取价 → 假设草稿。"""
    module = _ingest()
    company = module["company"]
    base = Path(company_dir).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    gen = _load_gen(base)

    trace = RunTrace(job_id or new_job_id())
    profile: Any = None
    summary: dict[str, Any] = {"job_id": trace.run_id, "company_dir": str(base)}

    # ── ① 定标 ───────────────────────────────────────────────────
    def stage_resolve() -> list[Path]:
        nonlocal profile
        company_json = base / "company.json"
        if target_keyword or target_code:
            profile = build_profile(
                base,
                target_keyword=target_keyword,
                target_code=target_code,
                peer_keywords=peer_keywords,
                transport=transport,
            )
        elif company_json.is_file():
            profile = company.CompanyProfile.load(company_json)
        else:
            raise JobError(
                "这家公司还没有定标清单 —— 请先在界面上输入公司名称，"
                f"或用 --target-code 指定（清单文件：{company_json}）"
            )
        paths = [profile_save(profile, company_json)]
        paths.append(profile.write_targets_csv(base / "targets.csv"))
        summary["target"] = profile.target.to_dict()
        summary["peers"] = [ref.to_dict() for ref in profile.peers]
        return paths

    def profile_save(item: Any, path: Path) -> Path:
        return item.save(path)

    _step(trace, "ingest_resolve", "finai_web.resolve", stage_resolve)

    # ── ② 下载年报 ────────────────────────────────────────────────
    targets = [ref.to_target() for ref in profile.all_refs]
    raw_dir = base / "raw_pdf"

    def stage_fetch() -> list[Path]:
        outcomes = module["downloader"].fetch_all(
            targets, force=force, out_dir=raw_dir, transport=transport
        )
        failed = [item for item in outcomes if not item.ok]
        summary["downloads"] = [item.to_dict() for item in outcomes]
        if failed:
            first = failed[0]
            raise JobError(
                f"{len(failed)} 份年报没下成功，第一份是 {first.code} {first.year}：\n"
                f"    {first.note.strip()}"
            )
        module["downloader"].save_manifest(outcomes, base / "manifest.json")
        return sorted(raw_dir.glob("*.pdf"))

    _step(trace, "ingest_fetch", "finai_web.downloader.fetch_all", stage_fetch)

    # ── ③ 抽取 ───────────────────────────────────────────────────
    extracted_dir = base / "extracted"
    titles_by_code = {item["code"]: item["name"] for item in summary["peers"]}
    names = {profile.target.code: profile.target.name, **titles_by_code}

    def stage_extract() -> list[Path]:
        written: list[Path] = []
        for pdf in sorted(raw_dir.glob("*.pdf")):
            stem_parts = pdf.stem.rsplit("_", 1)
            if len(stem_parts) != 2 or not stem_parts[1].isdigit():
                raise JobError(f"文件名不符合 <代码>_<年份>.pdf：{pdf.name}")
            code, year = stem_parts[0], int(stem_parts[1])
            target_path = extracted_dir / f"{code}_{year}.json"
            if target_path.is_file() and not force:
                written.append(target_path)
                continue
            record = module["extract"].extract_pdf(
                pdf, code=code, name=names.get(code, ""), year=year
            )
            written.append(module["extract"].write_record(record, extracted_dir))
        if not written:
            raise JobError(f"没有可抽取的 PDF（{raw_dir} 是空的）")
        summary["records"] = [item.name for item in written]
        return written

    _step(trace, "ingest_extract", "finai_web.extract.extract_pdf", stage_extract)

    # ── ④ 汇总成流水线输入 ────────────────────────────────────────
    def stage_build() -> Path:
        records = module["extract"].load_records(extracted_dir)
        payload = module["adapter"].build_pipeline_input(
            records, target_code=profile.target.code
        )
        path = module["adapter"].write_pipeline_input(
            payload, extracted_dir / "pipeline_input.json"
        )
        module["adapter"].write_peers_csv(
            payload["peers"].values(), extracted_dir / "peers.csv"
        )
        if not payload["peers"]:
            raise JobError("没有可比公司 —— 相对估值做不了，请在界面上至少选一家")
        summary["pipeline_input"] = str(path)
        return path

    pipeline_input_path = _step(
        trace, "ingest_build", "finai_web.adapter.build_pipeline_input", stage_build
    )
    payload = module["adapter"].read_pipeline_input(pipeline_input_path)
    # 每家公司一盘账：行情表要用它们的股本、有息负债、现金与少数股东权益。
    # 可比公司的那几项也要 —— 相对估值要算它们的市值与企业价值，不能只填目标公司。
    balances = {
        record["code"]: module["adapter"].opening_balance_from(record)
        for record in module["extract"].load_records(extracted_dir)
        if record.get("code")
    }

    # ── ⑤ 取价 + 行情表 ──────────────────────────────────────────
    def stage_market() -> Path:
        prices: dict[str, Any] = {}
        basis = as_of or ""
        for index, ref in enumerate(profile.all_refs):
            quote = module["quotes"].latest_close(
                ref.code, market=ref.market, as_of=basis or None, transport=transport
            )
            if index == 0:
                basis = quote.as_of  # 以目标公司的交易日为基准日，后面的都对齐到同一天
            elif quote.as_of != basis:
                raise JobError(
                    f"{ref.code} 取到的交易日是 {quote.as_of}，与基准日 {basis} 不一致 —— "
                    "停在这里：不同天的价格放进同一张表会让估值口径混乱"
                )
            prices[ref.code] = quote
        summary["prices"] = {code: quote.to_dict() for code, quote in prices.items()}
        summary["as_of"] = basis
        rows = build_market_rows(profile, balances, prices)
        target_quote = prices[profile.target.code]
        single = [
            ref.code
            for ref in profile.all_refs
            if getattr(prices[ref.code], "single_source", False)
        ]
        summary["single_source"] = single
        return module["market"].write_snapshot(
            base / "market.csv",
            rows=rows,
            as_of=basis,
            sources=[
                f"股价    {basis} 收盘价，两个独立来源交叉核对："
                f"{target_quote.source} 与 {target_quote.cross_source}"
                f"（查询时间 {target_quote.queried_at}，两源相差 {target_quote.agreement:.4%}）"
                + (
                    f"；⚠️ {'、'.join(single)} 只取到一个源，没有交叉核对 —— 提交前建议用终端数据核一遍"
                    if single
                    else ""
                ),
                "股数、有息负债、现金、少数股东权益    年报抽取结果（含页码与原文，"
                "见 extracted/pipeline_input.json 的 provenance）",
                "优先股、非经营性资产    年报没有可归入此类的披露，填 0（报告第 9 节说明这一点）",
            ],
        )

    _step(trace, "ingest_market", "finai_web.quotes.latest_close", stage_market)

    # ── ⑥ 假设草稿 ───────────────────────────────────────────────
    def stage_assumptions() -> Path:
        beta = None
        try:
            estimate = module["quotes"].estimate_beta(
                profile.target.code, market=profile.target.market, transport=transport
            )
            beta = estimate.beta
            summary["beta"] = estimate.describe()
            trace.record(
                step="ingest_beta",
                tool="finai_web.quotes.estimate_beta",
                extra={"beta": round(beta, 4), "window": estimate.window_end},
            )
        except module["quotes"].QuoteError as err:
            # beta 算不出来不该让整条取数失败：草稿会把它标成「沿用上一版 / 需人工填」
            summary["beta_error"] = str(err)
        draft = gen.build_draft(base, profile=profile, payload=payload, beta=beta)
        if summary.get("single_source"):
            draft.notes.append(
                "股价只取到一个行情源（"
                + "、".join(summary["single_source"])
                + "），没有交叉核对 —— 报告里的价格属于未核对项"
            )
        summary["draft"] = {
            "counts": {
                status: len(draft.by_status(status))
                for status in (gen.DERIVED, gen.DEFAULT, gen.KEPT, gen.MISSING)
            },
            "missing": gen.missing_fields(draft),
            "normalized": draft.normalized,
            "notes": draft.notes,
        }
        path, _ = gen.default_paths(base)
        return draft.save(path)

    _step(trace, "ingest_assumptions", "pipeline.assumptions_gen.build_draft", stage_assumptions)

    trace.finish(outputs=[base / "market.csv", base / "company.json"])
    summary["trace"] = str(trace.trace_path)
    return summary


def run(
    company_dir: str | Path,
    *,
    run_id: str | None = None,
    transport: Any = None,
) -> tuple[str, Path]:
    """读确认过的假设草稿 → 写 assumptions.toml → 跑流水线。"""
    base = Path(company_dir).expanduser()
    gen = _load_gen(base)
    draft_path, assumptions = gen.default_paths(base)
    if not draft_path.is_file():
        raise JobError(
            f"没有假设草稿：{draft_path}\n    先在界面上跑一次取数（生成草稿），再确认并估值。"
        )
    draft = gen.Draft.load(draft_path)
    gen.require_complete(draft)

    env = company_env(base)
    # 归一命中时才用前瞻口径：基期是业绩低谷年时，拿实际利润乘可比倍数会得出
    # 与 DCF 自相矛盾的结论（长春高新实测：DCF 76 元、相对估值 8 元）。
    env[TARGET_ACTUALS_ENV] = "forward" if draft.normalized.get("applied") else "actual"
    os.environ.update(env)

    written, backup = gen.write_assumptions(draft, assumptions)
    if backup is not None:
        print(f"已备份原假设文件：{backup}", file=sys.stderr)
    print(f"已写入假设文件：{written}", file=sys.stderr)

    # 导入流水线之前再确认一次环境变量已经就位 —— 它的路径默认值同样是导入时求值的
    os.environ.update(company_env(base))
    from . import run as pipeline_run

    identifier, output_dir = pipeline_run.run_pipeline(run_id)
    return identifier, output_dir


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.job",
        description="工作台的取数与估值任务（由 pipeline.web 起子进程调用，也可以手工跑）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="取数 + 生成假设草稿（不估值）")
    p_plan.add_argument("--company-dir", required=True, help="公司目录，例如 data/companies/000661")
    p_plan.add_argument("--target-code", default="", help="选定的目标公司代码")
    p_plan.add_argument("--target", default="", help="用户输入的名称（记进溯源）")
    p_plan.add_argument("--peer", action="append", default=[], help="可比公司：代码或名称，可多次")
    p_plan.add_argument("--as-of", default="", help="估值基准日 YYYY-MM-DD，默认取最近交易日")
    p_plan.add_argument("--job-id", default=None, help="任务编号（界面传进来，用来对齐进度）")
    p_plan.add_argument("--force", action="store_true", help="重新下载与重新抽取")
    p_plan.set_defaults(func=_cmd_plan)

    p_run = sub.add_parser("run", help="按确认过的草稿跑流水线")
    p_run.add_argument("--company-dir", required=True)
    p_run.add_argument("--run-id", default=None)
    p_run.set_defaults(func=_cmd_run)

    return parser


def _cmd_plan(args: argparse.Namespace) -> int:
    summary = plan(
        args.company_dir,
        target_keyword=args.target,
        target_code=args.target_code,
        peer_keywords=args.peer,
        as_of=args.as_of,
        job_id=args.job_id,
        force=args.force,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    run_id, output_dir = run(args.company_dir, run_id=args.run_id)
    print(f"运行编号：{run_id}\n产物目录：{output_dir}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (JobError, ValueError) as err:
        # DraftError 是 ValueError 的子类 —— 这里不直接引用它，是为了不让模块级
        # 导入配置层（见 _load_gen 的说明）。
        print(f"\n[任务未完成]\n{err}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
