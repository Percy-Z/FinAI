"""输出层：把算法层的结果落成文件。

与 finai 的关系
    finai 是算法层，本包只依赖它，不反向依赖。所以 finai 里不会出现
    output 的任何东西，层次是单向的。

命名提醒
    本包 output/ 是代码，产物写在 algorithm/runs/。
    产物目录统一叫 runs/（框架层那边是仓库根的 runs/），不再用 outputs，
    避免「output 是代码、outputs 是产物」只差一个 s 的混淆。
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
from .narrative import (
    NARRATIVE_SECTIONS,
    PROMPT_VERSION,
    NarrativeRequest,
    NarrativeResult,
    build_fact_sheet,
    build_messages,
    parse_sections,
    verify_numbers,
    write_narrative,
)
from .report import (
    REPORT_HTML_NUMBER,
    REPORT_NUMBER,
    Block,
    ReportContext,
    build_blocks,
    render_html,
    render_markdown,
)

__all__ = [
    "Block",
    "DATA_FILES",
    "DEFAULT_OUTPUT_ROOT",
    "NARRATIVE_SECTIONS",
    "PROMPT_VERSION",
    "NarrativeRequest",
    "NarrativeResult",
    "REPORT_HTML_NUMBER",
    "REPORT_NUMBER",
    "ReportContext",
    "RunArtifacts",
    "RunOutputs",
    "build_blocks",
    "build_fact_sheet",
    "build_messages",
    "data_filename",
    "export_run",
    "make_run_id",
    "parse_sections",
    "render_html",
    "render_markdown",
    "sha256_of",
    "summarize",
    "verify_numbers",
    "write_json",
    "write_narrative",
]
