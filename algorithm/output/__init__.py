"""输出层：把算法层的结果落成文件。

与 finai 的关系
    finai 是算法层，本包只依赖它，不反向依赖。所以 finai 里不会出现
    output 的任何东西，层次是单向的。

命名提醒
    本包叫 output（代码），产物写在 algorithm/outputs/（产物），只差一个 s。
"""

from .reporting import (
    DATA_FILES,
    DEFAULT_OUTPUT_ROOT,
    RunArtifacts,
    RunOutputs,
    data_filename,
    export_run,
    make_run_id,
    sha256_of,
    summarize,
    write_json,
)

__all__ = [
    "DATA_FILES",
    "DEFAULT_OUTPUT_ROOT",
    "RunArtifacts",
    "RunOutputs",
    "data_filename",
    "export_run",
    "make_run_id",
    "sha256_of",
    "summarize",
    "write_json",
]
