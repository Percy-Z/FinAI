"""控制台输出编码：不能因为「这个字打不出来」把程序打崩。

为什么单独测这个
----------------
中文 Windows 的代码页是 936（GBK）。输出被重定向到管道或文件时，Python 的
``sys.stdout`` 就按 GBK 编码，而且 ``errors`` 是 ``strict`` —— 打印一个 GBK
编码表里没有的字符会抛 ``UnicodeEncodeError``，异常把流水线直接中断在
**打印那一行**（实测中断点是 pipeline/run.py 里打印告警的那句）。

这个坑只在重定向、被别的程序捕获输出、CI 里出现：真实控制台里 Python 走的是
Windows 控制台 API（按 UTF-8 编码），反而不崩。所以「在自己机器上点着跑没事」
不等于「评审机器上没事」。这里用 ``PYTHONIOENCODING=gbk`` 把那个环境搬进测试。

两条：

    1. ``pipeline.console.safe_console()`` 本身 —— 编不出来的字退化成「?」，不抛异常
    2. 真实入口 ``run.main()`` 在 GBK 下能跑到结束（退出码 0）

第二条由子进程 + 探针脚本跑，为什么要探针见 ``_gbk_probe.py`` 的开头。
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# 本项目控制台输出里真的用到、而 GBK 编不出来的几类字符
SPECIAL = "⚠️ 注意 ✓ ✗ −"


class ConsoleEncodingTest(unittest.TestCase):
    """把「中文 Windows + 输出被重定向」这个环境搬进测试。"""

    def run_in_gbk_console(
        self, args: list[str], timeout: int = 300
    ) -> subprocess.CompletedProcess[str]:
        """在 GBK 环境下跑一个子进程并取回它的输出。

        ``PYTHONIOENCODING=gbk`` 正好等于「编码 GBK + errors strict」，
        与现场被重定向时的 sys.stdout 一致，但不要求跑测试的机器真的是中文 Windows。
        """
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "gbk"
        # 这两个会把子进程带偏（UTF-8 模式、数据源），与要测的东西无关
        for name in ("PYTHONUTF8", "FINAI_DATA_SOURCE"):
            env.pop(name, None)
        return subprocess.run(
            args,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            encoding="gbk",  # 子进程按 GBK 输出，就按 GBK 读回来
            errors="replace",
            timeout=timeout,
        )

    def test_编不出来的字退化成问号而不是中断程序(self) -> None:
        """⚠️ / ✓ / ✗ / − 都不在 GBK 里：能显示就显示，显示不了变「?」，但不许抛异常。"""
        script = (
            "from pipeline.console import safe_console\n"
            "safe_console()\n"
            f"print({SPECIAL!r})\n"
            "print('OK')\n"
        )
        done = self.run_in_gbk_console([sys.executable, "-c", script])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("OK", done.stdout)
        # 中文在 GBK 里编得出来，不该被一起换掉
        self.assertIn("注意", done.stdout)

    def test_真实入口在_gbk_控制台下能跑到结束(self) -> None:
        """端到端：真跑一遍 run.main()，必须退出码 0，而不是断在打印告警那一行。"""
        done = self.run_in_gbk_console(
            [sys.executable, "-m", "pipeline.tests._gbk_probe"]
        )
        self.assertEqual(done.returncode, 0, done.stdout[-4000:] + done.stderr[-4000:])
        self.assertIn("运行编号", done.stdout)


if __name__ == "__main__":
    unittest.main()
