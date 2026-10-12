"""抽取层：年报 PDF → 结构化字段（带出处）。

分工
----
    algorithm/finai/extract.py   算法层的抽取器（pdfplumber，科目表较窄）
    finai_web/extract.py         取数层的抽取编排：读 PDF → 扫描 → 拼流水线字段 → 落盘

为什么不是简单调一下算法层就算完
    流水线要的字段比算法层抽的那 12 个科目多：算 EBIT / EBITDA 要利息费用、
    所得税费用、折旧摊销；拼资产负债表要借款明细、股本、归母权益。
    算法层的科目表是为「报告里点名的那几项」设计的，硬加科目会动到它的测试与报告输出。
    所以这里的做法是：**自己的科目表（schema.ACCOUNTS）扫一遍**，
    同时把算法层的结果当**交叉验证**跑一遍，两者对数，不一致就写进 `_meta.cross_check`。
    这样既拿到了需要的字段，又保留了「同一个数两条路径都得出来」的复核。

三条底线
    1. 每个数字都要带页码和原文片段，能回查。
    2. 单位要认（千元 / 万元 → 元），认不出就标「未标注」并降低置信度。
    3. 抽不出就说抽不出，绝不返回 0 冒充数字。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import schema
from .config import PATHS

# 单位标记（与 algorithm/finai/extract.py 保持一致，免得两套抽取器算法不同）
UNIT_MARKERS: tuple[tuple[str, float, str], ...] = (
    ("单位：万元", 10_000.0, "万元"),
    ("单位:万元", 10_000.0, "万元"),
    ("单位：千元", 1_000.0, "千元"),
    ("单位:千元", 1_000.0, "千元"),
    ("单位：元", 1.0, "元"),
    ("单位:元", 1.0, "元"),
    ("单位：百万元", 1_000_000.0, "百万元"),
    ("单位:百万元", 1_000_000.0, "百万元"),
)

# 一行里的数字：千分位、小数、括号负数（财报惯例）
_NUMBER = re.compile(r"\(?-?\d[\d,]*\.?\d*\)?")

# 这些词跟在科目名后面时说明这行是衍生指标（「应收账款周转率」），不是报表科目
_DERIVED_TAIL = ("率", "周转", "账龄", "占比", "比重")


class ExtractError(RuntimeError):
    """抽取失败，且失败原因需要原样讲给使用者听。"""


@dataclass
class Hit:
    """一个科目的抽取结果。"""

    account: str
    label: str
    value_yuan: float
    raw_text: str
    page: int
    unit: str
    scale: float
    confidence: float
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "value_yuan": self.value_yuan,
            "label": self.label,
            "page": self.page,
            "raw_text": self.raw_text,
            "unit": self.unit,
            "scale": self.scale,
            "confidence": round(self.confidence, 3),
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# 读 PDF
# ---------------------------------------------------------------------------


def pdf_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_pages(pdf_path: str | Path) -> tuple[list[str], str]:
    """把 PDF 逐页读成文本，返回 (页文本列表, 用的后端名)。

    三个后端按顺序试：pdfplumber（表格最准）→ pypdf（最轻）→ PyMuPDF（最快）。
    一个都没有时抛 ExtractError 并告诉使用者装哪个 —— 不返回空列表假装成功。
    """
    path = Path(pdf_path)
    if not path.exists():
        raise ExtractError(f"PDF 不存在：{path}")

    try:
        import pdfplumber  # type: ignore[import-not-found]

        with pdfplumber.open(path) as pdf:
            return [page.extract_text() or "" for page in pdf.pages], "pdfplumber"
    except ImportError:
        pass

    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]

        reader = PdfReader(str(path))
        return [(page.extract_text() or "") for page in reader.pages], "pypdf"
    except ImportError:
        pass

    try:
        import fitz  # type: ignore[import-not-found]  # PyMuPDF

        with fitz.open(path) as doc:
            return [page.get_text() for page in doc], "pymupdf"
    except ImportError:
        pass

    raise ExtractError(
        "没有可用的 PDF 解析库，本地抽不了。装一个即可（任选其一）：\n"
        "    python -m pip install pdfplumber     # 推荐，表格最准\n"
        "    python -m pip install pypdf\n"
        "    python -m pip install PyMuPDF\n"
        "或者加 --firecrawl 走 Firecrawl 的 PDF 解析（需要 FIRECRAWL_API_KEY）。"
    )


def detect_scale(page_text: str) -> tuple[float, str]:
    """认页面上的「单位：X」；认不出返回 (1.0, '未标注') 并让调用方降置信度。"""
    for marker, scale, name in UNIT_MARKERS:
        if marker in page_text:
            return scale, name
    return 1.0, "未标注"


def _parse_number(text: str) -> float | None:
    match = _NUMBER.search(text)
    if not match:
        return None
    token = match.group(0)
    negative = token.startswith("(") or token.startswith("-")
    cleaned = token.strip("()-").replace(",", "")
    if not cleaned or cleaned == ".":
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


def _iter_lines(page_text: str) -> Iterable[str]:
    for raw in page_text.splitlines():
        line = raw.strip()
        if line:
            yield line


# 行首修饰：序号、项目符号、编号（"1." "（1）" "•"）。真实年报的报表行偶尔带这些
# 前缀，科目名本身还是从它们之后开始。
_ROW_LEAD = re.compile(
    r"^(?:[（(]?\s*[0-9一二三四五六七八九十]{1,3}\s*[）)、.．]?|[•·▪※-])\s*"
)


def _row_head(line: str) -> tuple[str, str] | None:
    """把一行拆成 (科目名, 数字那一段)；不是「科目名 + 数字」这种表行就返回 None。

    为什么必须这么严：真实年报里拿科目名做子串匹配是灾难现场 ——

        「非流动负债合计」里含「负债合计」    → 抽到非流动负债，倒挤出的差额变负数
        「固定资产改造」里含「固定资产」      → 抽到现金流量表的科目
        「报告期末，高新地产总资产72.27亿元」 → 一句叙述被当成报表数字

    这三类在演示用的合成数据里都不会出现，真实年报上第一条就中（000661 实测）。
    所以规则收紧成两条：科目名从行首开始（允许序号这类前缀），紧跟着就是数字。

    注意顺序：**先去行首序号，再找数字**。利润表里的行常写成「1.归属于母公司股东的
    净利润 154,971,452.80」，序号里的 1 会被当成数字，把整行判成「没有科目名」而丢掉
    （000661 的归母净利润就是这么丢的）。
    """
    text = _ROW_LEAD.sub("", line).strip()
    match = _NUMBER.search(text)
    if match is None:
        return None
    head = text[: match.start()].strip().strip("：: 　").strip()
    if not head:
        return None
    return head, text[match.start() :]


# 报表表头 → 这一节属于哪张表。真实年报按「合并资产负债表 → 母公司资产负债表 →
# 合并利润表 → 母公司利润表 → …」排下来，每张表通常跨 2 到 3 页。
_STATEMENT_MARKERS: tuple[tuple[str, str], ...] = (
    ("合并资产负债表", "consolidated-balance"),
    ("母公司资产负债表", "parent-balance"),
    ("合并利润表", "consolidated-income"),
    ("母公司利润表", "parent-income"),
    ("合并现金流量表", "consolidated-cashflow"),
    ("母公司现金流量表", "parent-cashflow"),
)


def _statement_segments(pages: Sequence[str]) -> list[list[tuple[int, str]]]:
    """给每一页标出「从第几行起属于哪张报表」。

    为什么需要这一层：只看「这一页有没有『合并』二字」会被报表混排骗到。
    000661（长春高新 2025 年报）里，母公司资产负债表的「负债合计 2.9 亿」正好和
    「合并利润表」的表头印在同一页 —— 那一页含「合并」，于是母公司口径的负债
    被打上高置信度，把合并口径的 52 亿挤掉了。

    做法分两步。先剔掉两类假表头：

        目录页    一页里列了好几张表的名字（「财务报表……合并资产负债表 81」）
        附注标题  「七、合并财务报表项目注释」里又出现一次「合并资产负债表」，
                  那是讲某个科目的一节，不是报表本身

    所以：一页里出现两个及以上表名就当目录页跳过；同一个表名只认第一次出现。

    然后把归属精确到**行**：一页里可能有「上一张表的尾行 + 下一张表的表头」，
    表头之前的那几行仍旧属于上一张表（真实年报就是这么排的）。
    """
    heads: dict[str, int] = {}
    for index, text in enumerate(pages):
        found = [kind for marker, kind in _STATEMENT_MARKERS if marker in text]
        if len(found) > 1:
            continue  # 目录页：一页列了好几张表的名字
        if not found:
            continue
        heads.setdefault(found[0], index)

    out: list[list[tuple[int, str]]] = []
    current = ""
    for index, text in enumerate(pages):
        segments: list[tuple[int, str]] = [(0, current)]
        for line_index, line in enumerate(text.splitlines()):
            for marker, kind in _STATEMENT_MARKERS:
                if heads.get(kind) == index and marker in line:
                    segments.append((line_index, kind))
        segments.sort(key=lambda item: item[0])
        out.append(segments)
        current = segments[-1][1]
    return out


def _statement_at(segments: Sequence[tuple[int, str]], line_index: int) -> str:
    """取某一行的报表归属：最后一个「起点不晚于这一行」的段。"""
    found = ""
    for start, kind in segments:
        if start > line_index:
            break
        found = kind
    return found


def scan_pages(
    pages: Sequence[str],
    accounts: Sequence[str] | None = None,
) -> dict[str, Hit]:
    """在页文本里扫科目，返回 {科目: Hit}。

    规则（每条都能解释，不靠黑盒）：
        - 页面里有「合并」二字时，这一页的匹配置信度更高 —— 我们要合并口径，
          母公司报表的数会低估负债与权益。
        - 同一科目多处出现时，取「置信度高、页码靠前」的那一处，其余的页数记进 note。
        - 科目名后面紧跟「率 / 周转 / 账龄」的行跳过，那是衍生指标。
        - 抽不到单位时按「元」处理并把置信度压到 0.5 以下，提醒人核对。
    """
    wanted = {
        account.name: account
        for account in schema.ACCOUNTS
        if accounts is None or account.name in accounts
    }
    candidates: dict[str, list[Hit]] = {}
    statements = _statement_segments(pages)

    # 报表单位：年报一般只在第一张表上写一次「单位：元」，后面几张不重复写。
    # 全篇找一次作为回退 —— 没有这个回退，利润表那种不写单位的页会被压到
    # 0.5 置信度，叙述句里的数字就会顶上来（000661 的归母净利润就是这样抽错的）。
    report_scale, report_unit = 1.0, "未标注"
    for text in pages:
        page_scale, page_unit = detect_scale(text)
        if page_unit != "未标注":
            report_scale, report_unit = page_scale, page_unit
            break

    for page_number, page_text in enumerate(pages, start=1):
        if not page_text:
            continue
        is_consolidated = "合并" in page_text
        segments = statements[page_number - 1]
        scale, unit_name = detect_scale(page_text)
        if unit_name == "未标注" and report_unit != "未标注":
            scale, unit_name = report_scale, report_unit
        for name, account in wanted.items():
            # 资产负债表与利润表的科目按「所在报表」排优先级：
            #     本表合并页（0.95）> 没标出报表的页（旧规则）> 母公司表（0.35）
            #     > 别的报表页（0.30）
            # 不做硬排除：有些报告（以及本仓库的离线样例）只在第一张表写了表头，
            # 硬排除会把后面的科目整片漏掉。打分只决定「同名行之间谁赢」——
            # 真实年报里母公司表的负债合计与合并利润表同页，正是靠这个排序才压得住。
            # 现金流量表科目（折旧摊销等）主要来自补充资料与附注，按全篇扫描。
            strict = account.section in ("balance", "income")
            consolidated = f"consolidated-{account.section}"
            parent = f"parent-{account.section}"
            for line_index, raw_line in enumerate(page_text.splitlines()):
                line = raw_line.strip()
                if not line:
                    continue
                statement = _statement_at(segments, line_index)
                row = _row_head(line)
                if row is None:
                    continue
                head, tail = row
                if head not in account.labels:
                    continue
                label = head
                if any(key in tail[:6] for key in _DERIVED_TAIL):
                    continue
                value = _parse_number(tail)
                if value is None:
                    continue
                if strict and statement == consolidated:
                    confidence = 0.95
                elif strict and statement == parent:
                    confidence = 0.35
                elif strict and statement:
                    confidence = 0.30
                else:
                    confidence = 0.9 if is_consolidated else 0.6
                if unit_name == "未标注":
                    confidence = min(confidence, 0.5)
                if name == "revenue" and label == "营业收入" and "营业总收入" in page_text:
                    # 两行都在的页面，营业收入是分项，营业总收入才是我们要的
                    confidence = min(confidence, 0.7)
                if strict and statement == parent:
                    note = "来自母公司报表，不是合并口径"
                elif is_consolidated:
                    note = ""
                else:
                    note = "本页没有『合并』二字，可能是母公司报表"
                candidates.setdefault(name, []).append(
                    Hit(
                        account=name,
                        label=label,
                        value_yuan=value * scale,
                        raw_text=line[:160],
                        page=page_number,
                        unit=unit_name,
                        scale=scale,
                        confidence=confidence,
                        note=note,
                    )
                )
                break

    best: dict[str, Hit] = {}
    for name, hits in candidates.items():
        winner = max(hits, key=lambda hit: (hit.confidence, -hit.page))
        if len(hits) > 1:
            others = "、".join(str(hit.page) for hit in hits if hit is not winner)
            winner.note = (winner.note + "；" if winner.note else "") + f"另有 {len(hits) - 1} 处（第 {others} 页）"
        best[name] = winner
    return best


def _values(matches: Mapping[str, Hit]) -> dict[str, float]:
    return {name: hit.value_yuan for name, hit in matches.items()}


def _cross_check_with_algorithm(pdf_path: Path, matches: Mapping[str, Hit]) -> dict[str, Any]:
    """拿算法层的抽取器再抽一遍，两边对不上就记下来。

    对不上不一定是错 —— 两个抽取器的科目表与择优规则不同（例如「营业收入」与
    「营业总收入」）。但**任何一处对不上都值得看一眼原文**，所以要留痕。
    """
    try:
        from algorithm.finai.extract import extract_financials
    except Exception as exc:  # pragma: no cover - 取决于运行位置
        return {"available": False, "reason": f"算法层抽取器导入失败：{exc}"}

    result = extract_financials(pdf_path)
    if not result.matches and result.notes:
        return {"available": True, "ok": False, "notes": result.notes[:3]}

    differences: list[str] = []
    for name, hit in result.matches.items():
        mine = matches.get(name)
        if mine is None or hit.value_yuan is None:
            continue
        if abs(mine.value_yuan - hit.value_yuan) > 1.0:
            differences.append(
                f"{name}：本模块第 {mine.page} 页 {mine.value_yuan:,.2f}，"
                f"算法层第 {hit.page} 页 {hit.value_yuan:,.2f}"
            )
    return {
        "available": True,
        "ok": not differences,
        "differences": differences,
        "algorithm_found": len(result.matches),
    }


# ---------------------------------------------------------------------------
# 拼一条记录
# ---------------------------------------------------------------------------


def build_record(
    *,
    code: str,
    name: str,
    year: int,
    pages: Sequence[str],
    pdf_path: str | Path | None = None,
    backend: str = "unknown",
    source_url: str = "",
    method: str = "local-pdf",
    cross_check: Mapping[str, Any] | None = None,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """页文本 → 一条完整的抽取记录（可落盘、可接流水线）。"""
    text_chars = sum(len(page) for page in pages)
    matches = scan_pages(pages)
    values = _values(matches)

    record: dict[str, Any] = {
        "code": code,
        "name": name,
        "year": year,
        "period": f"{year}A",
        "pdf": str(pdf_path) if pdf_path else "",
        "sha256": pdf_sha256(pdf_path) if pdf_path else "",
        "pages": len(pages),
        "has_text_layer": text_chars > 200,
        "backend": backend,
        "method": method,
        "source_url": source_url,
        "extracted_at": occurred_at or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "accounts": {name: hit.to_dict() for name, hit in sorted(matches.items())},
        "notes": [],
    }

    if not record["has_text_layer"]:
        record["notes"].append(
            f"整份 PDF 只抽到 {text_chars} 个字符，几乎没有文字层 —— 大概率是扫描件。"
            "扫描件要先 OCR；本模块不猜数字。"
        )

    # 拼流水线要的两块
    try:
        opening = schema.build_opening_balance(values)
        record["opening_balance"] = opening.to_dict()
        record["notes"].extend(opening.notes)
    except schema.SchemaError as err:
        record["opening_balance"] = None
        record["notes"].append(f"基期资产负债表拼不出来：{err}")

    try:
        peer = schema.build_peer_metrics(values)
        record["peer"] = peer.to_dict()
        record["notes"].extend(peer.notes)
    except schema.SchemaError as err:
        record["peer"] = None
        record["notes"].append(f"可比公司指标拼不出来：{err}")

    missing = [name for name in schema.ACCOUNT_NAMES if name not in matches]
    record["missing_accounts"] = missing
    if missing and record["has_text_layer"]:
        record["notes"].append(
            "这些科目没抽到：" + "、".join(missing) + "。"
            "可能是年报里写法不同或落在无法解析的表格里 —— 缺的科目会让对应字段变成默认值或倒挤，"
            "报告里会写明，请核对原文。"
        )

    if cross_check is not None:
        record["cross_check"] = dict(cross_check)
    return record


def extract_pdf(
    pdf_path: str | Path,
    *,
    code: str,
    name: str,
    year: int,
    source_url: str = "",
    with_cross_check: bool = True,
) -> dict[str, Any]:
    """本地抽一份年报 PDF。"""
    path = Path(pdf_path)
    pages, backend = read_pages(path)
    matches = scan_pages(pages)
    cross = _cross_check_with_algorithm(path, matches) if with_cross_check else None
    return build_record(
        code=code,
        name=name,
        year=year,
        pages=pages,
        pdf_path=path,
        backend=backend,
        source_url=source_url,
        method=f"local-pdf/{backend}",
        cross_check=cross,
    )


# ---------------------------------------------------------------------------
# Firecrawl 兜底
# ---------------------------------------------------------------------------

# 给大模型的结构化 schema。刻意只要「数字 + 页码 + 原文」三件套：
# 大模型擅长从乱七八糟的版面里找数，不擅长保证口径 —— 所以口径仍然由
# schema.py 的倒挤规则决定，这里只负责把原始数字捞出来。
FIRECRAWL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "unit": {"type": "string", "description": "报表单位，如 元 / 千元 / 万元；未标注写 unknown"},
        "accounts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "年报上的科目名，照抄原文"},
                    "value": {"type": "number", "description": "按报表单位填，不要换算成元"},
                    "page": {"type": "integer"},
                    "raw_text": {"type": "string"},
                },
                "required": ["name", "value"],
            },
        },
    },
    "required": ["accounts"],
}


def record_from_firecrawl(
    payload: Mapping[str, Any],
    *,
    code: str,
    name: str,
    year: int,
    source_url: str,
) -> dict[str, Any]:
    """把 Firecrawl 的 JSON 结果变成和本地抽取同构的记录。

    Firecrawl 返回的是「年报上的科目名」，这里要翻成内部字段名（schema.ACCOUNTS）。
    翻不出来的科目名会进 `unmapped`，不丢 —— 那可能正是我们缺的科目，
    留着让人一眼看到「模型读到了什么、我们没用上什么」。
    """
    unit = str(payload.get("unit") or "元")
    scale = next((item[1] for item in UNIT_MARKERS if item[2] == unit), 1.0)

    matches: dict[str, Hit] = {}
    unmapped: list[dict[str, Any]] = []
    for item in payload.get("accounts") or []:
        raw_name = str(item.get("name") or "").strip()
        value = item.get("value")
        if not raw_name or value is None:
            continue
        account = next(
            (candidate for candidate in schema.ACCOUNTS if raw_name in candidate.labels),
            None,
        )
        if account is None:
            unmapped.append({"name": raw_name, "value": value})
            continue
        page = int(item.get("page") or 0)
        matches.setdefault(
            account.name,
            Hit(
                account=account.name,
                label=raw_name,
                value_yuan=float(value) * scale,
                raw_text=str(item.get("raw_text") or "")[:160],
                page=page,
                unit=unit,
                scale=scale,
                # 大模型抽的数一律按「需要复核」处理：置信度不给到 0.9 以上
                confidence=0.6,
                note="Firecrawl 结构化抽取，建议与 PDF 原文核对",
            ),
        )

    pages = [""] * max((hit.page for hit in matches.values()), default=0)
    record = build_record(
        code=code,
        name=name,
        year=year,
        pages=pages,
        pdf_path=None,
        backend="firecrawl",
        source_url=source_url,
        method="firecrawl-json",
    )
    record["accounts"] = {name: hit.to_dict() for name, hit in sorted(matches.items())}
    record["has_text_layer"] = True
    record["unit_hint"] = unit
    record["unmapped"] = unmapped
    if unmapped:
        record["notes"].append(
            f"Firecrawl 读到 {len(unmapped)} 个本模块科目表里没有的名字（见 unmapped），"
            "如果里面有你要的科目，把它们加进 schema.ACCOUNTS 的 labels 即可。"
        )
    return record


def extract_via_firecrawl(
    client: Any,
    url: str,
    *,
    code: str,
    name: str,
    year: int,
) -> dict[str, Any]:
    """兜底路径：让 Firecrawl 解析 PDF/公告页，返回结构化记录。"""
    payload = client.extract(url, FIRECRAWL_SCHEMA)
    return record_from_firecrawl(payload, code=code, name=name, year=year, source_url=url)


# ---------------------------------------------------------------------------
# 落盘 / 读回
# ---------------------------------------------------------------------------


def record_path(code: str, year: int, directory: Path | None = None) -> Path:
    return (directory or PATHS.extracted) / f"{code}_{year}.json"


def write_record(record: Mapping[str, Any], directory: Path | None = None) -> Path:
    target = record_path(str(record["code"]), int(record["year"]), directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def load_records(directory: Path | None = None) -> list[dict[str, Any]]:
    """读回 data/extracted/ 下所有 `代码_年份.json`。

    只读符合命名约定的文件：pipeline_input.json 是汇总产物，
    不是单条记录，读进来只会制造困惑。
    """
    folder = directory or PATHS.extracted
    if not folder.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.json")):
        if path.name == PATHS.pipeline_input.name:
            continue
        if not re.fullmatch(r"[A-Za-z0-9]+_\d{4}", path.stem):
            continue
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as err:
            raise ExtractError(f"{path} 不是合法 JSON：{err}") from err
    return records


def latest_by_code(records: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """同一家公司多年份时取**最新年份**（可比公司一年只进一次表）。"""
    newest: dict[str, Mapping[str, Any]] = {}
    for record in records:
        code = str(record.get("code") or "")
        if not code:
            continue
        current = newest.get(code)
        if current is None or int(record.get("year") or 0) > int(current.get("year") or 0):
            newest[code] = record
    return newest
