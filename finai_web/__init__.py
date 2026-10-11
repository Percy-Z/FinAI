"""FinAI 网上取数层。

职责边界：定标公司清单 → 年报 PDF 落盘 → 结构化字段 → 流水线适配
（占位符替换、peers 表来源替换）→ 行情表校验。
"""
from __future__ import annotations

__version__ = "0.1.0"

__all__ = [
    "config",
    "http",
    "targets",
    "cninfo",
    "firecrawl",
    "downloader",
    "schema",
    "extract",
    "adapter",
    "market",
    "cli",
]
