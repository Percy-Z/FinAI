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
from dataclasses import dataclass, replace
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


# CID 码：PDF 字体缺少 ToUnicode 映射时，抽取器只能给出字符编号，
# 形如 (cid:14062)。这不是「文字层缺失」，而是「文字层不可用」——
# 本地解析再怎么换库也救不回来，只能 OCR 或走云端解析（Firecrawl）。
_CID_TOKEN = re.compile(r"\(cid:\d+\)")


def cid_ratio(pages: Sequence[str]) -> float:
    """CID 码字符占总字符的比例。0 表示正常，接近 1 表示文字层不可用。"""
    total = sum(len(page) for page in pages)
    if not total:
        return 0.0
    cid_chars = sum(len(match.group(0)) for page in pages for match in _CID_TOKEN.finditer(page))
    return cid_chars / total


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
# 中文数字日期：二零二五年十二月三十一日（港交所/台湾年报写法）
_PERIOD_END_CJK = re.compile(
    r"([〇零一二三四五六七八九]{4})年([一二三四五六七八九十]{1,3})月([一二三四五六七八九十]{1,3})日"
)
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
    match = _PERIOD_END_CJK.search(joined)
    if match:
        # 中文日期在一份年报里会出现很多次（股东会日期、报告日期…），
        # 第一个不一定是期末。按上下文选：前面有「於/截至」、后面跟「止年度」的，
        # 以及 12 月 31 日的，优先。
        best: tuple[int, str] | None = None
        for candidate in _PERIOD_END_CJK.finditer(joined):
            year = cjk_digits_to_year(candidate.group(1))
            month = cjk_number(candidate.group(2))
            day = cjk_number(candidate.group(3))
            if not (year and month and day):
                continue
            prefix = joined[max(0, candidate.start() - 6) : candidate.start()]
            suffix = joined[candidate.end() : candidate.end() + 4]
            score = 0
            if any(word in prefix for word in ("於", "于", "截至")):
                score += 2
            if any(word in suffix for word in ("止年度", "止期间", "结算", "結算")):
                score += 2
            if month == 12 and day == 31:
                score += 1
            value = f"{year:04d}-{month:02d}-{day:02d}"
            if best is None or score > best[0]:
                best = (score, value)
        if best is not None:
            return best[1]
    return ""

# 一行里的数字：千分位、小数、括号负数（财报惯例）
_NUMBER = re.compile(r"\(?-?\d[\d,]*\.?\d*\)?")

# 这些词跟在科目名后面时说明这行是衍生指标（「应收账款周转率」），不是报表科目
_DERIVED_TAIL = ("率", "周转", "账龄", "占比", "比重")

# 中日韩汉字范围：用来判断「标签前面是不是还有字」。
# 例：「长短期借款」里含「短期借款」、「总股本」里含「股本」、「借款利息费用」里含「利息费用」——
# 这些都不是那可个科目，但朴素子串匹配会把它们当成科目，且**静默给出错数**。
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def _is_word_char(char: str) -> bool:
    """这个字符是否会让标签往左延伸成一个不同的词。"""
    if not char:
        return False
    return bool(char.isalnum()) or bool(_CJK.match(char))


def label_position(text: str, label: str) -> int:
    """找标签在行里的位置；前面还连着汉字/字母/数字的匹配一律不算。

    为什么不只看「行首」：报表里科目常常带序号或前缀（「一、营业总收入」），
    要求行首会漏。这里只挡「左边还有字」的情况 —— 那才是另一个科目：

        长短期借款 239,284      ← 不是「短期借款」，是借款总额
        总出资相当于总股本…      ← 不是「股本」
        借款利息费用 (6,972)     ← 不是利润表的「利息费用」

    标签右边接着字是允许的，而且是我们想要的（「应收账款及应收票据」要能命中「应收账款」）。
    """
    start = text.find(label)
    while start >= 0:
        head_ok = start == 0 or not _is_word_char(text[start - 1])
        tail_ok = True
        if label in schema.EXACT_LABELS:
            after = text[start + len(label) : start + len(label) + 1]
            tail_ok = not _is_word_char(after)
        if head_ok and tail_ok:
            return start
        start = text.find(label, start + 1)
    return -1


def match_label(text: str, label: str) -> int:
    """统一的标签匹配入口：行首锚定 + 完整标题 + 前面不接字，三条规则一起用。

    单独一个函数是为了让「单列扫描」和「按列取数」走同一套判断 ——
    之前两处各写一遍，结果按列取数那条路漏掉了「完整标题」这条规则，
    于是腾讯的「股本溢价」被当成「股本」。
    """
    if label in schema.ANCHORED_LABELS:
        if not text.startswith(label):
            return -1
        if label in schema.EXACT_LABELS:
            after = text[len(label) : len(label) + 1]
            if _is_word_char(after):
                return -1
        return 0
    return label_position(text, label)


# 报表用破折号表示「未列示」：股本 33 – –
_DASH = re.compile(r"(?<!\d)[-–—](?!\d)")


def is_dash_row(text: str) -> bool:
    """这一行的金额是不是用破折号占位（未列示）。"""
    return bool(_DASH.search(text))

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

# 繁体 → 简体。港交所（腾讯这类）与台湾的年报是繁体，而科目表维护的是简体写法；
# 不转换的话，`資產總額` 永远匹配不上 `资产总额`，而且是**静默**匹配不上。
# 只放财务与年报常见字，且只做 1:1 转换；没覆盖到的字保持原样（会表现为「没抽到」，不会给错数）。
_TRADITIONAL_TO_SIMPLIFIED: dict[str, str] = {
    "資": "资", "產": "产", "負": "负", "債": "债", "權": "权", "損": "损", "現": "现",
    "淨": "净", "潤": "润", "應": "应", "賬": "账", "帳": "账", "貨": "货", "業": "业",
    "廠": "厂", "設": "设", "備": "备", "貸": "贷", "賃": "赁", "稅": "税", "費": "费",
    "總": "总", "額": "额", "積": "积", "攤": "摊", "銷": "销", "舊": "旧", "營": "营",
    "運": "运", "開": "开", "職": "职", "員": "员", "長": "长", "銀": "银", "據": "据",
    "無": "无", "譽": "誉", "東": "东", "間": "间", "報": "报", "導": "导", "體": "体",
    "計": "计", "狀": "状", "況": "况", "綜": "综", "務": "务", "術": "术", "場": "场",
    "廣": "广", "虧": "亏", "聯": "联", "佔": "占", "億": "亿", "萬": "万", "圓": "圆",
    "幣": "币", "櫃": "柜", "訊": "讯", "騰": "腾", "賣": "卖", "買": "买", "價": "价",
    "匯": "汇", "兌": "兑", "單": "单", "雙": "双", "週": "周", "餘": "余", "節": "节",
    "給": "给", "結": "结", "續": "续", "經": "经", "處": "处", "視": "视", "轉": "转",
    "變": "变", "動": "动", "對": "对", "衝": "冲", "儲": "储", "換": "换", "隨": "随",
    "屬": "属", "遞": "递", "險": "险", "際": "际", "歸": "归", "壞": "坏", "審": "审",
    "議": "议", "訟": "讼", "減": "减", "規": "规", "標": "标", "準": "准", "確": "确",
    "認": "认", "測": "测", "試": "试", "驗": "验", "證": "证", "穩": "稳", "極": "极",
    "興": "兴", "諮": "咨", "詢": "询", "庫": "库", "倉": "仓", "鑽": "钻", "們": "们",
    "個": "个", "從": "从", "來": "来", "為": "为", "說": "说", "這": "这", "麼": "么",
    "與": "与", "並": "并", "內": "内", "發": "发", "覺": "觉", "學": "学", "習": "习",
    "點": "点", "數": "数", "類": "类", "別": "别", "統": "统", "維": "维", "護": "护",
    "責": "责", "頭": "头", "項": "项", "條": "条", "則": "则", "預": "预", "範": "范",
    "圍": "围", "籤": "签", "網": "网", "絡": "络", "線": "线", "樂": "乐", "聽": "听",
    "頻": "频", "載": "载", "歲": "岁", "賺": "赚", "蝕": "蚀", "僱": "雇", "傭": "佣",
    "償": "偿", "撥": "拨", "抵": "抵", "質": "质", "贊": "赞", "贈": "赠", "免": "免",
    "退": "退", "罰": "罚", "補": "补", "貼": "贴", "津": "津", "薪": "薪", "酬": "酬",
    "獎": "奖", "醫": "医", "療": "疗", "保": "保", "養": "养", "齡": "龄", "潛": "潜",
    # 第二批：从腾讯年报的报表页里逐字比对出来的（先用脚本列出未覆盖的字，再补）
    "併": "并", "劃": "划", "勵": "励", "將": "将", "後": "后", "會": "会", "沖": "冲",
    "滙": "汇", "獲": "获", "簽": "签", "紹": "绍", "組": "组", "繳": "缴", "於": "于",
    "楊": "杨", "註": "注", "讓": "让", "財": "财", "購": "购", "進": "进", "過": "过",
    "還": "还", "關": "关", "馬": "马", "擔": "担", "頁": "页", "題": "题", "樣": "样",
    "觀": "观", "圖": "图", "書": "书", "畫": "画", "邊": "边", "遊": "游", "戲": "戏",
    "獨": "独", "錢": "钱", "幹": "干", "幾": "几", "態": "态", "慣": "惯", "燈": "灯",
    "滅": "灭", "絕": "绝", "製": "制", "產": "产", "電": "电", "腦": "脑", "雙": "双",
}


def normalize_text(text: str) -> str:
    """把排印字符换成 ASCII、把繁体换成简体，让科目表只维护一套写法。"""
    for fancy, plain in _TYPO_FIXES:
        if fancy in text:
            text = text.replace(fancy, plain)
    if any(character in _TRADITIONAL_TO_SIMPLIFIED for character in text):
        text = "".join(_TRADITIONAL_TO_SIMPLIFIED.get(character, character) for character in text)
    return text


# 中文数字：港交所年报的年份与日期写作「二零二五年十二月三十一日」。
_CJK_DIGITS = {
    "〇": "0", "零": "0", "一": "1", "二": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
}


def cjk_digits_to_year(text: str) -> int | None:
    """把「二零二五」这样的四位中文数字年份读成 2025。不是年份就返回 None。"""
    digits = "".join(_CJK_DIGITS.get(character, "") for character in text)
    if len(digits) == 4 and digits.isdigit():
        year = int(digits)
        if 1900 <= year <= 2100:
            return year
    return None


def cjk_number(text: str) -> int | None:
    """读中文数字（一~九十九），用来解析「十二月三十一日」这种日期。"""
    if not text:
        return None
    if text in _CJK_DIGITS:
        return int(_CJK_DIGITS[text])
    if "十" in text:
        left, _, right = text.partition("十")
        if left and left not in _CJK_DIGITS:
            return None
        if right and right not in _CJK_DIGITS:
            return None
        tens = int(_CJK_DIGITS[left]) if left else 1
        ones = int(_CJK_DIGITS[right]) if right else 0
        return tens * 10 + ones
    return None

# 报表正文页的特征词。用来区分「正文」与「五年摘要 / 管理层讨论 / 附注」——
# 同一个科目名在这三类页上都会出现，而数值口径不同：
#   华为第 7 页「总资产 190,961」是五年前的数字，第 71 页「资产总计 1,334,930」才是本期。
# 只看「哪页先出现」会抽到摘要页，所以正文页要加权。
STATEMENT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("balance", ("资产负债表", "财务状况表", "CONSOLIDATED BALANCE SHEETS")),
    ("income", ("利润表", "收益表", "CONSOLIDATED STATEMENTS OF OPERATIONS")),
    ("cashflow", ("现金流量表", "CONSOLIDATED STATEMENTS OF CASH FLOWS")),
)


def _page_kinds(page_text: str, *, title_lines: int = 6) -> set[str]:
    """判断这一页属于哪张报表，**只看页面顶部的标题区**。

    为什么只看标题区：正文里到处都会提到「综合收益表」「财务状况表」（附注的引用、
    五年摘要的小标题），按全文匹配会让「五年財務摘要」页被当成利润表 ——
    腾讯的摘要页就有「收入 560,118 554,552 609,015 660,257 751,766」这一行，
    一旦它被当成利润表，就会抢走真正的本期收入。

    报表页的标题总在开头几行，所以只扫前 title_lines 行。
    """
    head = "\n".join(
        [line for line in page_text.splitlines() if line.strip()][:title_lines]
    )
    return {
        kind
        for kind, markers in STATEMENT_MARKERS
        if any(marker in head for marker in markers)
    }


# 摘要页的标题特征。这些页的科目名与报表页一样，但**口径不同**：
# 腾讯第 4 页「財務概要 / 簡明綜合全面收益表」列了 2021–2025 五年、数字取整，
# 把它当利润表会抢走真正的本期收入（560,118 vs 751,766）。
_SUMMARY_MARKERS = (
    "財務概要", "财务概要", "財務摘要", "财务摘要", "摘要", "簡明", "简明", "選定的財務資料",
)


def is_summary_page(page_text: str, *, title_lines: int = 6) -> bool:
    """这一页是不是「财务概要/摘要」页（只看标题区）。"""
    head = "\n".join(
        [line for line in page_text.splitlines() if line.strip()][:title_lines]
    )
    return any(marker in head for marker in _SUMMARY_MARKERS)


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


@dataclass
class PdfContent:
    """一次读进来的 PDF 内容：页文本 + （可选的）词坐标。

    词坐标是「多年列示」按列取数的前提（见 columns.py）。只有 pdfplumber 给得到；
    其它后端拿不到坐标，就只给文本 —— 那时按列取数会自动跳过，回落到单列扫描。
    """

    pages: list[str]
    words: list[list[dict[str, Any]]] | None
    backend: str


def read_pdf(pdf_path: str | Path) -> PdfContent:
    """把 PDF 逐页读成文本（并用 pdfplumber 时附带词坐标）。

    三个后端按顺序试：pdfplumber（表格最准，且给词坐标）→ pypdf → PyMuPDF。
    一个都没有时抛 ExtractError 并告诉使用者装哪个 —— 不返回空列表假装成功。
    """
    path = Path(pdf_path)
    if not path.exists():
        raise ExtractError(f"PDF 不存在：{path}")

    try:
        import pdfplumber  # type: ignore[import-not-found]

        with pdfplumber.open(path) as pdf:
            pages: list[str] = []
            words: list[list[dict[str, Any]]] = []
            for page in pdf.pages:
                pages.append(page.extract_text() or "")
                words.append(
                    [
                        {
                            "text": str(word.get("text", "")),
                            "x0": float(word.get("x0", 0.0)),
                            "x1": float(word.get("x1", 0.0)),
                            "top": float(word.get("top", 0.0)),
                        }
                        for word in page.extract_words()
                    ]
                )
            return PdfContent(pages=pages, words=words, backend="pdfplumber")
    except ImportError:
        pass

    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]

        reader = PdfReader(str(path))
        return PdfContent(
            pages=[(page.extract_text() or "") for page in reader.pages],
            words=None,
            backend="pypdf",
        )
    except ImportError:
        pass

    try:
        import fitz  # type: ignore[import-not-found]  # PyMuPDF

        with fitz.open(path) as doc:
            return PdfContent(
                pages=[page.get_text() for page in doc], words=None, backend="pymupdf"
            )
    except ImportError:
        pass

    raise ExtractError(
        "没有可用的 PDF 解析库，本地抽不了。装一个即可（任选其一）：\n"
        "    python -m pip install pdfplumber     # 推荐，表格最准\n"
        "    python -m pip install pypdf\n"
        "    python -m pip install PyMuPDF\n"
        "或者加 --firecrawl 走 Firecrawl 的 PDF 解析（需要 FIRECRAWL_API_KEY）。"
    )


def read_pages(pdf_path: str | Path) -> tuple[list[str], str]:
    """把 PDF 逐页读成文本，返回 (页文本列表, 用的后端名)。"""
    content = read_pdf(pdf_path)
    return content.pages, content.backend


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
    # 整行的值是破折号（报表表示「未列示」）时，唯一的数字多半是附注编号。
    # 腾讯的「股本 33 – –」就是这种：真正的股本只在附注里，正文用「–」占位。
    # 不拦的话会把附注编号 33 当成股本（而且可能恰好对得上，属于「错得看不出来」）。
    if (
        len(tokens) == 1
        and _NOTE_REF.fullmatch(tokens[0])
        and is_dash_row(text)
    ):
        return None, "该行金额用破折号占位（未列示），不把附注编号当金额"
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


# 行首修饰：序号、项目符号、编号（"1." "（1）" "•"）。真实年报的报表行偶尔带这些
# 前缀，科目名本身还是从它们之后开始。
_ROW_LEAD = re.compile(
    r"^(?:[（(]?\s*[0-9一二三四五六七八九十]{1,3}\s*[）)、.．]?|[•·▪※-])\s*"
)

# 科目名与金额之间常夹一列「附注」（七、38 / 五.12 / 附注三、5）。不剥掉它，
# 附注里的序号会被当成金额 —— 688278 的资产负馈表整片抽不到就是这个原因。
_NOTE_CHAPTER = re.compile(
    r"^\s*[（(]?\s*(?:附注|注[：:]?)?\s*[一二三四五六七八九十百]{1,4}\s*[、.．,，]?\s*\d{1,4}\s*[)）]?\s+"
)

# 附注列的第二种写法：**只有序号，没有章节号** ——「利息支出  45  175,775,959.12」。
# 茅台就按这种排法，不剥掉会把 45 当成利息支出（2026-10-15 实测：600519 的 11 个
# 科目里有 8 个抽到的是附注序号）。
_BARE_NOTE = re.compile(r"^[（(]?\s*(\d{1,4})\s*[)）]?\s+(?=\S)")

# 「更像金额」的数：四位以上、带千分位、或者带小数点
_AMOUNT_LIKE = re.compile(r"[（(]?\s*-?(?:\d{1,3}(?:,\d{3})+|\d{4,}|\d+\.\d)")

# 数字后面紧跟中文标题：附注目录里的「所得税费用(1). 所得税费用表」。
# 那个 (1) 会被 _parse_number 当成负一（会计上括号表示负数），于是抽到一个 -1。
_HEADING_TAIL = re.compile(r"^[.．、,，)）\s]*[\u4e00-\u9fff]")

# 多大的数以下才算「像附注序号」。判标题行时要用它把「58,334,693.18 元」这种
# 真金额排除掉 —— 那种行的数后面也跟中文（单位），不能一律拒掉。
_NOTE_INDEX_LIMIT = 1000

# 科目名后面的**括号补充**：财政部模板常写「实收资本（或股本）」、
# 「所有者权益（或股东权益）合计」。它是科目名的一部分，不是内容 ——
# 不认的话那一截留在尾巴里，整行反而匹配不上（茅台 2025 年报第 58/60 页）。
# 只认「短、且不含数字」的括号：含数字的括号是金额（会计上还表示负数）。
_LABEL_TAIL_FRAGMENT = re.compile(r"^[（(][^（()）\d]{0,8}[）)]")


def _is_heading_row(trimmed: str, number: "re.Match[str]", value: float) -> bool:
    """这一行是附注目录里的标题行吗（「所得税费用(1). 所得税费用表」）。"""
    if abs(value) >= _NOTE_INDEX_LIMIT:
        return False
    return bool(_HEADING_TAIL.match(trimmed[number.end() :]))


def _strip_note(text: str) -> str:
    """剥掉科目名与金额之间的附注列（两种写法都认）。

    剥的条件很紧：那个小整数后面**还跟着一个更像金额的数**才敢剥。
    否则「股本 1000」这种真金额会被误剥掉，改成取后一列的数（那是上一年度的数）。
    """
    trimmed = _NOTE_CHAPTER.sub("", text, count=1).lstrip()
    match = _BARE_NOTE.match(trimmed)
    if not match:
        return trimmed
    rest = trimmed[match.end() :]
    if not _AMOUNT_LIKE.match(rest):
        return trimmed
    candidate = match.group(1)
    if len(candidate) >= 4 and int(candidate) > 300:
        return trimmed  # 四位以上的大整数不像附注序号
    return rest


def _row_value(
    line: str, labels: Sequence[str], next_line: str | None = None
) -> tuple[str, float, str] | None:
    """这一行是某个科目的表行吗？是就返回 (命中的科目名, 数值, 原文片段)。

    判定三条，缺一不可：
        ① 科目名从行首开始（允许「1.」这类序号前缀）
        ② 科目名与数字之间只允许出现一列附注（剥掉），不能有别的东西
        ③ 剥完之后紧跟一个数字

    为什么必须这么严：真实年报里拿科目名做子串匹配是灾难现场 ——

        「非流动负债合计」里含「负债合计」    → 抽到非流动负债，倒挤出的差额变负数
        「固定资产改造」里含「固定资产」      → 抽到现金流量表的科目
        「报告期末，高新地产总资产72.27亿元」 → 一句叙述被当成报表数字

    这三类在演示用的合成数据里都不会出现，真实年报上第一条就中（000661 实测）。

    next_line 是「科目名被排版拆行」的那条兜底：688278 的归母净资产排成了
        「归属于母公司所有者权益」/「3,449,578,652.54 …」/「（或股东权益）合计」
    三行 —— 前半截 + 金额 + 后半截。这时按「这一行是某个标签的前半截、下一行是
    金额」认下来，并在原文里标明是拼出来的。
    """
    text = _ROW_LEAD.sub("", line).strip()
    # 取**最长**的命中标签：「实收资本（或股本）」不能被「实收资本」截住，
    # 同理「归属于母公司所有者权益合计」也不该被它的前缀截住。
    label = max((item for item in labels if text.startswith(item)), key=len, default=None)
    if label is not None:
        tail = text[len(label) :]
        fragment = _LABEL_TAIL_FRAGMENT.match(tail.strip())
        if fragment:
            # 「实收资本（或股本） 37 1,252,270,215.00」：括号那截算科目名
            tail = tail.strip()[fragment.end() :]
        if any(key in tail[:6] for key in _DERIVED_TAIL):
            return None  # 「应收账款周转率 3.2」这类衍生指标
        trimmed = _strip_note(tail)
        number = _NUMBER.match(trimmed)
        if number is None:
            return None
        # 「股本 33 – –」：整行金额用破折号表示「未列示」，那个 33 其实是附注编号。
        # 不拦的话会把附注编号当成股本（腾讯实测），而且它可能恰好落在正常量级上，
        # 属于「错得看不出来」。
        if is_dash_row(trimmed) and _NOTE_REF.fullmatch(number.group(0)):
            return None
        value = _parse_number(trimmed)
        if value is None:
            return None
        if _is_heading_row(trimmed, number, value):
            return None  # 「所得税费用(1).所得税费用表」这类附注目录行
        return label, value, text[:160]

    if next_line is None or not text:
        return None
    prefix = next((item for item in labels if item.startswith(text)), None)
    if prefix is None:
        return None  # 「应收账款周转率 3.2」这类衍生指标
    numbers = _strip_note(next_line.strip())
    matched = _NUMBER.match(numbers)
    if matched is None:
        return None
    value = _parse_number(numbers)
    if value is None:
        return None
    if _is_heading_row(numbers, matched, value):
        return None
    return prefix, value, f"{text} {numbers[:60]}"


def _row_candidates(page_text: str) -> Iterable[tuple[int, str, str | None, bool]]:
    """逐行产出候选表行；标签单独占一行时，与后面两行拼起来再试一次。

    年报里长科目名常被排版拆行：「1.归属于母公司股东的净利润」一行、金额在下一行；
    「归属于母公司所有者权益」+「合计 24,869,…」。只按单行匹配会整条丢掉 ——
    688278 的归母净利润与归母净资产就是这么丢的。第三个返回值是「下一行的原文」，
    给「科目名被拆成前半截 + 金额 + 后半截」的情况用。第四个返回值标出这一行是
    **拼接出来的**：拼接只给最紧的匹配规则用（见 `_match_row`）。
    """
    lines = page_text.splitlines()
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        following = ""
        for extra in (1, 2):
            if index + extra >= len(lines):
                break
            candidate = lines[index + extra].strip()
            if candidate:
                following = candidate
                break
        yield index, line, following or None, False
        # 判断「这一行有没有数字」要先把行首序号去掉：否则「1.归属于母公司股东的净利润」
        # 会被那个 1 当成已有数字，跨行拼接就永远不会触发（688278 实测）。
        if _NUMBER.search(_ROW_LEAD.sub("", line)) is not None:
            continue
        merged = line
        for extra in (1, 2):
            if index + extra >= len(lines):
                break
            piece = lines[index + extra].strip()
            if not piece:
                break
            merged = f"{merged}{piece}"
            yield index, merged, piece, True


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
# 整行只有数字/千分位/括号/百分号 —— 报表里的「合计行」长这样（科目名在上一行）。
_AMOUNT_ONLY_LINE = re.compile(r"^[\d,().\-%\s]+$")


def _totals_index_below(lines: Sequence[str], start: int, *, lookahead: int = 12) -> int | None:
    """科目名单独一行、合计在下面若干行时，返回那行合计的**下标**。

    腾讯的综合收益表就是这么排的：

        收入
        增值服務 369,281 319,168
        …
        其他 8,077 7,759
        6 751,766 660,257      ← 这一行才是收入合计，而且没有科目名

    只在「标签这一行没有金额」时才启用，且要求那一行**整行都是数字**，
    所以不会误抓到普通明细行。

    lookahead 取 12 是因为合计行可能隔得比较远：腾讯的「本公司權益持有人應佔權益」
    下面还有股本、股本溢价、库存股、股份奖励计划所持股份、其他储备、保留盈利 6 行，
    合计才出现在第 7 行（而且是**没有科目名的裸合计行**）。
    """
    for offset, line in enumerate(lines[start + 1 : start + 1 + lookahead], start=1):
        stripped = line.strip()
        if not stripped or not any(ch.isdigit() for ch in stripped):
            continue
        if _AMOUNT_ONLY_LINE.fullmatch(stripped):
            return start + offset
    return None


def _totals_below(lines: Sequence[str], start: int, *, lookahead: int = 12) -> str:
    """同上，但直接返回那一行的文本。"""
    index = _totals_index_below(lines, start, lookahead=lookahead)
    return lines[index] if index is not None else ""


def _page_can_hold(
    page_kind_set: set[str],
    segments: Sequence[tuple[int, str]],
    account: "schema.Account",
    available_kinds: set[str],
) -> bool:
    """这一页能不能用来取这个科目。

    三条判断，按「谁说得更具体」的顺序：
        ① 页面标题写着这是哪张报表 → 必须是这张表（英文 10-K 也认）；
        ② 页面没有表头，但逐行的报表归属判出来了（一张表跨页的续页）→ 认它，
           否则「合并资产负债表」的第二页会整片抽不到；
        ③ 都没有 → 只有注明「可从附注取」的科目才允许（附注、管理层讨论里的
           同名表格口径不同，不能拿它当报表科目）。
    """
    if account.allow_notes:
        return True
    if page_kind_set:
        return account.section in page_kind_set
    sections = {kind.split("-", 1)[-1] for _, kind in segments if kind}
    if sections:
        return account.section in sections
    return account.section not in available_kinds


def _lenient_row(
    line: str, account: "schema.Account"
) -> tuple[str, float, str, str] | None:
    """宽松匹配：科目名可以在行中间，右边也可以先接别的字。

    只在这一页的标题明确写着报表类型时才用（见 `_match_row`）：它认得出
    「应收账款及应收票据」，也认得出「一、营业总收入」，但也更容易被子串带偏。
    """
    for item in account.labels:
        position = match_label(line, item)
        if position < 0:
            continue
        tail = line[position + len(item) :]
        if any(key in tail[:6] for key in _DERIVED_TAIL):
            continue  # 「应收账款周转率 3.2」这类衍生指标
        value, note = _parse_amount(tail)
        if value is None:
            continue
        return item, value, note, line[:160]
    return None


def _note_hint(line: str, label: str) -> str:
    """这一行剥掉附注列了吗？剥了就写进说明里。

    华为第 71 页的「物业、厂房及设备   14   189,578   176,565」：那个 14 是附注号，
    差三个数量级。抽取结果里留一句话，评审问「这个 14 为什么没用」能直接答。
    """
    text = _ROW_LEAD.sub("", line).strip()
    if not text.startswith(label):
        return ""
    tail = text[len(label) :]
    chapter = _NOTE_CHAPTER.match(tail)
    if chapter:
        return "跳过附注编号 " + chapter.group(0).strip()
    bare = _BARE_NOTE.match(tail.lstrip())
    if bare is None:
        return ""
    rest = tail.lstrip()[bare.end() :]
    if not _AMOUNT_LIKE.match(rest):
        return ""
    candidate = bare.group(1)
    if len(candidate) >= 4 and int(candidate) > 300:
        return ""
    return "跳过附注编号 " + candidate


def _totals_row(
    line: str,
    labels: Sequence[str],
    lines: Sequence[str],
    start: int | None,
) -> tuple[str, float, str, str] | None:
    """科目名单独占一行、合计在下面若干行（腾讯的「收入」）。

    只在标签这一行**完全没有金额**时才启用，且要求下面那一行整行都是数字 ——
    所以不会误抓到普通明细行。
    """
    if start is None:
        return None
    text = _ROW_LEAD.sub("", line).strip()
    label = next((item for item in labels if text == item), None)
    if label is None:
        return None
    totals = _totals_below(lines, start)
    if not totals:
        return None
    value, note = _parse_amount(totals)
    if value is None:
        return None
    note = (note + "；" if note else "") + "科目名与合计分行，取下方合计行"
    return label, value, note, totals[:160]


def _match_row(
    line: str,
    account: "schema.Account",
    following: str | None,
    lines: Sequence[str],
    start: int | None,
    trusted: bool,
) -> tuple[str, float, str, str] | None:
    """一行候选的三种取数路径，返回 (科目名, 金额, 说明, 原文)。

    顺序是「从紧到松」，越紧的规则越不容易抽到错数：

        ① `_row_value`   科目名从行首开始、中间只允许一列附注 —— 首选；
        ② `_totals_row`  科目名单独一行、这一行没有金额，合计在下面；
        ③ `_lenient_row` 允许科目名右边先接别的字、也允许不在行首 —— 只在这一页
                         的标题写着报表类型时才用（`trusted`）。

    为什么 ③ 要看 `trusted`：附注页与管理层讨论页里也有同名的表，但口径不同，
    那里只敢用最紧的规则（「固定资产改造 74,558,669.29」这种行就是靠这个挡住的）。
    `trusted` 还要与「这一行是不是拼出来的」一起看：拼接行只在最紧的规则下用，
    否则「Net sales:」+「Products 307,003」会被拼成一行，把分部收入当成总收入。
    """
    row = _row_value(line, account.labels, following)
    if row is not None:
        label, value, text = row
        return label, value, _note_hint(line, label), text
    row = _totals_row(line, account.labels, lines, start)
    if row is not None:
        return row
    if trusted:
        return _lenient_row(line, account)
    return None


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
    # 排印字符与繁体先归一化：科目表只维护一套写法。腾讯的「資產總額」、Apple 的
    # 弯引号 shareholders’ equity 不转就永远匹配不上，而且是**静默**匹配不上。
    pages = [normalize_text(text) if text else "" for text in pages]

    # 逐行的报表归属：合并 / 母公司 / 别的报表（跨页时跟着上一张表）
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

    page_kinds: dict[int, set[str]] = {}
    page_consolidated: dict[int, bool] = {}
    page_density: dict[int, int] = {}

    # 先看这份文档里出现过哪些报表页。若某种报表页存在，那么属于它的科目就**只**
    # 从这种页取数 —— 管理层讨论与附注里也有类似的表，但口径不同：
    # 华为第 65/66 页的「长短期借款」「其中：长期借款」是债务结构分析，
    # 不是合并资产负债表上的科目；拿它当 short_term_debt 会给出误导性的数。
    kinds_by_page: dict[int, set[str]] = {}
    for index, page_text in enumerate(pages, start=1):
        if page_text and not is_summary_page(page_text):
            # 摘要页不参与取数（口径不同：腾讯的五年摘要写着「簡明綜合全面收益表」）
            kinds_by_page[index] = _page_kinds(page_text)
    available_kinds = {kind for kinds in kinds_by_page.values() for kind in kinds}

    for page_number, page_text in enumerate(pages, start=1):
        if not page_text:
            continue
        if is_summary_page(page_text):
            continue
        page_kind_set = kinds_by_page.get(page_number, set())
        page_kinds[page_number] = page_kind_set
        segments = statements[page_number - 1]
        scale, unit_name = detect_scale(page_text)
        if unit_name == "未标注" and report_unit != "未标注":
            scale, unit_name = report_scale, report_unit
        # 合并口径：中文报表写「合并」，英文 10-K 的报表页写 "CONSOLIDATED"。
        # 不做这个判断，英文报表的每一页都会被标成「可能是母公司报表」并降置信度。
        is_consolidated = (
            "合并" in page_text
            or "综合" in page_text
            or "CONSOLIDATED" in page_text.upper()
        )
        page_consolidated[page_number] = is_consolidated

        # 「合计在下面」的兜底要按**非空行**的下标找（`_totals_below` 的入参）
        lines: list[str] = []
        row_of_line: dict[int, int] = {}
        for raw_index, raw_line in enumerate(page_text.splitlines()):
            stripped = raw_line.strip()
            if stripped:
                row_of_line[raw_index] = len(lines)
                lines.append(stripped)

        for name, account in wanted.items():
            if not _page_can_hold(page_kind_set, segments, account, available_kinds):
                continue
            # 页面标题写着这是哪张报表时才敢用宽松匹配；没有标题的页（附注、
            # 管理层讨论）只认「科目名从行首开始」的紧规则。
            trusted = bool(page_kind_set)
            # 资产负债表与利润表的科目按「所在报表」排优先级：
            #     本表合并页（0.95）> 没标出报表的页（0.9/0.6）> 母公司表（0.35）
            #     > 别的报表页（0.30）
            # 真实年报里母公司表的负债合计与合并利润表同页，正是靠这个排序才压得住；
            # 现金流量表科目（折旧摊销等）主要来自补充资料与附注，按全篇扫描。
            strict = account.section in ("balance", "income")
            consolidated = f"consolidated-{account.section}"
            parent = f"parent-{account.section}"
            page_hits: list[Hit] = []
            seen: set[int] = set()
            for line_index, line, following, joined in _row_candidates(page_text):
                if line_index in seen:
                    continue  # 同一行的拼接变体不重复计
                found = _match_row(
                    line,
                    account,
                    following,
                    lines,
                    row_of_line.get(line_index),
                    trusted and not joined,
                )
                if found is None:
                    continue
                label, value, note, raw_text = found
                seen.add(line_index)
                statement = _statement_at(segments, line_index)
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
                    note = (note + "；" if note else "") + "来自母公司报表，不是合并口径"
                elif not is_consolidated:
                    note = (note + "；" if note else "") + "本页没有『合并』二字，可能是母公司报表"
                page_hits.append(
                    Hit(
                        account=name,
                        label=label,
                        value_yuan=value * scale,
                        raw_text=raw_text,
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

        account = schema.ACCOUNT_BY_NAME[name]
        winner = max(hits, key=rank)
        # 拆成多段列示的科目可能落在**不同页**上：腾讯的「借款」非流动部分在
        # 第 133 页、流动部分在 134 页，只取一页会漏掉另一段。所以对 aggregate
        # 科目，把「同类报表页」上的命中加总。
        if account.aggregate:
            # 分组要同时看「哪张报表」和「合并还是母公司」：
            # 母公司资产负债表也是资产负债表，把两者的应收相加就错了。
            def statement_of(page: int) -> tuple:
                return (
                    tuple(sorted(page_kinds.get(page, set()))),
                    page_consolidated.get(page, False),
                )

            same_kind = [
                hit for hit in hits if statement_of(hit.page) == statement_of(winner.page)
            ]
            if len(same_kind) > 1:
                parts = " + ".join(f"{hit.value_yuan:,.0f}" for hit in same_kind)
                winner = replace(
                    winner,
                    value_yuan=sum(hit.value_yuan for hit in same_kind),
                    raw_text=" + ".join(hit.raw_text for hit in same_kind)[:200],
                    note=(winner.note + "；" if winner.note else "")
                    + f"分 {len(same_kind)} 页列示，已相加（{parts}）",
                )
        others = [hit.page for hit in hits if hit.page != winner.page]
        if others:
            winner.note = (winner.note + "；" if winner.note else "") + (
                f"另有 {len(others)} 处（第 " + "、".join(str(page) for page in others) + " 页）"
            )
        best[name] = winner
    return best


def _scan_context(pages: Sequence[str], rule: "schema.ContextRule") -> Hit | None:
    """按「触发词 → 后面几行里找取值标签」的规则取一个科目。"""
    for page_number, page_text in enumerate(pages, start=1):
        if not page_text:
            continue
        # 必须用归一化后的文本：繁体年报（腾讯）写「下列人士應佔」，
        # 不做繁简转换就永远匹配不上规则里的「下列人士应占」，而且是静默失败。
        page_text = normalize_text(page_text)
        if rule.trigger not in page_text:
            continue
        lines = list(_iter_lines(page_text))
        scale, unit_name = detect_scale(page_text)
        for index, line in enumerate(lines):
            if rule.trigger not in line:
                continue
            for follow in lines[index + 1 : index + 1 + rule.lookahead]:
                position = label_position(follow, rule.value_label)
                if position < 0:
                    continue
                tail = follow[position + len(rule.value_label) :]
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
    series: Mapping[str, Mapping[int, Hit]] | None = None,
) -> dict[str, Any]:
    """页文本 → 一条完整的抽取记录（可落盘、可接流水线）。

    series 是「多年列示」的按列取数结果 {科目: {年份: Hit}}，来自 columns.py。
    记录里会多出 `years` 与 `series` 两块；`accounts` 仍然是本期（最新一年）的值，
    所以只用 `accounts` 的旧调用方不受影响。
    """
    text_chars = sum(len(page) for page in pages)
    matches = scan_pages(pages)
    values = _values(matches)
    currency = detect_currency(pages)
    period_end = detect_period_end(pages)
    soup = cid_ratio(pages)
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
        "text_layer_usable": soup < 0.05,
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
    elif not record["text_layer_usable"]:
        record["notes"].append(
            f"⚠️ 这份 PDF 的**文字层不可用**：约 {soup:.0%} 的字符是 (cid:xxxx) 形式的"
            "字符编号 —— 说明字体里缺 ToUnicode 映射表，换任何本地解析库都读不出汉字"
            "（小米 2025 年报就是这样）。两条出路：① 用 OCR（先把页面渲染成图片再识别）；"
            "② 用 Firecrawl 之类的云端解析按本模块的 schema 输出（见 finai_web/firecrawl.py）。"
            "本地抽取结果只有零星科目，不要直接拿去做估值。"
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
        # 分开说：必需科目缺了会影响结果，可选科目缺了通常只是「这份报表没有这一行」
        # （美国报表没有「短期借款」，A 股报表没有「Term debt」），混在一起报
        # 会让人以为整份抽取都不靠谱。
        required = set(schema.REQUIRED_FOR_OPENING) | set(schema.REQUIRED_FOR_PEER)
        # 推导补上的科目不算「没抽到」（例如美国报表没有「归母净利润」这一行，
        # 但可以用净利润算出来）。
        resolved = set((record.get("peer") or {}).get("resolved") or []) | set(
            (record.get("opening_balance") or {}).get("resolved") or []
        )
        missing_required = [
            name for name in missing if name in required and name not in resolved
        ]
        missing_optional = [name for name in missing if name not in required]
        if missing_required:
            record["notes"].append(
                "⚠️ 这些**必需**科目没抽到：" + "、".join(missing_required) + "。"
                "它们会让对应字段缺失或退化成默认值，请核对原文（可能是写法不同，"
                "或落在无法解析的表格里）。"
            )
        if missing_optional:
            record["notes"].append(
                "这些可选科目在本报表里没有出现（多数是口径差异，例如美国报表没有"
                "「短期借款」、A 股报表没有「Term debt」；也可能是没抽到）："
                + "、".join(missing_optional)
                + "。可用 `python -m finai_web series --all` 看实际抽到了哪些科目。"
            )

    if cross_check is not None:
        record["cross_check"] = dict(cross_check)

    # 多年列示：每年各自的取值 + 出处。没抽到列结构时是空列表，不影响其它字段。
    if series:
        record["years"] = sorted(
            {year_value for by_year in series.values() for year_value in by_year},
            reverse=True,
        )
        record["series"] = {
            name_key: {
                str(year_value): hit.to_dict()
                for year_value, hit in sorted(by_year.items(), reverse=True)
            }
            for name_key, by_year in sorted(series.items())
        }
    else:
        record["years"] = []
        record["series"] = {}
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
    """本地抽一份年报 PDF（含多年列示：报表有几年就取几年）。"""
    from . import columns  # 延迟导入：columns 依赖本模块，模块级导入会成环

    path = Path(pdf_path)
    content = read_pdf(path)
    pages, backend = content.pages, content.backend
    matches = scan_pages(pages)
    cross = _cross_check_with_algorithm(path, matches) if with_cross_check else None
    series = columns.extract_by_year(pages, content.words) if content.words else {}
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
        series=series,
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
