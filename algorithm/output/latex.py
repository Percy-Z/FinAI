"""把报告渲染成 LaTeX 并编译成 PDF。

为什么用 LaTeX 而不是继续用 reportlab
    这份报告是要交出去的**报告书**，排版本身也是评分印象的一部分。
    LaTeX 的中文断行、表格（booktabs 三线表）、图表浮动体、页码与目录
    都是排版级别的处理，reportlab 要手工调很多参数才能接近。
    而且 ctexart 已经把中文字体映射处理好了 —— 报告里最常见的
    「中文变方块」在这条路上不会发生。

与其它渲染器的关系
    报告内容先被组装成结构化的块（report.Block），再由三个渲染器分别输出：
    Markdown、HTML、LaTeX。**三者共用同一份块**，所以内容不可能走偏。
    这是 report.py 里那个设计（先排块、再渲染）的直接收益。

依赖与降级
    LaTeX 是可选依赖。找不到 xelatex 时本模块返回 None，调用方退回
    reportlab 渲染器（output/pdf.py），再不行就只出 Markdown 与 HTML。
    流水线在任何一种情况下都能跑完。

字体
    字体集按平台选（见下面的 fontset()）：macOS 用 macnew、Windows 用 windows、
    其它用 fandol。**不能写死一个** —— ctex 的 fontset 不会自动换，写死 macnew
    在 Windows 上会直接报「字体找不到」，而且失败后会静默退回 reportlab，
    排版换了却没人发现（2026-10-12 踩过这个坑，测试里钉住了这条）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from .report import Block, ReportContext

LATEX_NUMBER = "19"

# PATH 里找不到引擎时，再按这份清单翻一遍安装目录。
#
# 为什么需要：MiKTeX 装完只把 bin 目录写进**用户 PATH**，而已经开着的终端与进程
# 读的还是旧环境 —— 从那种终端启动工作台，LaTeX 就又找不到了，报告会静默退回
# reportlab（PDF 还在，排版换了，看不出来）。多认几个常见位置，比要求使用者
# 「重启终端」可靠。
_INSTALL_DIRS = (
    Path("/Library/TeX/texbin"),  # macOS（MacTeX）
    Path("/usr/local/bin"),
    Path("/usr/bin"),
    Path.home() / "AppData/Local/Programs/MiKTeX/miktex/bin/x64",  # Windows 用户级
    Path("C:/Program Files/MiKTeX/miktex/bin/x64"),
    Path("C:/Program Files (x86)/MiKTeX/miktex/bin/x64"),
)

# 图注。用文件名当图注太难看，这里按图表编号映射成中文说明；
# 认不出的文件名退回用 alt 文本，不会丢信息。
_FIGURE_TITLES = {
    "11_football_field": "估值区间：各方法的区间与现价位置",
    "12_tornado": "单因素敏感性：各假设对每股价值的影响幅度",
    "13_sensitivity_heatmap": "双因素敏感性：永续增长率与 WACC 的组合",
    "14_peer_multiples": "可比公司倍数对比",
    "15_forecast_trend": "三表预测趋势：收入、利润与现金流",
    "16_validation_summary": "勾稽校验总览：逐年逐项结论",
}

# LaTeX 特殊字符。反斜杠必须第一个换，否则会把后面插入的反斜杠再转义一遍。
_ESCAPES = (
    ("\\", r"\textbackslash{}"),
    ("&", r"\&"),
    ("%", r"\%"),
    ("$", r"\$"),
    ("#", r"\#"),
    ("_", r"\_"),
    ("{", r"\{"),
    ("}", r"\}"),
    ("~", r"\textasciitilde{}"),
    ("^", r"\textasciicircum{}"),
)


def _escape(text: str) -> str:
    out = text
    for source, target in _ESCAPES:
        out = out.replace(source, target)
    return out


def _inline(text: str) -> str:
    """转义 + 还原我们要的加粗。先转义再加标记，顺序不能反。"""
    out = _escape(text)
    # **粗体** 在转义后仍是 **粗体**（星号不在转义表里），可以直接换
    out = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", out)
    out = re.sub(r"`(.+?)`", r"\\texttt{\1}", out)
    return out


def _table(block: Block) -> str:
    """三线表。列数 ≥2 时用 tabularx 让表格占满行宽并自动换行。"""
    header = [str(c) for c in block.header]
    rows = [[str(c) for c in row] for row in block.rows]
    columns = max((len(r) for r in rows), default=len(header))

    if columns <= 1:
        spec = "l"
        env = "tabular"
    else:
        # 第一列按内容宽度，其余等分剩余宽度；X 列会自动换行
        spec = "l" + "X" * (columns - 1)
        env = "tabularx"

    lines = [r"\begin{table}[htbp]", r"\centering\small",
             rf"\begin{{{env}}}{{\linewidth}}{{{spec}}}" if env == "tabularx"
             else rf"\begin{{tabular}}{{{spec}}}",
             r"\toprule",
             " & ".join(_inline(c) for c in header) + r" \\",
             r"\midrule"]
    for row in rows:
        padded = list(row) + [""] * (columns - len(row))
        lines.append(" & ".join(_inline(c) for c in padded) + r" \\")
    lines += [r"\bottomrule",
              rf"\end{{{env}}}",
              r"\end{table}", ""]
    return "\n".join(lines)


def _narrative_box(text: str, badge: str) -> str:
    """大模型写的正文：放在带左边框的浅色框里，并标出「AI 生成」。

    视觉上要和代码产出的事实区分开 —— 这是「区分事实、推论与观点」
    在排版层面的落地。
    """
    body: list[str] = []
    items: list[str] = []

    def flush_items() -> None:
        if items:
            body.append(r"\begin{itemize}[leftmargin=1.2em,itemsep=1pt]")
            body.extend(rf"  \item {item}" for item in items)
            body.append(r"\end{itemize}")
            items.clear()

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            flush_items()
            body.append("")
            continue
        if stripped.startswith(("- ", "* ")):
            items.append(_inline(stripped[2:]))
            continue
        if stripped.startswith("#"):
            flush_items()
            level = min(len(stripped) - len(stripped.lstrip("#")) + 2, 4)
            body.append(
                rf"\paragraph{{{_inline(stripped.lstrip('# ').strip())}}}"
                if level >= 4
                else rf"\textbf{{{_inline(stripped.lstrip('# ').strip())}}}\par"
            )
            continue
        flush_items()
        body.append(_inline(stripped))
        body.append("")
    flush_items()

    content = "\n".join(line for line in body).strip()
    return "\n".join(
        [
            r"\begin{aibox}",
            rf"\textbf{{{_escape(badge)}}}\par\vspace{{4pt}}",
            content,
            r"\end{aibox}",
            "",
        ]
    )


def render_latex(
    blocks: Sequence[Block],
    context: ReportContext,
    title: str,
    footer_note: str = "",
) -> str:
    """把结构化块渲染成一份完整的 LaTeX 文档。"""
    preamble = _PREAMBLE.format(
        fontset=fontset(),
        title=_escape(title),
        company=_escape(context.company_name),
        code=_escape(context.company_code),
        run_id=_escape(context.run_id),
        created=_escape(context.created_at),
        as_of=_escape(context.as_of or "—"),
        source=_escape(context.data_source or "—"),
    )

    body: list[str] = []
    first_section_seen = False
    for block in blocks:
        # 抬头那三个块（标题、元信息表、图例）已经在封面里排过了，跳过
        if block.kind == "h1":
            continue
        if block.kind == "h2":
            name = _inline(block.text)
            body.append(rf"\section{{{name}}}")
            first_section_seen = True
            continue
        if block.kind == "h3":
            body.append(rf"\subsection{{{_inline(block.text)}}}")
            continue
        if not first_section_seen and block.kind == "table":
            # 封面之后的第一个表是元信息表，已经排进封面
            continue
        if block.kind == "p":
            body.append(_inline(block.text) + "\n")
        elif block.kind == "note":
            body.append(rf"\begin{{notebox}}{_inline(block.text)}\end{{notebox}}")
        elif block.kind == "alert":
            body.append(rf"\begin{{alertbox}}{_inline(block.text)}\end{{alertbox}}")
        elif block.kind == "verdict":
            # 结论是全篇的落点，排版上要给足分量：金框 + 明确标记
            badge = (
                rf"\\[2pt]{{\footnotesize\color{{gray}}{_escape(block.badge)}}}"
                if block.badge
                else ""
            )
            body.append(
                "\n".join(
                    [
                        r"\begin{verdictbox}",
                        rf"\textbf{{\color{{finainavy}}【结论】}}\quad "
                        rf"\textbf{{{_inline(block.text)}}}",
                        badge,
                        r"\end{verdictbox}",
                        "",
                    ]
                )
            )
        elif block.kind == "quote":
            body.append(rf"\begin{{legendbox}}{_inline(block.text)}\end{{legendbox}}")
        elif block.kind == "list":
            body.append(r"\begin{itemize}[leftmargin=1.4em,itemsep=2pt]")
            body.extend(rf"  \item {_inline(item)}" for item in block.items)
            body.append(r"\end{itemize}")
        elif block.kind == "table":
            body.append(_table(block))
        elif block.kind == "image":
            # 图片是相对运行目录的路径，编译时 cwd 就是那里。
            # 用带编号的 \caption（论文体例）而不是 \caption* ——
            # 后者在 ctex 下会把星号当正文输出，渲染成「图 3: *」。
            stem = Path(block.src).stem
            caption = _FIGURE_TITLES.get(stem, block.alt)
            body.append(
                "\n".join(
                    [
                        r"\begin{figure}[htbp]",
                        r"  \centering",
                        rf"  \includegraphics[width=\linewidth]{{{_escape(block.src)}}}",
                        rf"  \caption{{{_escape(caption)}}}",
                        r"\end{figure}",
                        "",
                    ]
                )
            )
        elif block.kind == "markdown":
            body.append(_narrative_box(block.markdown, block.badge or "AI 生成"))
        elif block.kind == "hr":
            body.append(r"\vspace{6pt}\hrule\vspace{6pt}")

    closing = rf"\vfill\footnotesize\color{{gray}}{_escape(footer_note)}" if footer_note else ""
    return preamble + "\n".join(body) + "\n" + closing + "\n\\end{document}\n"


_PREAMBLE = r"""% 本文件由 algorithm/output/latex.py 自动生成，请勿手工修改。
% 编译：xelatex 19_report.tex（跑两遍，第二遍是为了目录与交叉引用）
\documentclass[11pt,a4paper,fontset={fontset}]{{ctexart}}

\usepackage[margin=2.2cm,top=2.4cm,bottom=2.4cm]{{geometry}}
\usepackage{{booktabs}}
\usepackage{{tabularx}}
\usepackage{{graphicx}}
\usepackage{{xcolor}}
\usepackage{{tcolorbox}}
\tcbuselibrary{{breakable,skins}}   % breakable 不加载的话报 pgfkeys 找不到键
\usepackage{{enumitem}}
\usepackage{{titlesec}}
\usepackage{{fancyhdr}}
\usepackage{{hyperref}}

\definecolor{{finainavy}}{{HTML}}{{0B1B33}}
\definecolor{{finaigold}}{{HTML}}{{D2A74A}}
\definecolor{{finaisoft}}{{HTML}}{{F7F8FA}}
\definecolor{{finaialert}}{{HTML}}{{8A1F1B}}
\definecolor{{finaialertbg}}{{HTML}}{{FDF2F2}}

% 标题：黑体、深蓝、带金色下划线
\titleformat{{\section}}{{\Large\bfseries\color{{finainavy}}}}{{}}{{0pt}}{{}}[\vspace{{-6pt}}\color{{finaigold}}\rule{{\linewidth}}{{1.2pt}}]
\titlespacing*{{\section}}{{0pt}}{{18pt}}{{10pt}}
\titleformat{{\subsection}}{{\large\bfseries\color{{finainavy}}}}{{}}{{0pt}}{{}}[\vspace{{-4pt}}\color{{finainavy!40}}\rule{{\linewidth}}{{0.5pt}}]
\titlespacing*{{\subsection}}{{0pt}}{{12pt}}{{6pt}}

\setlist{{nosep}}
\linespread{{1.25}}\selectfont

% AI 生成的正文：左侧金色竖条 + 浅底
\newtcolorbox{{aibox}}{{colback=finaisoft, colframe=finaigold, boxrule=0pt,
  leftrule=3pt, arc=1pt, left=8pt, right=8pt, top=6pt, bottom=6pt,
  breakable, enhanced}}
% 不合格横幅：红框，必须一眼看到
\newtcolorbox{{alertbox}}{{colback=finaialertbg, colframe=finaialert, boxrule=0.8pt,
  arc=2pt, left=10pt, right=10pt, top=8pt, bottom=8pt, breakable}}
% 说明与图例：灰底
\newtcolorbox{{notebox}}{{colback=finaisoft, colframe=finaisoft, boxrule=0pt,
  arc=1pt, left=8pt, right=8pt, top=5pt, bottom=5pt, breakable}}
\newtcolorbox{{legendbox}}{{colback=white, colframe=finaisoft!80, boxrule=0.4pt,
  arc=1pt, left=10pt, right=10pt, top=7pt, bottom=7pt, breakable}}
% 结论卡：全篇的落点，用金色边框单独框出来
\newtcolorbox{{verdictbox}}{{colback=finaigold!8, colframe=finaigold, boxrule=1.2pt,
  arc=2pt, left=12pt, right=12pt, top=10pt, bottom=10pt, breakable}}

\pagestyle{{fancy}}
\fancyhf{{}}
\fancyhead[L]{{\small\color{{gray}}{company}（{code}）估值报告}}
\fancyhead[R]{{\small\color{{gray}}{as_of}}}
\fancyfoot[C]{{\small\color{{gray}}\thepage}}
\renewcommand{{\headrulewidth}}{{0.3pt}}

\hypersetup{{colorlinks=true, linkcolor=finainavy, urlcolor=finainavy,
  pdftitle={{{title}}}}}

\begin{{document}}

\begin{{center}}
{{\LARGE\bfseries\color{{finainavy}} {company}（{code}）估值报告}}\\[4pt]
{{\color{{finaigold}}\rule{{0.8\linewidth}}{{1.5pt}}}}
\end{{center}}
\vspace{{6pt}}

\begin{{center}}
\small
\begin{{tabular}}{{ll}}
运行编号 & {run_id} \\
出具时间 & {created} \\
估值基准日 & {as_of} \\
数据来源 & {source} \\
\end{{tabular}}
\end{{center}}

"""


def fontset() -> str:
    """按平台选 ctex 的字体集。

    踩过的坑：这里以前写死 ``fontset=macnew``（在 macOS 上正好把正文映到宋体、
    标题映到黑体），搬到 Windows 上编译时 fontspec 直接报「字体找不到」，
    而 LaTeX 失败后会静默退回 reportlab —— 排版换了却没人发现。

        macOS   macnew   系统自带的宋体/黑体
        Windows windows  宋体 / 黑体（SimSun / SimHei）
        其它    fandol   TeX 发行版自带的 Fandol 系列（Linux 常用）
    """
    if sys.platform == "darwin":
        return "macnew"
    if sys.platform == "win32":
        return "windows"
    return "fandol"


def find_engine() -> str | None:
    """找一个能编中文的引擎。xelatex 优先，lualatex 也行。

    先查 PATH，再查 _INSTALL_DIRS —— 后者是为「装完 MiKTeX 但当前进程还是旧 PATH」
    这种情况准备的。
    """
    names = ("xelatex", "lualatex")
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    for directory in _INSTALL_DIRS:
        for name in names:
            for suffix in (".exe", ""):
                candidate = directory / f"{name}{suffix}"
                if candidate.is_file():
                    return str(candidate)
    return None


def compile_latex(
    tex_path: str | Path, runs: int = 1, timeout: int = 240
) -> tuple[Path | None, list[str]]:
    """编译 .tex，返回 (PDF 路径, 日志行)。失败时 PDF 为 None。

    默认只编一遍：本报告没有目录，也没有 \\ref 交叉引用，第二遍只是重复劳动
    （一遍约 1.5 秒）。**如果以后加了目录或图表编号引用，把 runs 改成 2** ——
    那时第二遍是必需的，页码和引用要靠它才对。
    """
    source = Path(tex_path)
    engine = find_engine()
    if engine is None:
        return None, ["找不到 xelatex / lualatex，无法编译 LaTeX"]

    log: list[str] = []
    for attempt in range(1, runs + 1):
        completed = subprocess.run(
            [engine, "-interaction=nonstopmode", "-halt-on-error", source.name],
            cwd=source.parent,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            # 只留报错附近的几行，整份日志几百行看不完
            interesting = [
                line
                for line in completed.stdout.splitlines()
                if line.startswith("!") or "Error" in line or "error" in line
            ]
            log.append(f"第 {attempt} 遍编译失败（退出码 {completed.returncode}）")
            log.extend(interesting[:6] or ["（日志里没有明显的错误行，看同目录的 .log）"])
            return None, log

    target = source.with_suffix(".pdf")
    if not target.exists():
        return None, ["编译没有报错，但没有产出 PDF"]
    return target, [f"{runs} 遍编译通过，引擎 {Path(engine).name}"]
