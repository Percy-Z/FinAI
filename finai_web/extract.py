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
    # 百万级单位。顺序要紧：带「单位：」的写法先匹配，最后才是裸的「百万元」。
    # IFRS 式年报（华为、中国移动等）不在表头写「单位：」，而是写成
    # 「（人民币百万元）」或直接标在表上方 —— 只认前者会漏，按 1 元处理，
    # 结果会差 6 个数量级。
    ("单位：百万元", 1_000_000.0, "百万元"),
    ("单位:百万元", 1_000_000.0, "百万元"),
    ("（人民币百万元）", 1_000_000.0, "百万元"),
    ("(人民币百万元)", 1_000_000.0, "百万元"),
    ("人民币百万元", 1_000_000.0, "百万元"),
    ("人民币千元", 1_000.0, "千元"),
    ("人民币万元", 10_000.0, "万元"),
    ("百万元", 1_000_000.0, "百万元"),
    # 英文报表（美国 10-K）。顺序要紧：Apple 的表头同时写着
    # 「In millions … which are reflected in thousands」—— 先匹配 millions，
    # 否则会被后面的 thousands 抢走，金额差 1000 倍。
    ("In millions", 1_000_000.0, "millions"),
    ("in millions", 1_000_000.0, "millions"),
    ("In billions", 1_000_000_000.0, "billions"),
    ("in billions", 1_000_000_000.0, "billions"),
    ("In thousands", 1_000.0, "thousands"),
    ("in thousands", 1_000.0, "thousands"),
)

# 币种。顺序即优先级：先认不会歧义的（人民币 / 港元），最后才是「$」——
# 「$」在港币、澳元、新加坡元的报表里都会出现，只有前面都没命中时才当美元。
CURRENCY_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("CNY", ("人民币", "RMB", "CNY")),
    ("HKD", ("港元", "港币", "HK$")),
    ("USD", ("$", "USD", "U.S. dollar", "US$")),
)


def detect_currency(pages: Sequence[str]) -> str:
    """判断报表用哪种货币计价。认不出返回空串（不猜）。"""
    joined = normalize_text("\n".join(pages[:60]))
    for currency, markers in CURRENCY_MARKERS:
        if any(marker in joined for marker in markers):
            return currency
    return ""


# 报表期末（会计期间截止日）。为什么需要它：
#   美国公司的财年不是自然年（Apple FY2025 止于 2025-09-27），
#   只写「2025A」会让人以为口径是 2025-12-31，和从 Wind/Choice 导出的
#   基准日行情放在一起比倍数时，就会混进几个月的口径差。
_EN_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
)
_PERIOD_END_EN = re.compile(
    rf"(?:fiscal year ended|fiscal years ended|years ended|year ended)\s+"
    rf"({_EN_MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})",
    re.IGNORECASE,
)
_PERIOD_END_CN = re.compile(r"(\d{4})\s*年\s*12\s*月\s*31\s*日")
_MONTH_NUMBERS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def detect_period_end(pages: Sequence[str]) -> str:
    """抽出报表期末（ISO 日期）。抽不到返回空串。"""
    joined = normalize_text("\n".join(pages[:80]))
    match = _PERIOD_END_EN.search(joined)
    if match:
        month = _MONTH_NUMBERS.get(match.group(1).lower())
        if month:
            return f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(2)):02d}"
    match = _PERIOD_END_CN.search(joined)
    if match:
        return f"{int(match.group(1)):04d}-12-31"
    return ""

# 一行里的数字：千分位、小数、括号负数（财报惯例）
_NUMBER = re.compile(r"\(?-?\d[\d,]*\.?\d*\)?")

# 这些词跟在科目名后面时说明这行是衍生指标（「应收账款周转率」），不是报表科目
_DERIVED_TAIL = ("率", "周转", "账龄", "占比", "比重")

# 排印字符归一化。英文年报几乎一定用弯引号与不换行空格：
#   Apple 的合并资产负债表写的是 "Total shareholders’ equity"（U+2019），
#   而科目表里写的是直引号 "'" —— 不归一化就永远匹配不上，而且是**静默**匹配不上。
# 只做替换，不改动任何数字或汉字。
_TYPO_FIXES: tuple[tuple[str, str], ...] = (
    ("\u2019", "'"),   # ’
    ("\u2018", "'"),   # ‘
    ("\u201c", '"'),   # “
    ("\u201d", '"'),   # ”
    ("\u00a0", " "),   # 不换行空格
    ("\u2212", "-"),   # 减号 −
)


def normalize_text(text: str) -> str:
    """把排印字符换成 ASCII 写法，让科目表只维护一套写法。"""
    for fancy, plain in _TYPO_FIXES:
        if fancy in text:
            text = text.replace(fancy, plain)
    return text

# 报表正文页的特征词。用来区分「正文」与「五年摘要 / 管理层讨论 / 附注」——
# 同一个科目名在这三类页上都会出现，而数值口径不同：
#   华为第 7 页「总资产 190,961」是五年前的数字，第 71 页「资产总计 1,334,930」才是本期。
# 只看「哪页先出现」会抽到摘要页，所以正文页要加权。
STATEMENT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("balance", ("资产负债表", "财务状况表", "CONSOLIDATED BALANCE SHEETS")),
    ("income", ("利润表", "收益表", "CONSOLIDATED STATEMENTS OF OPERATIONS")),
    ("cashflow", ("现金流量表", "CONSOLIDATED STATEMENTS OF CASH FLOWS")),
)


def _page_kinds(page_text: str) -> set[str]:
    """判断这一页属于哪张报表（可同时命中多张：有的年报把三表排在同一页）。"""
    return {
        kind
        for kind, markers in STATEMENT_MARKERS
        if any(marker in page_text for marker in markers)
    }


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


# 只含 1~2 位、没有千分位也没有括号的整数，看着像「附注编号」而不是金额。
# 真实的金额要么带千分位（1,334,930），要么带小数，要么带括号表示负数（(135)）。
_NOTE_REF = re.compile(r"^\d{1,2}$")


def _parse_amount(text: str) -> tuple[float | None, str]:
    """从「科目名之后的那段文字」里取金额，返回 (金额, 说明)。

    报表在这段文字里通常依次放着：附注编号、本期数、上期数。例如华为第 71 页：

        物业、厂房及设备   14      189,578    176,565
                          ↑附注号    ↑要的就是这个

    所以不能无脑取第一个数字 —— 那是附注编号，差三个数量级。
    判断规则：如果第一个数字是 1~2 位、没有千分位、也没有括号，而且后面还有
    别的数字，就当它是附注编号跳过，并把这个动作写进说明里（可追溯）。
    """
    tokens = [match.group(0) for match in _NUMBER.finditer(text)]
    if not tokens:
        return None, ""
    skipped = ""
    index = 0
    if len(tokens) >= 2 and _NOTE_REF.fullmatch(tokens[0]):
        skipped = f"跳过附注编号 {tokens[0]}"
        index = 1
    token = tokens[index]
    negative = token.startswith("(") or token.startswith("-")
    cleaned = token.strip("()-").replace(",", "")
    if not cleaned or cleaned == ".":
        return None, skipped
    try:
        value = float(cleaned)
    except ValueError:
        return None, skipped
    return (-value if negative else value), skipped


def _iter_lines(page_text: str) -> Iterable[str]:
    for raw in page_text.splitlines():
        line = raw.strip()
        if line:
            yield line


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
        - 标记为 aggregate 的科目在同一页出现多次时**相加**（报表把「借款」
          拆成非流动与流动两段列示，只取一处会漏）。
        - 标记为 anchored 的标签要求出现在行首（「收入」不能命中「其他收入」）。
        - 科目名之后先跳过附注编号再取金额（见 `_parse_amount`）。
        - 最后套用 schema.CONTEXT_RULES 处理「科目名与数字分两行」的版式。
    """
    wanted = {
        account.name: account
        for account in schema.ACCOUNTS
        if accounts is None or account.name in accounts
    }
    candidates: dict[str, list[Hit]] = {}
    page_kinds: dict[int, set[str]] = {}
    page_density: dict[int, int] = {}

    for page_number, page_text in enumerate(pages, start=1):
        if not page_text:
            continue
        page_text = normalize_text(page_text)
        page_kinds[page_number] = _page_kinds(page_text)
        scale, unit_name = detect_scale(page_text)
        # 合并口径：中文报表写「合并」，英文 10-K 的报表页写 "CONSOLIDATED"。
        # 不做这个判断，英文报表的每一页都会被标成「可能是母公司报表」并降置信度。
        is_consolidated = "合并" in page_text or "CONSOLIDATED" in page_text.upper()
        for name, account in wanted.items():
            page_hits: list[Hit] = []
            for line in _iter_lines(page_text):
                label = next(
                    (
                        item
                        for item in account.labels
                        if (
                            line.startswith(item)
                            if item in schema.ANCHORED_LABELS
                            else item in line
                        )
                    ),
                    None,
                )
                if label is None:
                    continue
                tail = line.split(label, 1)[1]
                if any(key in tail[:6] for key in _DERIVED_TAIL):
                    continue
                value, note = _parse_amount(tail)
                if value is None:
                    continue
                confidence = 0.9 if is_consolidated else 0.6
                if unit_name == "未标注":
                    confidence = min(confidence, 0.5)
                if name == "revenue" and label == "营业收入" and "营业总收入" in page_text:
                    # 两行都在的页面，营业收入是分项，营业总收入才是我们要的
                    confidence = min(confidence, 0.7)
                if not is_consolidated:
                    note = (note + "；" if note else "") + "本页没有『合并』二字，可能是母公司报表"
                page_hits.append(
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
                if not account.aggregate:
                    break  # 非合计科目：这一页取第一处就够

            if not page_hits:
                continue
            page_density[page_number] = page_density.get(page_number, 0) + 1
            if account.aggregate and len(page_hits) > 1:
                # 同一个标题在报表里分「非流动 / 流动」两段列示，两段都要。
                total = sum(hit.value_yuan for hit in page_hits)
                # 先记下各段的数再去改第一个 hit —— 否则明细会被改后的值污染，
                # 打印出来变成「90,678,000,000 + 78,328,000,000」这种自相矛盾的说明。
                parts = " + ".join(f"{hit.value_yuan:,.0f}" for hit in page_hits)
                merged = page_hits[0]
                merged.value_yuan = total
                merged.raw_text = " + ".join(hit.raw_text for hit in page_hits)[:200]
                merged.note = (merged.note + "；" if merged.note else "") + (
                    f"本页出现 {len(page_hits)} 次，已相加（{parts}）"
                )
                candidates.setdefault(name, []).append(merged)
            else:
                candidates.setdefault(name, []).append(page_hits[0])

    # 跨行科目：科目名与数字不在同一行（如华为的「净利润归属于：→ 本公司所有者」）
    for rule in schema.CONTEXT_RULES:
        if rule.account in candidates:
            continue
        hit = _scan_context(pages, rule)
        if hit is not None:
            candidates.setdefault(rule.account, []).append(hit)

    best: dict[str, Hit] = {}
    for name, hits in candidates.items():
        section = schema.ACCOUNT_BY_NAME[name].section

        def rank(hit: Hit, _section: str = section) -> tuple:
            """挑哪一处命中：先看置信度，再看是不是报表正文，最后才看页码。

            为什么要加「正文」这一档：五年摘要页与管理层讨论页里也会出现同样的
            科目名（而且往往排在更前面），只按页码或出现顺序会抽到那些数。
            """
            return (
                hit.confidence,
                _section in page_kinds.get(hit.page, set()),
                page_density.get(hit.page, 0),
                -hit.page,
            )

        winner = max(hits, key=rank)
        if len(hits) > 1:
            others = "、".join(str(hit.page) for hit in hits if hit is not winner)
            winner.note = (winner.note + "；" if winner.note else "") + f"另有 {len(hits) - 1} 处（第 {others} 页）"
        best[name] = winner
    return best


def _scan_context(pages: Sequence[str], rule: "schema.ContextRule") -> Hit | None:
    """按「触发词 → 后面几行里找取值标签」的规则取一个科目。"""
    for page_number, page_text in enumerate(pages, start=1):
        if not page_text or rule.trigger not in page_text:
            continue
        lines = list(_iter_lines(page_text))
        scale, unit_name = detect_scale(page_text)
        for index, line in enumerate(lines):
            if rule.trigger not in line:
                continue
            for follow in lines[index + 1 : index + 1 + rule.lookahead]:
                if rule.value_label not in follow:
                    continue
                tail = follow.split(rule.value_label, 1)[1]
                value, note = _parse_amount(tail)
                if value is None:
                    continue
                note = (note + "；" if note else "") + (
                    f"由版面规则抽出（第 {index + 1} 行「{rule.trigger}」→ "
                    f"下一批行里的「{rule.value_label}」）"
                )
                return Hit(
                    account=rule.account,
                    label=f"{rule.trigger} / {rule.value_label}",
                    value_yuan=value * scale,
                    raw_text=follow[:160],
                    page=page_number,
                    unit=unit_name,
                    scale=scale,
                    confidence=rule.confidence,
                    note=note,
                )
    return None


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
    currency = detect_currency(pages)
    period_end = detect_period_end(pages)
    units = [hit.unit for hit in matches.values() if hit.unit and hit.unit != "未标注"]
    unit_hint = max(set(units), key=units.count) if units else ""

    record: dict[str, Any] = {
        "code": code,
        "name": name,
        "year": year,
        "period": f"{year}A",
        "period_end": period_end,
        "pdf": str(pdf_path) if pdf_path else "",
        "sha256": pdf_sha256(pdf_path) if pdf_path else "",
        "pages": len(pages),
        "has_text_layer": text_chars > 200,
        "backend": backend,
        "method": method,
        "currency": currency,
        "unit_hint": unit_hint,
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

    if currency and currency != "CNY":
        record["notes"].append(
            f"这份报表以 {currency} 计价（单位 {unit_hint or '未知'}），而流水线的单位约定是"
            "人民币元 —— 入账前必须按基准日汇率换算。本模块不做汇率转换，也不假设 1:1，"
            "避免把「美元百万」当成「人民币元」直接算进估值。"
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
