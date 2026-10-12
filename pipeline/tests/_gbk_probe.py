"""GBK 控制台探针：在「GBK 控制台」里真跑一遍 run.main()。

由 test_console.py 用子进程拉起。文件名以 ``_`` 开头，不符合 unittest 默认的
``test*.py`` 匹配，所以不会被当成用例收集。

为什么不直接 ``python -m pipeline.run``：

    1. 仓库根目录放着 apikey.txt，直接跑会真的去调大模型 —— 三分钟、要联网、
       还可能花钱。测试必须自足，这一点 test_pipeline.py 已经踩过，处理方式一样：
       把 write_narrative 换成桩。
    2. 产物与日志要落进临时目录，不能往 runs/、logs/ 里堆。

除此之外走的都是真实路径：真实的 main()、真实的九个阶段、真实的 print ——
要测的恰恰是「打印告警那一行会不会把程序打断」，所以输出不能改。
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from algorithm.output.narrative import (  # noqa: E402  （要先把仓库根加进 sys.path）
    PROMPT_VERSION,
    NarrativeResult,
)
from pipeline import run  # noqa: E402
from pipeline.tracing import RunTrace  # noqa: E402


def _stub_narrative(
    facts: dict[str, Any], request: Any, log_dir: Any = None
) -> NarrativeResult:
    """假的叙述结果：结构完整、内容为空，等价于「没有 key 时的降级」。"""
    return NarrativeResult(
        ok=False,
        model=request.model,
        endpoint=request.endpoint,
        prompt_version=PROMPT_VERSION,
        temperature=request.temperature,
        notes=["探针：本用例不调用真实大模型接口"],
    )


def trace_factory(log_root: Path) -> Callable[[str], RunTrace]:
    """造一个替代 RunTrace 的工厂，把日志写进临时目录。"""

    def make(run_id: str) -> RunTrace:
        return RunTrace(run_id, log_root=log_root)

    return make


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="finai-gbk-probe-"))
    with mock.patch.object(run, "OUTPUT_ROOT", root / "runs"), mock.patch.object(
        run, "write_narrative", _stub_narrative
    ), mock.patch.object(run, "RunTrace", trace_factory(root / "logs")):
        return run.main()


if __name__ == "__main__":
    raise SystemExit(main())
