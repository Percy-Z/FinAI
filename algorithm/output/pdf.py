"""把报告从 Markdown 渲染成 PDF。

来源说明
    渲染逻辑（中文字体注册、Markdown 子集到 reportlab flowables 的转换、
    表格样式）取自本仓库另一条分支上的 `pipeline/report.py`（队友实现，
    见 origin/main 的提交「框架层：补上报告三件套」）。这里把它抽成通用模块：
    输入任意 Markdown 文件 + 图片基准目录，输出 PDF。

    这样两边的分工就清楚了：**报告内容由算法层的 AI 推理产出，
    排版出 PDF 用这套渲染器**。同一份 Markdown 既能看网页、也能出 PDF，
    不存在两份内容会走偏的问题。

依赖
    reportlab 可选。没装时 `markdown_to_pdf()` 返回 None，调用方记一条提示，
    流水线照常跑完 —— 与 matplotlib、pdfplumber 的处理方式一致。

中文字体
    PDF 里放中文必须嵌入字体，否则会是方块或乱码。这里按 macOS / Windows /
    Linux 依次找系统自带的黑体，都找不到时退回 reportlab 内置的 STSong-Light
    （不嵌入字体文件，需要阅读器支持）。字体选择不影响内容。
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

HAS_REPORTLAB = importlib.util.find_spec("reportlab") is not None

# 中文字体候选：名字、路径、ttc 里的第几个字重
_FONT_CANDIDATES: list[tuple[str, str, int]] = [
    # macOS
    ("FinAINormal", "/System/Library/Fonts/STHeiti Light.ttc", 0),
    ("FinAIBold", "/System/Library/Fonts/STHeiti Medium.ttc", 0),
    # Windows
    ("FinAINormal", "C:/Windows/Fonts/msyh.ttc", 0),
    ("FinAIBold", "C:/Windows/Fonts/msyhbd.ttc", 0),
    # Linux
    ("FinAINormal", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("FinAIBold", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
]

# 图片占满正文宽度（A4 正文宽 174mm，留点边），太大反而看不清
_IMAGE_WIDTH_MM = 160.0


def _register_fonts() -> str:
    """注册中文字体，返回正文用的字体名。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    registered: dict[str, str] = {}
    for name, path, index in _FONT_CANDIDATES:
        if name in registered or not Path(path).exists():
            continue
        try:
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, path, subfontIndex=index))
        except Exception:  # noqa: BLE001 - 字体读不了就试下一个
            continue
        registered[name] = path

    normal = "FinAINormal" if "FinAINormal" in registered else None
    bold = "FinAIBold" if "FinAIBold" in registered else None

    if normal is None:
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont

        if "STSong-Light" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        return "STSong-Light"

    # 注册字体族，<b> 标签才找得到粗体
    pdfmetrics.registerFontFamily(
        normal, normal=normal, bold=bold or normal, italic=normal,
        boldItalic=bold or normal,
    )
    return normal


def _pdf_styles() -> dict[Any, Any]:
    """排版样式。字号按 A4 阅读舒适度定，正文 9.5pt。"""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.styles import ParagraphStyle

    font = _register_fonts()
    return {
        1: ParagraphStyle("h1", fontName=font, fontSize=18, leading=26, spaceAfter=10),
        2: ParagraphStyle(
            "h2", fontName=font, fontSize=14, leading=21, spaceBefore=14, spaceAfter=8
        ),
        3: ParagraphStyle(
            "h3", fontName=font, fontSize=11.5, leading=17, spaceBefore=10, spaceAfter=6
        ),
        "body": ParagraphStyle("body", fontName=font, fontSize=9.5, leading=15, spaceAfter=5),
        "quote": ParagraphStyle(
            "quote", fontName=font, fontSize=9, leading=14, leftIndent=10,
            # 用 HexColor 而不是字符串：reportlab 的签名要求 Color 对象，
            # 字符串在运行时能用但在严格类型检查下会报错
            textColor=HexColor("#4a5568"), spaceAfter=6,
        ),
        "bullet": ParagraphStyle(
            "bullet", fontName=font, fontSize=9.5, leading=15, leftIndent=12, spaceAfter=2
        ),
    }


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(text: str) -> str:
    """行内标记转成 reportlab 认的标签。先转义再加标签，顺序不能反。"""
    out = _escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"`(.+?)`", r'<font face="Courier">\1</font>', out)
    return out


def _flowables(markdown: str, base_dir: Path) -> list[Any]:
    """把 Markdown 子集转成 reportlab 的 flowables。

    只认我们报告里实际用到的几种：标题、表格、图片、引用块、无序列表、段落。
    认不出的语法会当普通段落处理，不会丢内容。
    """
    from reportlab.platypus import Image, Paragraph, Spacer, Table, TableStyle

    styles = _pdf_styles()
    flow: list[Any] = []
    lines = markdown.splitlines()
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue

        # 标题
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            flow.append(Paragraph(_inline(stripped[level:].strip()), styles[min(level, 3)]))
            i += 1
            continue

        # 图片：相对路径按 base_dir 解析（报告与 charts/ 在同一个目录下）
        picture = re.match(r"!\[([^\]]*)\]\(([^)]+)\)", stripped)
        if picture:
            path = (base_dir / picture.group(2)).resolve()
            if path.exists():
                from reportlab.lib.utils import ImageReader

                width, height = ImageReader(str(path)).getSize()
                target = _IMAGE_WIDTH_MM * 72 / 25.4
                flow.append(Image(str(path), width=target, height=target * height / width))
                flow.append(Spacer(1, 8))
            i += 1
            continue

        # 表格
        if stripped.startswith("|"):
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                # 分隔行（全是 - 和 :）要跳过
                if not all(set(c) <= set("-: ") and c for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                body = [[Paragraph(_inline(c), styles["body"]) for c in row] for row in rows]
                table = Table(body, repeatRows=1, hAlign="LEFT")
                table.setStyle(
                    TableStyle(
                        [
                            ("GRID", (0, 0), (-1, -1), 0.4, "#c8d0da"),
                            ("BACKGROUND", (0, 0), (-1, 0), "#eef2f6"),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 4),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                            ("TOPPADDING", (0, 0), (-1, -1), 3),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                        ]
                    )
                )
                flow.append(table)
                flow.append(Spacer(1, 8))
            continue

        # 引用块：报告里的「AI 生成」徽章、内容性质说明、不合格横幅都在这里
        if stripped.startswith(">"):
            block: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                block.append(lines[i].strip().lstrip(">").strip())
                i += 1
            text = "<br/>".join(_inline(b) for b in block if b)
            if text:
                flow.append(Paragraph(text, styles["quote"]))
            continue

        # 无序列表
        if stripped.startswith("- "):
            while i < len(lines) and lines[i].strip().startswith("- "):
                flow.append(
                    Paragraph("• " + _inline(lines[i].strip()[2:]), styles["bullet"])
                )
                i += 1
            flow.append(Spacer(1, 4))
            continue

        # 普通段落：连续行合并成一段
        block = []
        while i < len(lines):
            candidate = lines[i].strip()
            if not candidate or candidate.startswith(("#", "|", ">", "- ", "![")):
                break
            block.append(candidate)
            i += 1
        if block:
            flow.append(Paragraph(" ".join(_inline(b) for b in block), styles["body"]))

    return flow


def markdown_to_pdf(
    markdown_path: str | Path,
    pdf_path: str | Path | None = None,
    break_before: str | None = "## 10. 附录",
) -> Path | None:
    """把一份 Markdown 渲染成 A4 PDF。

    break_before 指定在哪个二级标题前另起一页 —— 报告里图表集中在附录，
    不换页的话图片会插在正文中间，读起来很跳。默认值对应本项目的报告结构；
    传 None 表示不强制换页。

    返回 PDF 路径；没装 reportlab 时返回 None。
    """
    if not HAS_REPORTLAB:
        return None

    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import PageBreak, SimpleDocTemplate

    source = Path(markdown_path)
    if not source.exists():
        raise FileNotFoundError(f"找不到要渲染的 Markdown：{source}")
    target = Path(pdf_path) if pdf_path else source.with_suffix(".pdf")
    base_dir = source.parent

    document = SimpleDocTemplate(
        str(target),
        pagesize=A4,
        title=source.stem,
        leftMargin=18 * 72 / 25.4,
        rightMargin=18 * 72 / 25.4,
        topMargin=18 * 72 / 25.4,
        bottomMargin=18 * 72 / 25.4,
    )

    markdown = source.read_text(encoding="utf-8")
    if break_before and break_before in markdown:
        head, _, tail = markdown.partition(break_before)
        flow = _flowables(head, base_dir)
        flow.append(PageBreak())
        flow.extend(_flowables(break_before + tail, base_dir))
    else:
        flow = _flowables(markdown, base_dir)

    document.build(flow)
    return target


def main(argv: list[str] | None = None) -> int:
    import sys

    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        print("用法：python3 -m algorithm.output.pdf <报告.md> [输出.pdf]")
        return 2
    if not HAS_REPORTLAB:
        print("没有安装 reportlab，无法生成 PDF：uv pip install reportlab")
        return 1
    result = markdown_to_pdf(args[0], args[1] if len(args) > 1 else None)
    print(f"已生成 {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
