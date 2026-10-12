"""LaTeX 排版层：字体集按平台选、引擎找得到。

踩过的两个坑（2026-10-12）
------------------------
1. 字体集写死了 ``fontset=macnew``。这份 preamble 最早在 macOS 上写，macnew 在那里
   正好把正文映到宋体、标题映到黑体；搬到 Windows 上编译时 fontspec 直接报
   「字体找不到」。而 LaTeX 那条路失败后代码会**静默退回 reportlab** ——
   PDF 还在，排版却换了，不长眼睛看不出来。

2. 引擎查找只看 PATH。MiKTeX 装完只把 bin 目录写进用户 PATH，**已经开着的终端**
   看不到它；从那种终端启动工作台，LaTeX 又「消失」了，报告照样悄悄降级。

所以这里把两件事都钉住：平台到字体集的对应，以及 PATH 之外还要认哪些安装位置。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from algorithm.output import latex


def _context() -> latex.ReportContext:
    """最小的报告上下文：抬头那四个字段是必填的，其余都有默认值。"""
    return latex.ReportContext(
        run_id="20261012T120000000",
        created_at="2026-10-12 12:00",
        company_name="示例公司",
        company_code="T",
    )


class TestFontset(unittest.TestCase):
    def test_字体集按平台选(self) -> None:
        for platform, expected in (
            ("darwin", "macnew"),
            ("win32", "windows"),
            ("linux", "fandol"),
        ):
            with self.subTest(platform=platform), mock.patch.object(
                sys, "platform", platform
            ):
                self.assertEqual(latex.fontset(), expected)

    def test_生成的文档里用的就是选定的字体集(self) -> None:
        """不能只测那个小函数：真正要钉住的是 preamble 里写下来的值。"""
        with mock.patch.object(sys, "platform", "win32"):
            text = latex.render_latex(
                [latex.Block(kind="p", text="正文")], _context(), "标题"
            )
        self.assertIn("fontset=windows", text)
        self.assertNotIn("fontset=macnew", text)


class TestEngineLookup(unittest.TestCase):
    def test_PATH_里找不到时去常见安装位置找(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "xelatex.exe"
            fake.write_text("", encoding="utf-8")
            patched = mock.patch.object(latex, "_INSTALL_DIRS", (Path(tmp),))
            with mock.patch.object(latex.shutil, "which", return_value=None), patched:
                self.assertEqual(latex.find_engine(), str(fake))

    def test_哪里都没有就返回_None(self) -> None:
        patched = mock.patch.object(latex, "_INSTALL_DIRS", ())
        with mock.patch.object(latex.shutil, "which", return_value=None), patched:
            self.assertIsNone(latex.find_engine())

    def test_PATH_里有就不去翻目录(self) -> None:
        with mock.patch.object(latex.shutil, "which", return_value="/usr/bin/xelatex"):
            self.assertEqual(latex.find_engine(), "/usr/bin/xelatex")


if __name__ == "__main__":
    unittest.main()
