"""命令行入口：把整条流水线包成一个可安装的程序。

为什么要有它
    之前只能用 `python3 -m pipeline.run` —— 这要求使用者在仓库根目录，
    而且得知道包名。装好之后就有 `finai` 这个命令，在任何目录都能用：

        pip install -e .
        finai run

子命令
    run      跑完整流水线（配置 → 计算 → 报告）
    check    验收某次运行的产物是否齐全
    extract  从年报 PDF 抽取关键财务科目
    web      启动本地 Web 工作台（浏览器里一键运行、看进度、看报告）
    mcp      启动 MCP 服务（stdio）
    version  打印版本与依赖可用情况

设计约定
    所有子命令都返回退出码：0 成功、非 0 失败。这样能直接用在 CI 与脚本里。
    参数解析用标准库 argparse，不引入 click 之类的依赖。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

from .console import safe_console

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"


def _print_optional_dependencies() -> None:
    """列出可选依赖的可用情况。排查环境问题时这一行最有用。"""
    checks = (
        ("matplotlib", "六张分析图"),
        ("reportlab", "PDF 后备渲染"),
        ("pdfplumber", "年报抽取"),
    )
    for module, purpose in checks:
        state = "有" if importlib.util.find_spec(module) else "没有"
        print(f"  {module:<12} {state:<4}  {purpose}")
    # LaTeX 不是 Python 包，用 PATH 查
    import shutil

    engine = shutil.which("xelatex") or shutil.which("lualatex")
    print(f"  {'LaTeX':<12} {'有' if engine else '没有':<4}  报告 PDF 首选排版")


def cmd_run(args: argparse.Namespace) -> int:
    from . import run as pipeline_run

    identifier, directory = pipeline_run.run_pipeline(args.run_id)
    print(f"运行完成：{identifier}")
    print(f"产物目录：{directory}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from . import check_run

    directory = (
        check_run.RUNS_ROOT / args.run_id if args.run_id else check_run.newest_run()
    )
    outcome = check_run.check_run(directory, require_charts=not args.allow_missing_charts)
    print(f"验收目录：{directory}")
    for note in outcome.notes:
        print(f"  说明    {note}")
    for warning in outcome.warnings:
        print(f"  警告    {warning}")
    for error in outcome.errors:
        print(f"  不合格  {error}")
    print("结论：" + ("通过" if outcome.ok else f"不通过（{len(outcome.errors)} 个问题）"))
    return 0 if outcome.ok else 1


def cmd_extract(args: argparse.Namespace) -> int:
    from algorithm.finai.extract import extract_financials, format_extraction

    result = extract_financials(args.pdf, accounts=args.account or None)
    print(format_extraction(result))
    return 0 if result.matches else 1


def cmd_mcp(args: argparse.Namespace) -> int:
    from mcp_server.server import serve

    serve()
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from .web import serve

    serve(port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"FinAI {VERSION}")
    print(f"仓库根目录：{REPO_ROOT}")
    print(f"Python {sys.version.split()[0]}")
    print("可选依赖：")
    _print_optional_dependencies()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finai",
        description="自动化估值建模：从财务假设跑到带推理与图表的估值报告",
    )
    parser.add_argument("--version", action="version", version=f"finai {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="跑完整流水线")
    p_run.add_argument("--run-id", default=None, help="指定运行编号；默认按时间戳生成")
    p_run.set_defaults(func=cmd_run)

    p_check = sub.add_parser("check", help="验收某次运行的产物")
    p_check.add_argument("run_id", nargs="?", default=None)
    p_check.add_argument(
        "--allow-missing-charts", action="store_true", help="明确接受没有图表"
    )
    p_check.set_defaults(func=cmd_check)

    p_extract = sub.add_parser("extract", help="从年报 PDF 抽取关键科目")
    p_extract.add_argument("pdf", help="年报 PDF 路径")
    p_extract.add_argument("--account", action="append", help="只抽指定科目，可重复")
    p_extract.set_defaults(func=cmd_extract)

    p_mcp = sub.add_parser("mcp", help="启动 MCP 服务（stdio）")
    p_mcp.set_defaults(func=cmd_mcp)

    p_web = sub.add_parser("web", help="启动本地 Web 工作台")
    p_web.add_argument("--port", type=int, default=8770)
    p_web.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p_web.set_defaults(func=cmd_web)

    p_version = sub.add_parser("version", help="版本与依赖情况")
    p_version.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    safe_console()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
