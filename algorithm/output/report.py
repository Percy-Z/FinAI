"""报告层：把算好的结果组装成一份可交的报告。

三个阶段与它们的边界
    1. 事实（facts）      代码从 RunOutputs 机械抽出，每个数字都有出处
    2. 叙述（narrative）  大模型按事实表写（见 narrative.py），失败就没有
    3. 组装（blocks）     代码把两者排成结构化内容，再渲染成 Markdown / HTML

为什么先排成「块」再渲染，而不是先写 Markdown 再转 HTML
    Markdown 转 HTML 需要写解析器，而解析器要处理大模型写的任意 Markdown，
    出错就会把排版搞乱。改成先构造结构化块：块的种类由我们定死
    （标题 / 段落 / 表格 / 列表 / 引文 / 图片），两个渲染器各写一遍很直白，
    而且大模型给的文字只当纯文本处理，不会被当成标记语言解释 —— 它写
    <script> 出来就是一行普通文字，不是脚本。

标注机制
    竞赛通知要求「明确区分事实、推论与观点」。这块参考了 FinRobot
    （AI4Finance-Foundation，Apache-2.0）在 report_structure.py 里用
    .ai-badge / .data-source 做视觉标注的思路，改成我们自己的三档：
        【事实】年报或市场数据里直接读到的数
        【推论】基于事实推导出来的判断
        【观点】我们的价值判断
    每个章节标题后面标出该章节的内容性质，大模型写的段落单独打徽章。
"""

from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .narrative import NarrativeResult

# 报告在产物目录里的编号。数据文件占 01-10，图表占 11-16，报告接在最后。
REPORT_NUMBER = "17"
REPORT_HTML_NUMBER = "18"

# 内容性质标签
FACT = "【事实】"
INFERENCE = "【推论】"
OPINION = "【观点】"
CONCLUSION = "【结论】"

_DIRECTION_TEXT = {"higher": "更高", "lower": "更低", "same": "相同"}


@dataclass
class Block:
    """报告里的一个内容块。渲染器只认这几种，不做通用 Markdown 解析。"""

    kind: str  # h1 h2 h3 p table list quote image note markdown hr
    text: str = ""
    header: Sequence[str] = field(default_factory=tuple)
    rows: Sequence[Sequence[str]] = field(default_factory=tuple)
    items: Sequence[str] = field(default_factory=tuple)
    alt: str = ""
    src: str = ""
    badge: str = ""
    markdown: str = ""


@dataclass(frozen=True)
class ReportContext:
    """报告的抬头信息与过程记录。这些是元信息，不是分析结论。"""

    run_id: str
    created_at: str
    company_name: str
    company_code: str
    as_of: str = ""
    data_source: str = ""
    # 流水线的执行轨迹（步骤名、状态、耗时），来自日志层。
    # 通知要求「完整记录文件访问、工具调用、计算过程和结果生成情况」，
    # 这些记录落在附录里，是「可追溯」的直接证据。
    steps: Sequence[dict[str, Any]] = field(default_factory=tuple)
    # 读过的输入文件与哈希：(相对路径, 哈希前 12 位)
    inputs: Sequence[tuple[str, str]] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------


def _fmt(value: Any, digits: int = 2) -> str:
    """数字格式化。None 显示为破折号 —— 缺数据与零不是一回事，不能都显示 0。"""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return f"{value:,.{digits}f}"
    return str(value)


def _pct(value: Any) -> str:
    """带正负号的百分比，用于回报率这类有方向的量。"""
    if isinstance(value, (int, float)):
        return f"{value:+.2%}"
    return "—"


def _rate(value: Any, digits: int = 2) -> str:
    """不带符号的百分比，用于利率、税率、权重这类没有方向的量。"""
    if isinstance(value, (int, float)):
        return f"{value:.{digits}%}"
    return "—"


def _gap(value: Any) -> str:
    """资产负债表差额。这个数是浮点运算的残差，量级在 1e-13 左右，
    按小数位显示会变成一串没意义的零，所以小于阈值时直接说「0」。"""
    if not isinstance(value, (int, float)):
        return "—"
    if abs(value) < 1e-9:
        return "0（浮点精度范围内）"
    return f"{value:,.2f}"


def _stance(price: float, low: float, high: float) -> str:
    """现价相对估值区间的位置。这是可复核的算术判断，不是观点 ——
    大模型的结论建立在这个机械判断之上，两者分得开。"""
    if price < low:
        return "低于估值区间下界"
    if price > high:
        return "高于估值区间上界"
    return "落在估值区间之内"


def _verdict_block(
    narrative: NarrativeResult | None,
    dcf: dict[str, Any],
    field_data: dict[str, Any],
) -> Block:
    """生成「最终结论」块。

    为什么要有这一块：分析报告可以通篇是事实与推论，但**必须有一个落点**。
    竞赛要求「涉及估值或投资判断的，应说明主要假设、适用范围和风险因素」——
    判断就是那个落点。

    大模型写出 final_verdict 时用它；没写（或没有 key）时用代码的机械判断兜底：
    现价相对估值区间的位置 + DCF 隐含回报。**结论这一项不能因为缺 key 就消失**，
    那样报告就没有判断了。来源会在括号里标明，读者知道这句是谁下的。
    """
    if narrative is not None and narrative.ok:
        text = narrative.sections.get("final_verdict", "").strip()
        if text:
            return Block(kind="verdict", text=text, badge="AI 生成 · 结论")

    overall = field_data.get("overall") or []
    price = field_data.get("price") or dcf.get("price")
    if len(overall) == 2 and isinstance(price, (int, float)):
        low, high = float(overall[0]), float(overall[1])
        stance = _stance(float(price), low, high)
        ret = dcf.get("implied_return")
        tail = f"，现金流折现的隐含回报 {_pct(ret)}" if isinstance(ret, (int, float)) else ""
        return Block(
            kind="verdict",
            text=(
                f"综合判断：当前股价 {_fmt(price)} 元{stance}，"
                f"估值区间为 {_fmt(low)} ~ {_fmt(high)} 元{tail}。"
                "该判断基于当前假设，假设变更后需重算。"
            ),
            badge="代码判断（未使用大模型）",
        )
    return Block(
        kind="verdict",
        text="本次运行没有可用的估值结果，无法给出结论。",
        badge="代码判断",
    )


def _narrative_block(narrative: NarrativeResult | None, key: str) -> list[Block]:
    """取某章节的叙述。没有就留一句说明，不留空白，也不假装写过。"""
    if narrative is not None and narrative.ok and key in narrative.sections:
        return [
            Block(
                kind="markdown",
                badge="AI 生成 · 推论",
                markdown=narrative.sections[key].strip(),
            )
        ]
    reason = "；".join(narrative.notes) if narrative and narrative.notes else "未启用"
    return [
        Block(kind="note", text=f"本节叙述文字未生成（{reason}）。数据部分不受影响。")
    ]


def _chart_blocks(charts: dict[str, Path], keys: Iterable[str]) -> list[Block]:
    """按顺序插入图表。图不存在就跳过，不留断图。"""
    blocks: list[Block] = []
    for key in keys:
        path = charts.get(key)
        if path is None:
            continue
        blocks.append(
            Block(
                kind="image",
                src=f"charts/{Path(path).name}",
                alt=Path(path).stem,
                badge=Path(path).name,
            )
        )
    return blocks


def _embed(path: Path) -> str:
    """把图片读成 data URI。

    为什么要嵌进去：报告是要交出去的文件。只写相对路径的话，单独把
    18_report.html 发给别人，六个图全断 —— 收到的人只看到破图图标，等于没图。
    嵌成 data URI 之后这一个 HTML 文件本身就是完整报告：双击能看、
    能直接打印成 PDF、发给谁都不丢图。
    """
    try:
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return ""
    suffix = path.suffix.lower().lstrip(".") or "png"
    mime = "image/svg+xml" if suffix == "svg" else f"image/{suffix}"
    return f"data:{mime};base64,{payload}"


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _md_cell(text: str) -> str:
    """表格单元格里的竖线与换行会破坏表格结构，替换掉。"""
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(blocks: Sequence[Block]) -> str:
    """渲染成 Markdown。表格用管道语法，图片用相对路径。"""
    lines: list[str] = []
    for block in blocks:
        if block.kind in ("h1", "h2", "h3"):
            level = int(block.kind[1])
            prefix = "\n" if level > 1 and lines else ""
            lines.append(f"{prefix}{'#' * level} {block.text}\n")
        elif block.kind == "p":
            lines.append(f"{block.text}\n")
        elif block.kind == "note":
            lines.append(f"> _{block.text}_\n")
        elif block.kind == "alert":
            lines.append(f"> ⚠️ **{block.text}**\n")
        elif block.kind == "verdict":
            # 结论要一眼能看到：单独一段，前面加【结论】标记
            lines.append(f"> {CONCLUSION} **{block.text}**\n")
            if block.badge:
                lines.append(f"> _{block.badge}_\n")
        elif block.kind == "quote":
            lines.extend(f"> {line}" for line in block.text.splitlines())
            lines.append("")
        elif block.kind == "list":
            lines.extend(f"- {item}" for item in block.items)
            lines.append("")
        elif block.kind == "table":
            header = "| " + " | ".join(_md_cell(str(c)) for c in block.header) + " |"
            divider = "|" + "|".join(" --- " for _ in block.header) + "|"
            lines.append(header)
            lines.append(divider)
            for row in block.rows:
                lines.append("| " + " | ".join(_md_cell(str(c)) for c in row) + " |")
            lines.append("")
        elif block.kind == "image":
            if block.badge:
                lines.append(f"*{block.badge}*\n")
            lines.append(f"![{block.alt}]({block.src})\n")
        elif block.kind == "markdown":
            if block.badge:
                lines.append(f"> **{block.badge}**\n")
            lines.append(block.markdown)
            lines.append("")
        elif block.kind == "hr":
            lines.append("---\n")
    return "\n".join(lines).rstrip() + "\n"


_CSS = """
:root { --primary:#0B1B33; --accent:#D2A74A; --text:#333333; --muted:#6b7280;
        --line:#e5e7eb; --bg:#ffffff; --soft:#f7f8fa; }
* { box-sizing: border-box; }
body { margin:0; padding:48px 24px 72px; background:var(--soft); color:var(--text);
       font-family:-apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",
       "Helvetica Neue",Arial,sans-serif; line-height:1.75; font-size:15px; }
main { max-width:900px; margin:0 auto; background:var(--bg); padding:56px 60px 72px;
       border-radius:6px; box-shadow:0 1px 3px rgba(0,0,0,.06); }
h1 { font-size:26px; color:var(--primary); border-bottom:3px solid var(--accent);
     padding-bottom:14px; margin:0 0 28px; }
h2 { font-size:19px; color:var(--primary); margin:44px 0 14px; padding-left:12px;
     border-left:4px solid var(--primary); }
h3 { font-size:16px; color:var(--primary); margin:28px 0 10px; }
table { width:100%; border-collapse:collapse; margin:14px 0 20px; font-size:14px; }
th { background:var(--primary); color:#ffffff; text-align:left; padding:9px 12px;
     font-weight:500; }
td { padding:8px 12px; border-bottom:1px solid var(--line); }
tr:nth-child(even) td { background:#fafbfc; }
img { max-width:100%; height:auto; display:block; margin:18px 0 6px;
      border:1px solid var(--line); border-radius:4px; }
/* 预测表有 7 列，窄窗口下会顶出页面。用滚动容器兜住，不挤压字号。 */
.table-wrap { overflow-x:auto; margin:14px 0 20px; }
.table-wrap table { margin:0; }
td, th { word-break:break-word; }
.caption { font-size:13px; color:var(--muted); margin:0 0 22px; }
.ai-badge { display:inline-block; background:var(--accent); color:#ffffff;
            font-size:12px; padding:1px 8px; border-radius:3px; margin-bottom:8px; }
.ai-generated { border-left:3px solid var(--accent); padding:4px 0 4px 16px;
                margin:16px 0; }
.note { color:var(--muted); font-size:13.5px; border-left:2px solid var(--line);
        padding-left:12px; margin:12px 0; }
.alert { border:1px solid #d9534f; background:#fdf2f2; color:#8a1f1b;
         padding:14px 18px; border-radius:4px; margin:18px 0; font-weight:500; }
.verdict { border:1px solid var(--accent); background:color-mix(in srgb, var(--accent) 8%, transparent);
           padding:16px 20px; border-radius:4px; margin:18px 0; }
.verdict .label { font-weight:500; color:var(--primary); }
blockquote { margin:16px 0; padding:12px 18px; background:var(--soft);
             border-left:3px solid var(--line); color:var(--muted); font-size:13.5px; }
footer { max-width:900px; margin:20px auto 0; color:var(--muted); font-size:12.5px;
         text-align:center; }
@media print { body { background:#ffffff; padding:0; } main { box-shadow:none;
    max-width:none; padding:0; } h2 { break-after:avoid; } img,table { break-inside:avoid; } }
"""


def _inline_html(text: str) -> str:
    """行内转换：先转义，再只处理我们认得的加粗。

    先转义后加粗，是为了让大模型写的内容不可能变成可执行的标记 ——
    它写 <script> 出来就是一行普通文字。
    """
    escaped = html.escape(text, quote=False)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)


def _markdown_to_html_fragment(text: str) -> str:
    """把大模型写的 Markdown 转成 HTML。只认标题、无序列表、段落三种，其余当纯文本。"""
    parts: list[str] = []
    pending_list: list[str] = []

    def flush_list() -> None:
        if pending_list:
            items = "".join(f"<li>{item}</li>" for item in pending_list)
            parts.append(f"<ul>{items}</ul>")
            pending_list.clear()

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            flush_list()
            continue
        if stripped.startswith(("- ", "* ")):
            pending_list.append(_inline_html(stripped[2:]))
            continue
        if stripped.startswith("#"):
            flush_list()
            level = min(len(stripped) - len(stripped.lstrip("#")) + 1, 4)
            parts.append(
                f"<h{level}>{_inline_html(stripped.lstrip('# ').strip())}</h{level}>"
            )
            continue
        flush_list()
        parts.append(f"<p>{_inline_html(stripped)}</p>")
    flush_list()
    return "\n".join(parts)


def render_html(
    blocks: Sequence[Block],
    title: str,
    footer_note: str = "",
    base_dir: Path | None = None,
) -> str:
    """渲染成自包含的 HTML。样式与图都内嵌，双击就能看，也能直接打印成 PDF。

    base_dir 用来解析图片的相对路径（通常是这次的运行目录）。
    不给就退回相对路径引用 —— 那样 HTML 就不再自包含，换个位置打开会丢图。
    """
    parts: list[str] = []
    for block in blocks:
        if block.kind in ("h1", "h2", "h3"):
            parts.append(
                f"<{block.kind}>{_inline_html(block.text)}</{block.kind}>"
            )
        elif block.kind == "p":
            parts.append(f"<p>{_inline_html(block.text)}</p>")
        elif block.kind == "note":
            parts.append(f'<p class="note">{_inline_html(block.text)}</p>')
        elif block.kind == "alert":
            parts.append(f'<div class="alert">⚠️ {_inline_html(block.text)}</div>')
        elif block.kind == "verdict":
            badge = (
                f'<div class="text-small text-muted">{_inline_html(block.badge)}</div>'
                if block.badge
                else ""
            )
            parts.append(
                f'<div class="verdict"><span class="label">结论</span>　'
                f"{_inline_html(block.text)}{badge}</div>"
            )
        elif block.kind == "quote":
            parts.append(f"<blockquote>{_inline_html(block.text)}</blockquote>")
        elif block.kind == "list":
            items = "".join(f"<li>{_inline_html(item)}</li>" for item in block.items)
            parts.append(f"<ul>{items}</ul>")
        elif block.kind == "table":
            head = "".join(f"<th>{_inline_html(str(c))}</th>" for c in block.header)
            body = "".join(
                "<tr>"
                + "".join(f"<td>{_inline_html(str(c))}</td>" for c in row)
                + "</tr>"
                for row in block.rows
            )
            parts.append(
                '<div class="table-wrap">'
                f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
                "</div>"
            )
        elif block.kind == "image":
            # 嵌成 data URI，让报告成为自包含文件（图不会因为换地方就断）
            source = Path(block.src)
            if base_dir is not None and not source.is_absolute():
                source = Path(base_dir) / source
            embedded = _embed(source)
            parts.append(
                f'<img src="{html.escape(embedded or block.src, quote=True)}" '
                f'alt="{html.escape(block.alt, quote=True)}">'
            )
            if block.badge:
                parts.append(f'<p class="caption">{_inline_html(block.badge)}</p>')
        elif block.kind == "markdown":
            badge = (
                f'<span class="ai-badge">{html.escape(block.badge)}</span>'
                if block.badge
                else ""
            )
            parts.append(
                f'<div class="ai-generated">{badge}'
                f"{_markdown_to_html_fragment(block.markdown)}</div>"
            )
        elif block.kind == "hr":
            parts.append("<hr>")

    footer = f"<footer>{_inline_html(footer_note)}</footer>" if footer_note else ""
    body = "\n".join(parts)
    return (
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n<style>{_CSS}</style>\n</head>\n"
        f"<body>\n<main>\n{body}\n</main>\n{footer}\n</body>\n</html>\n"
    )


# ---------------------------------------------------------------------------
# 组装：facts + narrative -> blocks
# ---------------------------------------------------------------------------


def build_blocks(
    facts: dict[str, Any],
    narrative: NarrativeResult | None,
    charts: dict[str, Path],
    context: ReportContext,
) -> list[Block]:
    """把事实表与叙述排成完整报告。"""
    blocks: list[Block] = []
    add = blocks.append

    dcf = facts.get("dcf") or {}
    wacc = facts.get("wacc") or {}
    forecast = facts.get("forecast") or {}
    validation = facts.get("validation") or {}
    sensitivity = facts.get("sensitivity") or {}
    comps = facts.get("comps") or {}
    field_data = facts.get("football_field") or {}
    reverse = facts.get("reverse_dcf") or []
    notes = facts.get("notes") or {}

    # ── 抬头 ──────────────────────────────────────────────────────────
    add(Block(kind="h1", text=f"{context.company_name}（{context.company_code}）估值报告"))
    add(
        Block(
            kind="table",
            header=("项目", "内容"),
            rows=(
                ("运行编号", context.run_id),
                ("出具时间", context.created_at),
                ("估值基准日", context.as_of or "—"),
                ("数据来源", context.data_source or "—"),
            ),
        )
    )
    add(
        Block(
            kind="quote",
            text=(
                "内容性质标注："
                f"**{FACT}** 从财务数据或市场快照直接读到的数字；"
                f"**{INFERENCE}** 基于事实推导出来的判断；"
                f"**{OPINION}** 我们的价值判断；"
                f"**{CONCLUSION}** 全篇的落点 —— 把推论收敛成一句可执行的判断。"
                "带「AI 生成」徽章的段落由大模型撰写，写后逐数字校验，"
                "校验结论见附录 10.3。"
            ),
        )
    )

    # 不合格横幅：缺大模型内容或缺图时，必须在最显眼的位置说清楚。
    # 理由很实际 —— 一份「看起来完整、其实没有推理」的报告交上去，
    # 比一份明显有问题的报告更糟：它会被当成成品。
    if narrative is None or not narrative.ok:
        reason = (
            "；".join(narrative.notes)
            if narrative is not None and narrative.notes
            else "未调用大模型"
        )
        add(
            Block(
                kind="alert",
                text=(
                    "本报告不含大模型生成的推理与结论。"
                    f"原因：{reason}。"
                    "竞赛要求「至少使用一个大语言模型作为核心推理引擎」，"
                    "所以这份报告不满足提交要求，只能用于核对数据。"
                ),
            )
        )
    if not charts:
        add(
            Block(
                kind="alert",
                text=(
                    "本报告不含任何图表。原因：本次运行没有渲染图表"
                    "（通常是当前解释器缺少 matplotlib）。"
                    "报告缺少图表即不合格，换一个装了 matplotlib 的解释器重跑。"
                ),
            )
        )

    # ── 1 投资结论 ────────────────────────────────────────────────────
    #
    # 结论先行：这是研究报告的惯例，也是评委最先看的地方。
    # 代码先给出机械判断（现价与区间的位置关系，纯算术），
    # 大模型再在此基础上写判断与理由 —— 两者分开标注，谁说的很清楚。
    add(Block(kind="h2", text=f"1. 投资结论　{INFERENCE}{OPINION}"))
    if field_data.get("overall") and dcf:
        low, high = field_data["overall"]
        price = field_data.get("price")
        if price is None:
            price = dcf.get("price")
        add(
            Block(
                kind="table",
                header=("项目", "结论"),
                rows=(
                    ("估值区间（全部方法）", f"{_fmt(low)} ~ {_fmt(high)} 元"),
                    ("当前股价", f"{_fmt(price)} 元"),
                    (
                        "价格位置",
                        # price 可能缺失（比如只跑了部分阶段），缺了就不给位置判断
                        _stance(float(price), float(low), float(high))
                        if isinstance(price, (int, float))
                        else "—",
                    ),
                    ("现金流折现的隐含回报", _pct(dcf.get("implied_return"))),
                ),
            )
        )
    # 结论卡：先给判断，再讲理由。放在数字表之后、展开分析之前。
    add(_verdict_block(narrative, dcf, field_data))
    blocks.extend(_narrative_block(narrative, "investment_conclusion"))

    # ── 2 估值摘要 ────────────────────────────────────────────────────
    add(Block(kind="h2", text=f"2. 估值摘要　{FACT}{INFERENCE}"))
    if dcf:
        add(
            Block(
                kind="table",
                header=("指标", "数值"),
                rows=(
                    ("每股价值（现金流折现）", f"{_fmt(dcf.get('per_share'))} 元"),
                    ("当前股价", f"{_fmt(dcf.get('price'))} 元"),
                    ("隐含回报", _pct(dcf.get("implied_return"))),
                    ("企业价值", f"{_fmt(dcf.get('enterprise_value'))} 元"),
                    ("权益价值", f"{_fmt(dcf.get('equity_value'))} 元"),
                    ("净债务", f"{_fmt(dcf.get('net_debt'))} 元"),
                    ("终值占企业价值", _rate(dcf.get("terminal_share_of_ev"))),
                    ("隐含退出倍数", f"{_fmt(dcf.get('implied_exit_multiple'))} 倍"),
                ),
            )
        )
    if field_data.get("overall"):
        low, high = field_data["overall"]
        add(
            Block(
                kind="p",
                text=(
                    "把现金流折现与相对估值放在一起，本次估值的总区间为 "
                    f"**{_fmt(low)} ~ {_fmt(high)} 元**，当前股价 "
                    f"{_fmt(field_data.get('price'))} 元。"
                ),
            )
        )
    blocks.extend(_narrative_block(narrative, "executive_summary"))

    # ── 3 公司与预测概览 ──────────────────────────────────────────────
    add(Block(kind="h2", text=f"3. 公司与预测概览　{FACT}"))
    if forecast.get("years"):
        add(
            Block(
                kind="table",
                header=(
                    "年份",
                    "营业收入",
                    "EBIT",
                    "归母净利润",
                    "经营现金流",
                    "期末现金",
                    "FCFF",
                ),
                rows=tuple(
                    (
                        str(year["year"]),
                        _fmt(year["revenue"]),
                        _fmt(year["ebit"]),
                        _fmt(year["net_profit_attributable"]),
                        _fmt(year["operating_cash_flow"]),
                        _fmt(year["cash"]),
                        _fmt(year["fcff"]),
                    )
                    for year in forecast["years"]
                ),
            )
        )
        add(
            Block(
                kind="p",
                text=(
                    "预测期的三张表由代码联动生成，全部年份的资产负债表最大差额为 "
                    f"{_gap(forecast.get('worst_balance_gap'))}。"
                ),
            )
        )
    blocks.extend(_chart_blocks(charts, ("chart_forecast_trend",)))
    blocks.extend(_narrative_block(narrative, "company_overview"))

    # ── 4 假设合理性审查 ──────────────────────────────────────────────
    #
    # 通知里「假设依据」是明写的考察点。这一节分两块：
    #   4.1 依据台账 —— 配置里登记的依据与状态，代码机械列出，可核验
    #   4.2 审查意见 —— 大模型对每条假设的推理（依据够不够、哪条最可疑、
    #        反证条件是什么），标注为推论
    add(Block(kind="h2", text=f"4. 假设合理性审查　{INFERENCE}"))
    evidence = facts.get("evidence") or {}
    if evidence:
        add(Block(kind="h3", text="4.1 假设依据台账"))
        add(Block(kind="p", text="以下依据由数据文件登记，未经大模型加工。"))
        add(
            Block(
                kind="table",
                header=("假设", "依据与状态"),
                rows=tuple((name, text) for name, text in evidence.items()),
            )
        )
    else:
        add(
            Block(
                kind="note",
                text="数据文件里没有登记假设依据（[evidence] 小节为空），"
                "所以这一节只有审查意见、没有依据台账。",
            )
        )
    add(Block(kind="h3", text="4.2 审查意见（大模型推理）"))
    blocks.extend(_narrative_block(narrative, "assumption_review"))

    # ── 5 财务勾稽校验 ────────────────────────────────────────────────
    add(Block(kind="h2", text=f"5. 财务勾稽校验　{FACT}{INFERENCE}"))
    if validation:
        totals = validation.get("totals", {})
        add(
            Block(
                kind="table",
                header=("结论", "条数", "含义"),
                rows=(
                    ("通过", totals.get("pass", 0), "该平的平了"),
                    ("告警", totals.get("warning", 0), "有差异但存在合理解释空间"),
                    ("失败", totals.get("fail", 0), "该平的没平，属于数据错误"),
                    ("跳过", totals.get("skipped", 0), "输入缺失，**不等于通过**"),
                ),
            )
        )
        notable = validation.get("notable") or []
        if notable:
            add(Block(kind="h3", text="需要说明的检查项"))
            add(
                Block(
                    kind="table",
                    header=("年份", "编号", "结论", "检查项", "说明"),
                    rows=tuple(
                        (
                            str(item["year"]),
                            item["check"],
                            item["status"],
                            item["description"],
                            item["note"] or "—",
                        )
                        for item in notable[:20]
                    ),
                )
            )
        else:
            add(Block(kind="p", text="本次校验没有告警、失败或跳过项。"))
    blocks.extend(_chart_blocks(charts, ("chart_validation_summary",)))
    blocks.extend(_narrative_block(narrative, "financial_validation"))

    # ── 6 估值分析 ────────────────────────────────────────────────────
    add(Block(kind="h2", text=f"6. 估值分析　{FACT}{INFERENCE}"))
    add(Block(kind="h3", text="6.1 现金流折现"))
    if dcf:
        add(
            Block(
                kind="table",
                header=("估值桥", "金额（元）"),
                rows=(
                    ("企业价值", _fmt(dcf.get("enterprise_value"))),
                    ("其中：终值现值", _fmt(dcf.get("pv_terminal"))),
                    ("减：净债务", _fmt(dcf.get("net_debt"))),
                    ("权益价值", _fmt(dcf.get("equity_value"))),
                    ("摊薄股数", _fmt(dcf.get("diluted_shares"))),
                    ("每股价值", _fmt(dcf.get("per_share"))),
                ),
            )
        )
    if wacc:
        wacc_rows = [
            (label, _rate(wacc.get(key)))
            for label, key in (
                ("无风险利率", "risk_free"),
                ("股权风险溢价", "equity_risk_premium"),
                ("债务成本", "cost_of_debt"),
                ("税率", "tax_rate"),
                ("股权权重", "equity_weight"),
                ("债务权重", "debt_weight"),
                ("加权平均资本成本", "value"),
            )
            if key in wacc
        ]
        if "beta" in wacc:
            wacc_rows.insert(1, ("贝塔系数", _fmt(wacc.get("beta"))))
        add(
            Block(
                kind="table",
                header=("资本成本构成", "取值"),
                rows=tuple(wacc_rows),
            )
        )

    add(Block(kind="h3", text="6.2 相对估值"))
    multiples = comps.get("multiples") or {}
    if multiples:
        add(
            Block(
                kind="table",
                header=("倍数", "样本数", "下四分位", "中位数", "上四分位"),
                rows=tuple(
                    (
                        label,
                        str(values.get("count", 0)),
                        _fmt(values.get("q1")),
                        _fmt(values.get("median")),
                        _fmt(values.get("q3")),
                    )
                    for label, values in multiples.items()
                ),
            )
        )
    if comps.get("implied_range"):
        low, high = comps["implied_range"]
        add(
            Block(
                kind="p",
                text=(
                    "可比公司倍数推出的隐含每股价值区间为 "
                    f"**{_fmt(low)} ~ {_fmt(high)} 元**，"
                    f"中心值 {_fmt(comps.get('central_value'))} 元，"
                    f"相对现价的隐含回报 {_pct(comps.get('implied_return'))}。"
                ),
            )
        )

    bands = field_data.get("bands") or []
    if bands:
        add(Block(kind="h3", text="6.3 各方法的估值区间"))
        add(
            Block(
                kind="table",
                header=("方法", "区间下界", "中心值", "区间上界"),
                rows=tuple(
                    (
                        band["name"],
                        _fmt(band["low"]),
                        _fmt(band["central"]),
                        _fmt(band["high"]),
                    )
                    for band in bands
                ),
            )
        )
    blocks.extend(
        _chart_blocks(charts, ("chart_football_field", "chart_peer_multiples"))
    )
    blocks.extend(_narrative_block(narrative, "valuation_analysis"))

    # ── 7 敏感性与区间归因 ────────────────────────────────────────────
    add(Block(kind="h2", text=f"7. 敏感性与区间归因　{FACT}{INFERENCE}"))
    variables = sensitivity.get("variables") or []
    if variables:
        add(
            Block(
                kind="table",
                header=(
                    "假设",
                    "下界",
                    "上界",
                    "下界对应每股价值",
                    "上界对应每股价值",
                    "影响幅度（元）",
                ),
                rows=tuple(
                    (
                        item["name"],
                        _fmt(item["low"], 4),
                        _fmt(item["high"], 4),
                        _fmt(item["per_share_at_low"]),
                        _fmt(item["per_share_at_high"]),
                        _fmt(item["swing"]),
                    )
                    for item in variables
                ),
            )
        )
    grid = sensitivity.get("grid")
    if grid:
        add(
            Block(
                kind="p",
                text=(
                    f"双因素网格（{grid['row']} × {grid['col']}）得到的估值区间为 "
                    f"**{_fmt(grid['low'])} ~ {_fmt(grid['high'])} 元**。"
                    "这个区间取决于每个假设的浮动幅度，幅度的取值理由见运行清单。"
                ),
            )
        )
    blocks.extend(_chart_blocks(charts, ("chart_tornado", "chart_sensitivity")))
    blocks.extend(_narrative_block(narrative, "sensitivity_analysis"))

    # ── 8 反向估值 ────────────────────────────────────────────────────
    add(Block(kind="h2", text=f"8. 反向估值　{FACT}{INFERENCE}"))
    if reverse:
        add(
            Block(
                kind="table",
                header=("假设", "基准值", "市场隐含值", "方向"),
                rows=tuple(
                    (
                        str(item["name"]),
                        _fmt(item["base_value"], 4),
                        _fmt(item["implied_value"], 4),
                        str(
                            _DIRECTION_TEXT.get(
                                item["direction"], item["direction"]
                            )
                        ),
                    )
                    for item in reverse
                ),
            )
        )
    else:
        add(Block(kind="p", text="本次没有反解出任何参数，原因见下节。"))
    blocks.extend(_narrative_block(narrative, "reverse_dcf"))

    # ── 9 风险与适用边界 ──────────────────────────────────────────────
    add(Block(kind="h2", text=f"9. 风险与适用边界　{INFERENCE}{OPINION}"))
    warnings: list[str] = []
    warnings.extend(dcf.get("diagnostics") or [])
    totals = validation.get("totals") or {}
    if totals:
        warnings.append(
            f"勾稽校验：失败 {totals.get('fail', 0)} 项、告警 "
            f"{totals.get('warning', 0)} 项、跳过 {totals.get('skipped', 0)} 项"
            "（跳过不等于通过）"
        )
    warnings.extend(comps.get("warnings") or [])
    warnings.extend(field_data.get("warnings") or [])
    warnings.extend(notes.get("peers") or [])
    warnings.extend(notes.get("reverse_skipped") or [])
    if warnings:
        add(Block(kind="list", items=tuple(dict.fromkeys(warnings))))
    else:
        add(Block(kind="p", text="本次运行没有产生告警。"))
    add(
        Block(
            kind="list",
            items=(
                "基期资产负债表与可比公司财务数据目前是手工填写的过渡数据，"
                "尚未由年报抽取生成，口径以数据文件里的注释为准。",
                "可比公司的利润类指标取自最近一个完整年度，目标公司取自预测第一年，"
                "两者口径不同，做倍数比较时需要注意。",
                "收入增速与利润率的敏感性分析是把整条预测路径拉平成一个值再算，"
                "不是只改第一年。",
                "本报告不构成投资建议。",
            ),
        )
    )
    blocks.extend(_narrative_block(narrative, "risks"))

    # ── 10 附录 ───────────────────────────────────────────────────────
    add(Block(kind="h2", text="10. 附录"))
    add(Block(kind="h3", text="10.1 数据文件索引"))
    add(
        Block(
            kind="list",
            items=(
                "01_projections.json　三表联动预测的逐年明细",
                "02_validation_report.json　勾稽校验的逐条结论",
                "03_valuation.json　现金流折现的完整结果与诊断",
                "04_tornado.csv　单因素敏感性排序",
                "05_sensitivity.csv　双因素网格",
                "06_reverse_dcf.json　反解出的市场隐含假设",
                "07_comps.csv　可比公司倍数表",
                "08_comps_implied.csv　各倍数的隐含每股价值",
                "09_football_field.json　各方法的估值区间",
                "10_run_manifest.json　本次运行的清单与全部产物的哈希",
            ),
        )
    )
    if charts:
        add(Block(kind="h3", text="10.2 图表索引"))
        add(Block(kind="list", items=tuple(sorted(Path(p).name for p in charts.values()))))
    add(Block(kind="h3", text="10.3 大模型使用与数字校验"))
    if narrative is not None and narrative.ok:
        usage = narrative.usage or {}
        add(
            Block(
                kind="table",
                header=("项目", "内容"),
                rows=(
                    ("模型", narrative.model),
                    ("接口", narrative.endpoint),
                    ("提示词版本", narrative.prompt_version),
                    ("温度", _fmt(narrative.temperature)),
                    ("提示词 sha256", narrative.prompt_sha256[:16] + "…"),
                    ("响应 sha256", narrative.response_sha256[:16] + "…"),
                    (
                        "token 用量",
                        f"输入 {usage.get('prompt_tokens', '—')} / "
                        f"输出 {usage.get('completion_tokens', '—')}",
                    ),
                    (
                        "数字校验",
                        "未发现事实表之外的数字"
                        if not narrative.number_warnings
                        else f"{len(narrative.number_warnings)} 处待核",
                    ),
                ),
            )
        )
        if narrative.number_warnings:
            add(Block(kind="list", items=tuple(narrative.number_warnings[:15])))
        add(
            Block(
                kind="p",
                text=(
                    "数字校验的做法：把大模型写出的每一个数字与事实表逐一比对，"
                    "对不上的全部列在上面。这个校验是筛子而不是证明 —— "
                    "年份、编号和小于 100 的整数会被放宽，"
                    "它也查不出「数字对但话说过了」的问题。"
                ),
            )
        )
    else:
        reason = "；".join(narrative.notes) if narrative and narrative.notes else "未启用"
        add(
            Block(
                kind="p",
                text=f"本次报告的叙述文字由数据表与模板构成，未调用大模型（{reason}）。",
            )
        )
    add(
        Block(
            kind="quote",
            text=(
                "报告章节结构与写作规则参考 FinRobot（AI4Finance-Foundation，"
                "Apache-2.0）的 report_structure 与 report_agent 设计，代码为自主实现。"
            ),
        )
    )

    add(Block(kind="h3", text="10.4 过程追溯"))
    add(
        Block(
            kind="p",
            text=(
                "通知要求完整记录文件访问、工具调用、计算过程和结果生成情况。"
                "下面是本次运行的实际执行记录，取自日志层的 trace.jsonl —— "
                "每一步的名字、状态、耗时都是跑出来的，不是写出来的。"
            ),
        )
    )
    if context.inputs:
        add(
            Block(
                kind="table",
                header=("读入的文件", "sha256（前 12 位）"),
                rows=tuple(
                    (str(path), str(digest)[:12]) for path, digest in context.inputs
                ),
            )
        )
    if context.steps:
        add(
            Block(
                kind="table",
                # 刻意不打印耗时：它是某一次运行的原始记录，每次都不一样。
                # 报告正文要可复现（两次跑同一份配置应当逐字相同），
                # 所以易变的东西留在 trace.jsonl 里，报告只放稳定的部分。
                header=("序号", "阶段", "状态"),
                rows=tuple(
                    (
                        str(step.get("seq", "")),
                        str(step.get("step", "")),
                        str(step.get("status", "")),
                    )
                    for step in context.steps
                ),
            )
        )
        add(
            Block(
                kind="note",
                text=(
                    "每步的耗时、输入输出与哈希记录在 "
                    "logs/<运行编号>/trace.jsonl 里，那里是原始执行记录。"
                ),
            )
        )
    if not context.inputs and not context.steps:
        add(
            Block(
                kind="note",
                text="本次没有传入过程记录（直接调用导出层时会出现这种情况）。",
            )
        )

    return blocks
