"""MCP 服务器的单元测试。

验的是协议层：握手、工具清单、调用、错误处理、通知不回包。
不验真正跑流水线那个工具 —— 那会写文件而且耗时几分钟，
端到端的行为由 pipeline/tests 里的测试覆盖。
"""

import json
import unittest
from typing import Any

from mcp_server.server import PROTOCOL_VERSION, SERVER_NAME, TOOLS, MCPServer


def handle(server: MCPServer, message: dict[str, Any]) -> dict[str, Any]:
    """发一条消息并要求有回包。

    handle() 的返回类型是 dict | None（None 表示这是通知，按协议不回包）。
    测试里绝大多数调用都期待有回包，所以统一在这里断言，
    省得每处都写一遍 —— 也避免类型检查器说「None 不可下标」。
    """
    reply = server.handle(message)
    assert reply is not None, "这条消息应该有回包"
    return reply


class TestProtocol(unittest.TestCase):
    def setUp(self):
        self.server = MCPServer()

    def test_initialize_returns_capabilities(self):
        reply = handle(self.server, 
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        )
        result = reply["result"]
        self.assertEqual(reply["id"], 1)
        self.assertEqual(result["protocolVersion"], PROTOCOL_VERSION)
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], SERVER_NAME)

    def test_notification_gets_no_reply(self):
        """通知没有 id，按协议不能回包。回了会让客户端解析出错。"""
        self.assertIsNone(
            handle(self.server, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_ping(self):
        reply = handle(self.server, {"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(reply["result"], {})

    def test_unknown_method_returns_error_code(self):
        reply = handle(self.server, {"jsonrpc": "2.0", "id": 3, "method": "x/y"})
        self.assertEqual(reply["error"]["code"], -32601)

    def test_tools_list_exposes_schema(self):
        reply = handle(self.server, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        tools = reply["result"]["tools"]
        self.assertEqual(len(tools), len(TOOLS))
        for tool in tools:
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_required_tool_names_present(self):
        names = {tool["name"] for tool in TOOLS}
        for expected in (
            "run_valuation_pipeline",
            "get_valuation",
            "list_runs",
            "check_run",
            "extract_annual_report",
        ):
            self.assertIn(expected, names)


class TestToolCall(unittest.TestCase):
    def test_successful_call_wraps_text(self):
        server = MCPServer(handlers={"echo": lambda args: f"收到 {args.get('x')}"})
        reply = handle(server, 
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"x": 42}},
            }
        )
        result = reply["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(result["content"][0]["text"], "收到 42")

    def test_tool_failure_is_reported_not_raised(self):
        """工具报错要回给调用方的模型看，不能中断服务器。"""

        def boom(args: dict[str, Any]) -> str:
            raise ValueError("参数不对")

        server = MCPServer(handlers={"boom": boom})
        reply = handle(server, 
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "boom", "arguments": {}},
            }
        )
        result = reply["result"]
        self.assertTrue(result["isError"])
        self.assertIn("参数不对", result["content"][0]["text"])

    def test_unknown_tool_is_reported(self):
        reply = handle(MCPServer(), 
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "没有这个工具", "arguments": {}},
            }
        )
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("没有名为", reply["result"]["content"][0]["text"])

    def test_missing_arguments_defaults_to_empty(self):
        server = MCPServer(handlers={"probe": lambda args: json.dumps(args)})
        reply = handle(server, 
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "probe"}}
        )
        self.assertEqual(json.loads(reply["result"]["content"][0]["text"]), {})


class TestRealToolsDoNotCrash(unittest.TestCase):
    """真实工具在缺文件时应该回一句人话，而不是抛栈。"""

    def test_get_valuation_with_bad_run_id(self):
        reply = handle(MCPServer(), 
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "get_valuation",
                    "arguments": {"run_id": "不存在的编号"},
                },
            }
        )
        result = reply["result"]
        self.assertTrue(result["isError"])
        self.assertIn("找不到运行目录", result["content"][0]["text"])

    def test_extract_requires_pdf_argument(self):
        reply = handle(MCPServer(), 
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "extract_annual_report", "arguments": {}},
            }
        )
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("pdf", reply["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
