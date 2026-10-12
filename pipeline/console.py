"""控制台输出的保险：编不出来的字，不该把程序打崩。

为什么需要这个文件
------------------
本项目往控制台打的字里有 ``⚠️ / ✓ / ✗ / −`` 这类 **GBK 编码表里没有**的字符。
中文 Windows 的代码页是 936（GBK），一旦输出被重定向到管道或文件，
Python 的 ``sys.stdout`` 就按 GBK 编码，而且 ``errors`` 是 ``strict`` ——
打印一个编码表里没有的字符会抛 ``UnicodeEncodeError``，异常直接把整条流水线
中断在**打印那一行**，而不是温和地显示成乱码。实测踩到的入口：

    python -m pipeline.run > log.txt              第 846 行打印告警时中断
    python -m finai_web check | more              取数层自检打印 ✓ 时中断
    python finai_web/examples/offline_demo.py     抽完第一家就中断

为什么平时跑不出来：真实控制台里 Python 走的是 Windows 控制台 API（按 UTF-8
编码），反而不崩。这个坑只在重定向、被别的程序捕获输出、以及 CI 里出现 ——
恰恰是「在自己机器上点着跑没事、到评审那边就炸」的那类问题。

做法
----
把 ``sys.stdout`` / ``sys.stderr`` 的 ``errors`` 从 ``strict`` 改成 ``replace``：
目标编码编不出来的字符退化成「?」，其余照常。**不动 encoding** —— 保留控制台
自己的编码，中文在中文 Windows 上才不会变成乱码（改成 UTF-8 会让管道里的
中文在 PowerShell 里显示成乱码，那是把一个坑换成另一个坑）。

为什么不管 ``mcp_server``：它以 stdout 讲 JSON-RPC，动输出层属于协议风险。
"""

from __future__ import annotations

import sys
from typing import Any


def safe_console() -> None:
    """把标准输出的编码错误策略改成「替换」。

    每个命令行入口（``main()``）第一件事就调它，只做一次、不重复调也无害。
    """
    for stream in (sys.stdout, sys.stderr):
        _use_replace_policy(stream)


def _use_replace_policy(stream: Any) -> None:
    """把单个流的 ``errors`` 改成 ``replace``。

    被测试替换过的假流、已经关掉的流、被重定向成不可配置对象的情况，
    都直接跳过 —— 为了「让打印更保险」而让程序崩掉是本末倒置。
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(errors="replace")
    except (ValueError, OSError):
        pass
