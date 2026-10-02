"""MCP 服务器的实现。

协议要点（2024-11-05 版）
    JSON-RPC 2.0，stdio 传输，一条消息一行。
    客户端先发 initialize 握手，收到结果后发 notifications/initialized。
    之后 tools/list 拿工具清单、tools/call 调工具。
    通知（没有 id 的消息）不需要回包。

设计原则
    工具返回的是**文本**，不是内部对象 —— 协议只认 content 数组。
    所以每个工具都把自己整理成人能读、模型也能读的文本，
    而不是把 Python 对象 repr 出去。
    工具出错不抛异常到协议层，而是回 isError: true 加一句人话说明 ——
    这样调用方的模型能看到「为什么失败」，而不是收到一个传输层错误。
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "finai-valuation"
SERVER_VERSION = "0.1.0"


def _run_root() -> Path:
    return REPO_ROOT / "runs"


def _newest_run() -> Path | None:
    root = _run_root()
    if not root.exists():
        return None
    candidates = sorted(p for p in root.iterdir() if p.is_dir())
    return candidates[-1] if candidates else None


# ---------------------------------------------------------------------------
# 工具实现。每个函数吃一个参数字典，返回一段文本（失败时抛异常，由调用层包装）。
# ---------------------------------------------------------------------------


def tool_run_pipeline(arguments: dict[str, Any]) -> str:
    """跑完整流水线。这是唯一一个会写文件的工具，耗时也最长（含大模型调用）。"""
    from pipeline import run as pipeline_run

    requested = arguments.get("run_id")
    run_id, directory = pipeline_run.run_pipeline(
        requested if isinstance(requested, str) else None
    )
    return (
        "流水线运行完成。\n"
        f"运行编号：{run_id}\n"
        f"产物目录：{directory}\n"
        f"报告：{directory / '17_report.md'}、{directory / '18_report.html'}"
    )


def tool_get_valuation(arguments: dict[str, Any]) -> str:
    """读某次运行的估值结果。默认最近一次。"""
    run_id = arguments.get("run_id")
    directory = _run_root() / run_id if run_id else _newest_run()
    if directory is None or not directory.exists():
        raise FileNotFoundError(f"找不到运行目录：{directory}")

    valuation_path = directory / "03_valuation.json"
    if not valuation_path.exists():
        raise FileNotFoundError(f"{valuation_path} 不存在，这次运行没有产出估值")
    payload = json.loads(valuation_path.read_text(encoding="utf-8"))

    lines = [
        f"运行编号：{directory.name}",
        f"每股价值（现金流折现）：{payload.get('per_share')} 元",
        f"当前股价：{payload.get('price')} 元",
        f"隐含回报：{payload.get('implied_return'):+.2%}"
        if isinstance(payload.get("implied_return"), (int, float))
        else "隐含回报：—",
        f"企业价值：{payload.get('enterprise_value')} 元",
        f"权益价值：{payload.get('equity_value')} 元",
        f"终值占企业价值：{payload.get('terminal_share_of_ev')}",
    ]
    diagnostics = payload.get("diagnostics") or []
    if diagnostics:
        lines.append("诊断：")
        lines.extend(f"  - {item}" for item in diagnostics)
    return "\n".join(lines)


def tool_list_runs(arguments: dict[str, Any]) -> str:
    """列出最近的运行，并标出产物是否齐全。"""
    limit = int(arguments.get("limit", 5))
    root = _run_root()
    if not root.exists():
        return "还没有跑过流水线。"

    from pipeline.check_run import REQUIRED_CHARTS, REQUIRED_DATA_FILES, check_run

    directories = sorted(p for p in root.iterdir() if p.is_dir())[-limit:]
    lines: list[str] = []
    for directory in reversed(directories):
        charts = len(list((directory / "charts").glob("*.png")))
        outcome = check_run(directory, require_charts=True)
        status = "完整" if outcome.ok else f"不完整（{len(outcome.errors)} 个问题）"
        lines.append(
            f"{directory.name}  数据文件 "
            f"{sum((directory / n).exists() for n in REQUIRED_DATA_FILES)}"
            f"/{len(REQUIRED_DATA_FILES)}  图表 {charts}/{len(REQUIRED_CHARTS)}  "
            f"报告 {'有' if (directory / '17_report.md').exists() else '无'}  {status}"
        )
        if not outcome.ok:
            lines.extend(f"    - {err}" for err in outcome.errors[:3])
    return "\n".join(lines)


def tool_check_run(arguments: dict[str, Any]) -> str:
    """验收某次运行的产物。"""
    from pipeline.check_run import check_run

    run_id = arguments.get("run_id")
    directory = _run_root() / run_id if run_id else _newest_run()
    if directory is None or not directory.exists():
        raise FileNotFoundError(f"找不到运行目录：{directory}")

    outcome = check_run(directory, require_charts=True)
    lines = [f"验收：{directory.name}", "结论：" + ("通过" if outcome.ok else "不通过")]
    lines.extend(f"  说明  {note}" for note in outcome.notes)
    lines.extend(f"  警告  {warn}" for warn in outcome.warnings)
    lines.extend(f"  不合格  {err}" for err in outcome.errors)
    return "\n".join(lines)


def tool_extract_annual_report(arguments: dict[str, Any]) -> str:
    """从年报 PDF 里抽取关键科目，每个数字带页码与原文片段。"""
    from algorithm.finai.extract import extract_financials, format_extraction

    pdf = arguments.get("pdf")
    if not pdf:
        raise ValueError("必须给 pdf 参数：年报文件的路径")
    result = extract_financials(pdf)
    return format_extraction(result)


_RUN_PIPELINE_TOOL: dict[str, Any] = {
    "name": "run_valuation_pipeline",
    "description": (
        "跑完整的自动化估值流水线：读配置 → 三表联动预测 → 勾稽校验 → "
        "资本成本 → 现金流折现 → 敏感性分析 → 反向求解 → 相对估值 → "
        "足球场图 → 调用大模型写报告。耗时约 3 分钟（含大模型）。"
        "返回本次运行的编号与产物目录。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "可选。指定运行编号；不给就自动按时间戳生成。",
            }
        },
    },
}

TOOLS: list[dict[str, Any]] = [
    _RUN_PIPELINE_TOOL,
    {
        "name": "get_valuation",
        "description": "读取某次运行的估值结果（每股价值、企业价值、隐含回报、诊断）。默认读最近一次。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "可选。运行编号。"}
            },
        },
    },
    {
        "name": "list_runs",
        "description": "列出最近几次运行，标出产物是否齐全。用来快速判断上一次跑得怎么样。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "列出几次，默认 5。"}
            },
        },
    },
    {
        "name": "check_run",
        "description": "验收某次运行的产物：数据文件、两份报告、六张图是否齐全，报告有没有引用全部图。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "可选。运行编号。"}
            },
        },
    },
    {
        "name": "extract_annual_report",
        "description": (
            "从年报 PDF 里抽取关键财务科目（营业收入、归母净利润、资产总计等），"
            "每个数字都带页码与原文片段。扫描件（没有文字层）会明确报告抽不出来。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pdf": {"type": "string", "description": "年报 PDF 的绝对路径。"}
            },
            "required": ["pdf"],
        },
    },
]

HANDLERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "run_valuation_pipeline": tool_run_pipeline,
    "get_valuation": tool_get_valuation,
    "list_runs": tool_list_runs,
    "check_run": tool_check_run,
    "extract_annual_report": tool_extract_annual_report,
}


# ---------------------------------------------------------------------------
# 协议层
# ---------------------------------------------------------------------------


class MCPServer:
    """MCP 服务器。把请求处理与读写分开，便于测试时不碰 stdin/stdout。"""

    def __init__(self, handlers: dict[str, Callable[[dict[str, Any]], str]] | None = None):
        self.handlers = handlers or HANDLERS

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """处理一条消息，返回要回的内容；通知类消息返回 None。"""
        method = message.get("method")
        request_id = message.get("id")
        is_notification = "id" not in message

        if is_notification:
            # 通知不需要回包（例如 notifications/initialized）
            return None

        if method == "initialize":
            return self._ok(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    },
                },
            )
        if method == "ping":
            return self._ok(request_id, {})
        if method == "tools/list":
            return self._ok(request_id, {"tools": TOOLS})
        if method == "tools/call":
            return self._call_tool(request_id, message.get("params") or {})
        return self._error(request_id, -32601, f"不支持的方法：{method}")

    def _call_tool(
        self, request_id: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            # 协议要求 name 是字符串；给个明确回包比抛 KeyError 好
            return self._ok(
                request_id,
                {
                    "content": [
                        {"type": "text", "text": "tools/call 缺少字符串类型的 name"}
                    ],
                    "isError": True,
                },
            )
        handler = self.handlers.get(name)
        if handler is None:
            return self._ok(
                request_id,
                {
                    "content": [{"type": "text", "text": f"没有名为 {name} 的工具"}],
                    "isError": True,
                },
            )
        try:
            text = handler(arguments)
        except Exception as err:  # noqa: BLE001 - 工具失败要回给人看，不能中断服务
            detail = traceback.format_exc(limit=3)
            return self._ok(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": f"工具 {name} 执行失败：{err}\n{detail}",
                        }
                    ],
                    "isError": True,
                },
            )
        return self._ok(
            request_id,
            {"content": [{"type": "text", "text": text}], "isError": False},
        )

    @staticmethod
    def _ok(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


def serve(server: MCPServer | None = None) -> None:
    """stdio 主循环：一行一条 JSON-RPC 消息。

    注意两件事：
        1. stdout 只用来发协议消息。任何调试打印都会污染协议，
           所以日志一律走 stderr。
        2. 单条消息出错不能让服务挂掉 —— 记一条 stderr，继续读下一行。
    """
    server = server or MCPServer()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as err:
            print(f"[mcp] 收到非法 JSON：{err}", file=sys.stderr)
            continue
        try:
            reply = server.handle(message)
        except Exception:  # noqa: BLE001 - 兜底，保证服务不因单条消息退出
            print(f"[mcp] 处理消息失败：\n{traceback.format_exc()}", file=sys.stderr)
            continue
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    serve()
