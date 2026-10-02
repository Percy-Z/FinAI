"""MCP 服务器：把估值流水线的能力暴露成标准协议接口。

为什么需要它
------------
竞赛通知在「源码应包括…」里点名了 MCP，与编排框架、Tool、Prompt、Skill 并列。
它的实际作用是：把我们的能力从「只能被自己的 main() 调用」变成
「任何支持 MCP 的智能体都能调用」。这才是「智能体系统」该有的接口形状 ——
模型自己决定什么时候调哪个工具，而不是被写死在一条流水线里。

实现范围
--------
标准库实现，不依赖 MCP 官方 SDK。传输走 stdio（一行一个 JSON-RPC 消息），
这是 MCP 最常用的本地传输方式。实现四个方法：
    initialize            握手，声明能力
    tools/list            列出能调的工具
    tools/call            调用某个工具
    ping                  连通性检查

运行：
    python3 -m mcp_server
"""

from .server import MCPServer, PROTOCOL_VERSION, TOOLS, serve

__all__ = ["MCPServer", "PROTOCOL_VERSION", "TOOLS", "serve"]
