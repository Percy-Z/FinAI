"""测试用的断言辅助。

有些结果字段本来就是 `float | None`：勾稽校验的差额、隐含每股价值、反向求解
解出来的数值等等——没算出来的时候它们确实是空的。直接把这些值丢给
`assertAlmostEqual`，类型检查器会说「可能为空」，而且真为空时报错信息也很难懂。

这两个包装把 None 当成断言失败，顺带让类型变窄，所有测试文件统一用它。
"""

from __future__ import annotations

import unittest


def assert_close(
    case: unittest.TestCase,
    actual: float | None,
    expected: float | None,
    places: int = 7,
    msg: str | None = None,
) -> None:
    """断言两个数字足够接近。任一侧是 None 就直接判失败。"""
    if actual is None or expected is None:
        case.fail(msg or f"期望约等于 {expected}，实际是 {actual}")
    case.assertAlmostEqual(actual, expected, places=places, msg=msg)


def assert_not_close(
    case: unittest.TestCase,
    actual: float | None,
    expected: float | None,
    places: int = 7,
    msg: str | None = None,
) -> None:
    """断言两个数字不接近。actual 是 None 时视为通过（本来就没有值可比）。"""
    if actual is None or expected is None:
        return
    case.assertNotAlmostEqual(actual, expected, places=places, msg=msg)
