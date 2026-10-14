"""命令行入口：`python -m finai_web <子命令>`。

子命令对应取数链路的每一段，可以分开跑，也可以一把梭：

    check          环境自检（依赖、目录、密钥、清单、行情表）
    targets        看定标公司清单 / 生成模板
    fetch          下年报 PDF → data/raw_pdf/
    extract        抽 PDF → data/extracted/<代码>_<年份>.json
    build          汇总 → data/extracted/pipeline_input.json + data/peers.csv
    market         生成行情表模板 / 校验你从 Wind、Choice 导出的表
    placeholders   列出可以填进模板的占位符与当前取值
    run            fetch + extract + build

设计约定
    1. 每个子命令都有退出码：0 成功、非 0 失败。批量任务里「有失败」也算非 0，
       但**能做的都做完了** —— 失败清单会在最后完整打印。
    2. 只做本地文件读写，网络调用集中在 downloader / firecrawl 两处。
    3. 所有产出落在 data/ 下（已 gitignore），代码本身不写任何业务数据。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Sequence

from . import adapter, cninfo, extract, market, schema
from . import targets as targets_mod
from .config import PATHS, SOURCE_CONFIG, SOURCE_EXTRACTED, firecrawl_api_key
from .downloader import (
    FetchOutcome,
    fetch_all,
    import_local,
    load_manifest,
    pdf_path,
    save_manifest,
)
from .firecrawl import Firecrawl, FirecrawlUnavailable

from pipeline.console import safe_console


def _print_outcome(outcome: FetchOutcome) -> None:
    if outcome.ok:
        detail = f"{outcome.bytes / 1024 / 1024:6.1f} MB  {outcome.method}"
        print(f"  ✓ {outcome.code} {outcome.year}  {detail}")
    else:
        detail = (outcome.note or "失败").splitlines()[0]
        print(f"  ✗ {outcome.code or '?'} {outcome.year or '----'}  {detail}")


def _firecrawl(args: argparse.Namespace) -> Firecrawl | None:
    """按需构造 Firecrawl 客户端。没密钥时返回 None（调用方降级）。"""
    if getattr(args, "no_firecrawl", False):
        return None
    return Firecrawl() if firecrawl_api_key() else None


def _source_url(code: str, year: int) -> str:
    """从 manifest 里找回这份 PDF 的来源 URL —— Firecrawl 兜底要用它。"""
    entry = load_manifest()["entries"].get(f"{code}_{year}")
    return str((entry or {}).get("url") or "")


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    """环境自检。跑之前先看这一屏，比出错后再回头查快得多。"""
    print("FinAI 取数层自检")
    print(f"  Python      {sys.version.split()[0]}")
    print(f"  仓库根目录  {PATHS.root}")
    for module, purpose in (
        ("pdfplumber", "本地 PDF 抽取（首选）"),
        ("pypdf", "本地 PDF 抽取（轻量备选）"),
        ("fitz", "本地 PDF 抽取（PyMuPDF，最快）"),
    ):
        state = "有" if importlib.util.find_spec(module) else "没有"
        print(f"  {module:<12}{state:<6}{purpose}")
    print(f"  Firecrawl   密钥{'已配置' if firecrawl_api_key() else '未配置（只能走本地路径）'}")

    print("\n文件")
    exists = "存在" if PATHS.targets_csv.is_file() else "不存在"
    print(f"  定标清单    {PATHS.targets_csv}  {exists}")
    if PATHS.targets_csv.is_file():
        try:
            items = targets_mod.read_targets(PATHS.targets_csv)
            print(f"              共 {len(items)} 家：{', '.join(item.ticker for item in items)}")
        except targets_mod.TargetError as err:
            print(f"              ⚠️ {err}")
    market_state = "存在" if PATHS.market_csv.is_file() else "不存在"
    print(f"  行情表      {PATHS.market_csv}  {market_state}")
    if PATHS.market_csv.is_file():
        report = market.validate(PATHS.market_csv)
        state = "通过" if report.ok else f"{len(report.errors)} 个问题"
        print(f"              校验：{state}")
    pdf_count = len(list(PATHS.raw_pdf.glob("*.pdf"))) if PATHS.raw_pdf.is_dir() else 0
    print(f"  已下 PDF    {pdf_count} 份")
    print(f"  抽取结果    {len(extract.load_records())} 条记录")
    payload_state = "已生成" if PATHS.pipeline_input.is_file() else "未生成"
    print(f"  流水线输入  {PATHS.pipeline_input}  {payload_state}")
    print(f"\n数据源开关   FINAI_DATA_SOURCE={SOURCE_EXTRACTED} 时改读抽取结果；")
    print(f"             默认（{SOURCE_CONFIG}）仍读 assumptions.toml 与 peers.csv。")
    return 0


def cmd_targets(args: argparse.Namespace) -> int:
    if args.init:
        # 不给路径时写「你自己的清单」config/targets.csv（模板文件保持原样，
        # 免得覆盖掉组员已经填好的模板注释），给了路径就写那个路径。
        if isinstance(args.init, str):
            path = targets_mod.write_example(args.init, overwrite=args.force)
        else:
            path = PATHS.targets_csv
            if path.exists() and not args.force:
                print(f"{path} 已存在，未改动（--force 可覆盖）")
                return 0
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(targets_mod.EXAMPLE, encoding="utf-8")
        print(f"已写出清单：{path}")
        print("打开它，把定标公司填进去（ticker 用六位代码，years 写要抽的年份）。")
        return 0
    items = targets_mod.read_targets(args.file)
    if not items:
        print("清单是空的")
        return 1
    print(f"{'代码':<10}{'简称':<14}{'板块':<8}{'年份':<16}年报直链")
    for item in items:
        years = ",".join(str(year) for year in item.years_desc) or "—"
        link = "有" if item.annual_report_url else "—"
        print(f"{item.ticker:<10}{item.name:<14}{item.cninfo_column or '—':<8}{years:<16}{link}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    client = _firecrawl(args)
    if args.import_files:
        outcomes = import_local(args.import_files)
    else:
        items = targets_mod.read_targets(args.file)
        if args.code:
            wanted = {code.strip() for code in args.code.split(",") if code.strip()}
            items = [item for item in items if item.ticker in wanted]
            if not items:
                print(f"清单里没有这些代码：{sorted(wanted)}")
                return 1
        years = [int(year) for year in args.year.split(",") if year.strip()] if args.year else None
        if args.dry_run:
            for item in items:
                for year in years or item.years_desc:
                    print(f"  计划 {item.ticker} {year} → {pdf_path(item.ticker, year)}")
            return 0
        print(f"下载年报 PDF：{len(items)} 家 → {PATHS.raw_pdf}")
        outcomes = fetch_all(
            items,
            years=years,
            firecrawl=client,
            force=args.force,
            progress=_print_outcome,
        )

    if not args.dry_run:
        print(f"\n清单已更新：{save_manifest(outcomes)}")

    failed = [outcome for outcome in outcomes if not outcome.ok]
    if failed:
        print(f"\n{len(failed)} 项没成功：")
        for outcome in failed:
            print(f"  ✗ {outcome.code or '?'} {outcome.year or '----'}")
            for line in (outcome.note or "").splitlines():
                print(f"      {line}")
        return 1
    print(f"\n全部成功：{len(outcomes)} 份")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    client = _firecrawl(args)
    items = targets_mod.read_targets(args.file, required=False)
    by_code = {item.ticker: item for item in items}

    pdfs = [Path(item) for item in args.pdf] if args.pdf else sorted(PATHS.raw_pdf.glob("*.pdf"))
    if not pdfs:
        print(f"没有找到 PDF：{PATHS.raw_pdf} 是空的。先跑 `python -m finai_web fetch`。")
        return 1

    print(f"抽取 {len(pdfs)} 份 PDF → {PATHS.extracted}")
    failures: list[str] = []
    for path in pdfs:
        parts = path.stem.rsplit("_", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            failures.append(f"{path.name}：文件名不符合 <代码>_<年份>.pdf")
            continue
        code, year = parts[0], int(parts[1])
        target = by_code.get(code)
        name = target.name if target else ""
        try:
            record = extract.extract_pdf(
                path, code=code, name=name, year=year, source_url=_source_url(code, year)
            )
        except extract.ExtractError as err:
            url = _source_url(code, year)
            if client is not None and client.available and url:
                print(f"  ⚠️ {path.name} 本地抽取失败，改用 Firecrawl：{err}")
                try:
                    record = extract.extract_via_firecrawl(client, url, code=code, name=name, year=year)
                except Exception as frr:  # noqa: BLE001 - 兜底失败要看得到原因
                    failures.append(f"{path.name}：{err}；Firecrawl 兜底也失败：{frr}")
                    continue
            else:
                failures.append(f"{path.name}：{err}")
                continue

        written = extract.write_record(record)
        flags: list[str] = []
        if record.get("opening_balance") is None:
            flags.append("基期拼不出")
        if record.get("peer") is None:
            flags.append("可比指标拼不出")
        tail = "  ⚠️ " + "、".join(flags) if flags else ""
        print(
            f"  ✓ {code} {year}  {len(record.get('accounts', {}))} 个科目  "
            f"缺 {len(record.get('missing_accounts', []))} 个{tail}"
        )
        for note in (record.get("notes") or [])[:2]:
            print(f"      {note}")
        print(f"      → {written}")

    if failures:
        print(f"\n{len(failures)} 份没抽成：")
        for item in failures:
            print(f"  ✗ {item}")
        return 1
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    records = extract.load_records()
    if not records:
        print(f"{PATHS.extracted} 下没有抽取记录。先跑 `python -m finai_web extract`。")
        return 1

    target_code = args.target
    if target_code is None:
        target_code = _market_target_code()
        if target_code:
            print(f"目标公司取自 market.csv：{target_code}")

    payload = adapter.build_pipeline_input(records, target_code=target_code)
    path = adapter.write_pipeline_input(payload)
    peers_path = PATHS.peers_csv if args.sync_peers else PATHS.peers_extracted
    peers_written = adapter.write_peers_csv(payload["peers"].values(), peers_path)

    opening = payload["opening_balance"]
    total_assets = sum(
        opening[name]
        for name in (
            "cash",
            "accounts_receivable",
            "inventory",
            "fixed_assets",
            "other_non_current_assets",
        )
    )
    total_liabilities = (
        opening["accounts_payable"]
        + opening["interest_bearing_debt"]
        + opening["other_non_current_liabilities"]
    )
    total_equity = (
        opening["share_capital"] + opening["retained_earnings"] + opening["minority_equity"]
    )
    print("\n基期资产负债表（来源：年报抽取）")
    print(f"  资产总计  {total_assets:>20,.2f}")
    print(f"  负债合计  {total_liabilities:>20,.2f}")
    print(f"  权益合计  {total_equity:>20,.2f}")
    print(f"  平衡差额  {total_assets - total_liabilities - total_equity:>20,.2f}  （应为 0）")
    print(f"\n可比公司 {len(payload['peers'])} 家：{'、'.join(sorted(payload['peers'])) or '—'}")

    differences = adapter.compare_with_toml(opening)
    if differences:
        print("\n与手填的 [opening_balance] 比对（差异 > 1 元）：")
        for line in differences:
            print(f"  {line}")
        print("  差异不代表谁错 —— 但切换数据源前，请确认差的是「年报口径 vs 手填口径」。")
    else:
        print("\n与手填的 [opening_balance] 一致（差异 ≤ 1 元）。")

    print(f"\n已写出：{path}")
    print(f"已写出：{peers_written}")
    if not args.sync_peers:
        print(
            f"（没有动 {PATHS.peers_csv} —— 那是 git 跟踪的手填数据。"
            "想让 config 数据源也用抽取结果，加 --sync-peers，或手工复制过去。）"
        )
    print("\n下一步（用抽取结果跑流水线）：")
    print('  $env:FINAI_DATA_SOURCE="extracted"   # PowerShell；bash 用 export')
    print("  python -m pipeline.run")
    return 0


def _market_target_code() -> str | None:
    try:
        rows = market.read_rows()
    except market.MarketError:
        return None
    for row in rows:
        if (row.get("role") or "").strip().lower() == "target":
            return (row.get("code") or "").strip() or None
    return None


def cmd_market(args: argparse.Namespace) -> int:
    items = targets_mod.read_targets(args.file, required=False)
    if args.template:
        wanted = Path(args.csv) if args.csv else PATHS.market_csv
        if wanted.exists() and not args.force:
            print(f"{wanted} 已存在，未改动（--force 可覆盖）")
        else:
            print(f"已写出行情表模板：{market.write_template(wanted, targets=items, as_of=args.as_of or '')}")
        print("把 Wind / Choice 导出的数字填进去，再跑 `python -m finai_web market --check` 校验。")
        return 0

    report = market.validate(args.csv, targets=items)
    print(f"行情表：{report.path}")
    for line in report.errors:
        print(f"  ✗ {line}")
    for line in report.warnings:
        print(f"  ! {line}")
    if report.ok:
        print(
            f"  结论：通过（{len(report.rows)} 行，目标 {report.target.get('code')}，"
            f"可比 {len(report.peers)} 家）"
        )
        return 0
    print(f"  结论：不通过（{len(report.errors)} 个问题）")
    return 1


def _sync_targets(directory: Path) -> int:
    """按公司目录里的 company.json 重新生成 targets.csv。"""
    from . import company as company_mod

    profile = company_mod.CompanyProfile.load(directory / company_mod.FILENAME)
    path = profile.write_targets_csv(directory / company_mod.TARGETS_NAME)
    print(f"已写出：{path}（{len(profile.all_refs)} 家公司）")
    return 0


def cmd_company(args: argparse.Namespace) -> int:
    """公司目录：迁移老数据集，或把 company.json 同步成 targets.csv。

    为什么需要迁移这一步：真实数据集原先散在两处 —— `data/real-000661/` 放三个输入
    文件，平铺的 `data/raw_pdf`、`data/extracted` 放取数产物。工作台改成按公司目录
    读之后，要把它们收拢到 `data/companies/000661/` 下。

    这是**移动文件**，所以先把清单打印出来，要 `--yes` 才真动手 —— 搬数据的命令
    不该有「敲了就执行」的默认值。
    """
    if args.sync:
        return _sync_targets(Path(args.sync))
    if not args.migrate:
        print("要么给 --migrate <代码>，要么给 --sync <公司目录>")
        return 1

    code = str(args.migrate).strip()
    destination = Path(args.into) if args.into else PATHS.root / "data" / "companies" / code

    # 这套数据集包含哪些公司：定标清单的第一行是目标公司，后面几行是可比公司。
    # 清单不在或第一行不是它，就只迁这一家。
    try:
        items = targets_mod.read_targets(PATHS.targets_csv, required=False)
    except Exception as err:  # noqa: BLE001 - 清单有问题不该挡住迁移本身
        print(f"（读定标清单失败，只迁 {code} 一家：{err}）")
        items = []
    codes = [item.ticker for item in items] if items and items[0].ticker == code else [code]

    moves: list[tuple[Path, Path]] = []
    legacy = PATHS.root / "data" / f"real-{code}"
    for name in ("assumptions.toml", "market.csv", "peers.csv"):
        # 只认 data/real-<代码>/ 这一处。**刻意不看** data/ 下那份同名的文件：
        # 那三个是单元测试的夹具（示例公司），搬走会让测试整片失败，
        # 而且失败原因是「夹具不见了」，最难查。
        candidate = legacy / name
        if candidate.is_file():
            moves.append((candidate, destination / name))
        else:
            print(f"  （没有 {candidate.relative_to(PATHS.root)}，跳过）")
    for item_code in codes:
        for path in sorted(PATHS.raw_pdf.glob(f"{item_code}_*.pdf")):
            moves.append((path, destination / "raw_pdf" / path.name))
        for path in sorted(PATHS.extracted.glob(f"{item_code}_*.json")):
            moves.append((path, destination / "extracted" / path.name))
    for extra in (PATHS.pipeline_input, PATHS.peers_extracted):
        if extra.is_file():
            moves.append((extra, destination / "extracted" / extra.name))

    print(f"把 {code} 这套数据集迁进：{destination}")
    print(f"  包含 {len(codes)} 家公司：{'、'.join(codes)}")
    for source, target in moves:
        print(f"  {source.relative_to(PATHS.root)}  →  {target.relative_to(PATHS.root)}")
    if not moves:
        print("没有找到可迁的文件。")
        return 1
    if not args.yes:
        print("\n加 --yes 才会真的移动（现在什么都没做）。")
        return 1

    destination.mkdir(parents=True, exist_ok=True)
    for source, target in moves:
        if target.exists() and not args.force:
            print(f"  跳过（目标已存在）：{target.relative_to(PATHS.root)}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)

    # 定标清单里的每家公司信息 → company.json（它是「这套数据集是谁」的唯一出处）
    if items:
        from . import company as company_mod

        target_ref = company_mod.CompanyRef(
            code=items[0].ticker,
            name=items[0].name,
            org_id=items[0].org_id,
            market=items[0].market,
            column=items[0].column,
            years=items[0].years,
            annual_report_url=items[0].annual_report_url,
        )
        peers = [
            company_mod.CompanyRef(
                code=item.ticker,
                name=item.name,
                org_id=item.org_id,
                market=item.market,
                column=item.column,
                years=item.years,
                annual_report_url=item.annual_report_url,
            )
            for item in items[1:]
        ]
        profile = company_mod.CompanyProfile(
            target=target_ref,
            peers=peers,
            resolved_from=items[0].name,
            resolved_at=company_mod.now_iso(),
            notes=["由 finai_web company --migrate 从平铺数据集迁移而来"],
        )
        profile.save(destination / company_mod.FILENAME)
        profile.write_targets_csv(destination / company_mod.TARGETS_NAME)
        print(f"\n已写出：{(destination / company_mod.FILENAME).relative_to(PATHS.root)}")

    manifest = Path(args.manifest) if args.manifest else PATHS.manifest_json
    if manifest.is_file():
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
            entries = raw.get("entries") or {}
            if isinstance(entries, dict):
                kept = {
                    key: value
                    for key, value in entries.items()
                    if str((value or {}).get("code") or "") in codes
                }
                if kept:
                    destination.joinpath("manifest.json").write_text(
                        json.dumps(
                            {**raw, "entries": kept}, ensure_ascii=False, indent=2
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    print(f"下载清单也复制了一份（{len(kept)} 条）到公司目录。")
        except (OSError, json.JSONDecodeError) as err:
            print(f"（下载清单没复制成：{err}）")

    print("\n迁移完成。之后这样跑：")
    print(f"  $env:FINAI_COMPANY_DIR = \"{destination.relative_to(PATHS.root)}\"")
    print('  $env:FINAI_DATA_SOURCE = "extracted"')
    print("  python -m pipeline.run")
    return 0


def cmd_placeholders(args: argparse.Namespace) -> int:
    payload: dict = {}
    if PATHS.pipeline_input.is_file():
        payload = adapter.read_pipeline_input()
    values = adapter.placeholders_from(payload) if payload else {}
    for name, origin in schema.PIPELINE_PLACEHOLDERS.items():
        value = values.get(name)
        shown = f"{value:,.2f}" if isinstance(value, float) else "—"
        print(f"[{name}]{'':<{max(0, 26 - len(name))}}{shown:>20}  {origin}")
    if not values:
        print("\n（还没有 pipeline_input.json，所以只有名字没有取值；先跑 build）")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """fetch + extract + build。等价于依次跑三条命令，少敲两次。"""
    failed: list[str] = []
    if cmd_fetch(args) != 0:
        failed.append("fetch")
    if cmd_extract(args) != 0:
        failed.append("extract")
    if cmd_build(args) != 0:
        failed.append("build")
    if failed:
        print(f"\n有环节没通过：{'、'.join(failed)}")
        return 1
    print("\n取数链路跑通。")
    return 0


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def _add_fetch_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--file", default=None, help="定标清单路径")
    parser.add_argument("--code", default=None, help="只处理这些代码，逗号分隔")
    parser.add_argument("--year", default=None, help="只处理这些年份，逗号分隔")
    parser.add_argument("--force", action="store_true", help="已有 PDF 也重新下载")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不动网络")
    parser.add_argument("--import-files", nargs="*", default=None, help="把手工下载的 <代码>_<年份>.pdf 入库")
    parser.add_argument("--no-firecrawl", action="store_true", help="不调用 Firecrawl")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m finai_web",
        description="FinAI 取数层：定标公司 → 年报 PDF → 结构化字段 → 接进流水线",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="环境自检").set_defaults(func=cmd_check)

    p_targets = sub.add_parser("targets", help="查看定标公司清单")
    p_targets.add_argument("--init", nargs="?", const=True, default=None, help="生成清单模板（可给路径）")
    p_targets.add_argument("--force", action="store_true", help="模板已存在时也覆盖")
    p_targets.add_argument("--file", default=None, help="清单路径（默认 config/targets.csv）")
    p_targets.set_defaults(func=cmd_targets)

    p_fetch = sub.add_parser("fetch", help="下载年报 PDF")
    _add_fetch_args(p_fetch)
    p_fetch.set_defaults(func=cmd_fetch)

    p_extract = sub.add_parser("extract", help="抽取 PDF → JSON")
    p_extract.add_argument("--pdf", nargs="*", default=None, help="指定 PDF（默认抽 data/raw_pdf/ 下全部）")
    p_extract.add_argument("--file", default=None, help="定标清单路径（用于取公司名）")
    p_extract.add_argument("--no-firecrawl", action="store_true", help="不调用 Firecrawl 兜底")
    p_extract.add_argument("--firecrawl-only", action="store_true", help="只走 Firecrawl")
    p_extract.set_defaults(func=cmd_extract)

    p_build = sub.add_parser("build", help="汇总成流水线输入")
    p_build.add_argument("--target", default=None, help="目标公司代码（默认取 market.csv 的 target）")
    p_build.add_argument(
        "--sync-peers",
        action="store_true",
        help="同时覆盖 data/peers.csv（默认不覆盖，只写 data/extracted/peers.csv）",
    )
    p_build.set_defaults(func=cmd_build)

    p_market = sub.add_parser("market", help="行情表模板与校验")
    p_market.add_argument("--template", action="store_true", help="生成模板")
    p_market.add_argument("--check", action="store_true", help="校验（默认动作）")
    p_market.add_argument("--csv", default=None, help="行情表路径（默认 data/market.csv）")
    p_market.add_argument("--as-of", default=None, help="基准日 YYYY-MM-DD")
    p_market.add_argument("--force", action="store_true", help="模板已存在时也覆盖（会盖掉手填的数）")
    p_market.add_argument("--file", default=None, help="定标清单路径")
    p_market.set_defaults(func=cmd_market)

    sub.add_parser("placeholders", help="列出可用占位符").set_defaults(func=cmd_placeholders)

    p_company = sub.add_parser("company", help="公司目录：迁移老数据集 / 同步定标清单")
    p_company.add_argument("--migrate", default="", help="把这个代码的数据集迁进公司目录")
    p_company.add_argument("--sync", default="", help="按公司目录里的 company.json 重生成 targets.csv")
    p_company.add_argument("--into", default="", help="目标目录（默认 data/companies/<代码>）")
    p_company.add_argument("--manifest", default="", help="下载清单的路径（默认 data/manifest.json）")
    p_company.add_argument("--yes", action="store_true", help="确认执行移动（不加就只打印清单）")
    p_company.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    p_company.set_defaults(func=cmd_company)

    p_run = sub.add_parser("run", help="fetch + extract + build")
    _add_fetch_args(p_run)
    p_run.add_argument("--target", default=None, help="目标公司代码")
    p_run.set_defaults(func=cmd_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    safe_console()
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except (
        targets_mod.TargetError,
        market.MarketError,
        adapter.AdapterError,
        extract.ExtractError,
        cninfo.CninfoError,
        FirecrawlUnavailable,
    ) as err:
        print(f"\n[没跑通]\n{err}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
