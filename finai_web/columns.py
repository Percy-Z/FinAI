"""按列取数：从多年列示的报表里把每一年的数分别取出来。

为什么单独一层
    单列扫描（`extract.scan_pages`）的规则是「一行里取标签后面的第一个金额」。
    这对只有一列的本期报表是对的，但对**多年列示**的报表（Apple 10-K 列 3 年、
    华为年报列 2 年）就只能拿到最近一期，另外几年白白丢掉 —— 而历史增速、
    多年均值、趋势判断都要靠它们。

做法（每一步都能解释，不靠黑盒）
    1. 用 pdfplumber 的词坐标把页面拆成「行」（同一 top 的词算一行）
    2. 在表头里找年份词（`2025` / `2025年` / `2024年12月31日`），按 x 排序
    3. 把金额词按右边界 x 聚成「列」
    4. 年份与列按 x 位置就近配对（一一对应，不允许多对一）
    5. 对每个科目：找到标签，把标签右侧的金额按 x 归属到各列 → 各年的值

底线
    抽不出列结构（扫描件、单列表、只有文本层没有坐标）时返回空结果，
    调用方回落到单列扫描 —— 宁可少一个能力，也不要给错数。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import schema
from .extract import (
    _DERIVED_TAIL,
    _totals_index_below,
    Hit,
    _page_kinds,
    cjk_digits_to_year,
    detect_scale,
    is_dash_row,
    is_summary_page,
    match_label,
    normalize_text,
)

Word = dict[str, Any]

# 金额形状：可带 $、千分位、小数、括号负数
_AMOUNT = re.compile(r"^\(?\$?-?[\d,]*\d[\d,]*(?:\.\d+)?\)?$")
# 年份词：2025 / 2025年 / 2025年度
_YEAR = re.compile(r"^((?:19|20)\d{2})(?:年|年度|年末)?$")
# 中文数字年份：二零二五年（港交所年报的列标题就是这么写的）
_CJK_YEAR = re.compile(r"^([〇零一二三四五六七八九]{4})(?:年|年度|年末)?$")
# 中文期末日期：2024年12月31日
_CN_DATE = re.compile(r"^((?:19|20)\d{2})年\d{1,2}月\d{1,2}日$")
# 附注编号：1~2 位、无千分位、无括号
_NOTE_REF = re.compile(r"^\d{1,2}$")


def looks_like_amount(text: str) -> bool:
    """这个词像不像一个金额？（必须是数字，且带千分位/小数/括号之一）"""
    token = text.strip().replace("$", "")
    if not _AMOUNT.fullmatch(token):
        return False
    return any(ch in token for ch in ",.()-")


def word_amount(text: str) -> float | None:
    """把一个金额词读成数字。不是金额形状就返回 None。"""
    token = text.strip().replace("$", "")
    if not _AMOUNT.fullmatch(token):
        return None
    negative = token.startswith("(") or token.startswith("-")
    cleaned = token.strip("()-").replace(",", "")
    if not cleaned or cleaned == ".":
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


@dataclass
class Row:
    """页面上的一行：词 + 拼出来的文本 + 每个词在文本里的下标。"""

    words: list[Word]
    text: str
    starts: list[int]

    def words_after(self, position: int) -> list[Word]:
        """取文本下标 position 之后（含跨过 position）的词。"""
        return [w for w, start in zip(self.words, self.starts) if start >= position]


def rows_from_words(words: Sequence[Word], *, tolerance: float = 3.0) -> list[Row]:
    """按 top 把词聚成行。容差 3pt 是为了吸收同一行里字号不同带来的基线偏差。"""
    grouped: list[list[Word]] = []
    for word in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
        if grouped and abs(float(grouped[-1][0]["top"]) - float(word["top"])) <= tolerance:
            grouped[-1].append(word)
        else:
            grouped.append([word])

    rows: list[Row] = []
    for group in grouped:
        parts: list[str] = []
        starts: list[int] = []
        position = 0
        for word in group:
            starts.append(position)
            parts.append(str(word["text"]))
            position += len(str(word["text"])) + 1
        rows.append(Row(words=list(group), text=" ".join(parts), starts=starts))
    return rows


def detect_anchors(words: Sequence[Word], *, tolerance: float = 6.0, minimum: int = 2) -> list[float]:
    """把金额词的右边界聚成列，返回每列的代表 x（升序）。

    只统计「像金额」的词（带千分位/小数/括号）——附注编号那种裸的小整数
    会自成一群，它不该被当成一列。
    `minimum` 表示至少要出现几次才算一列，挡掉正文里偶然出现的数字。
    """
    xs = sorted(float(w["x1"]) for w in words if looks_like_amount(str(w["text"])))
    clusters: list[list[float]] = []
    for x in xs:
        if clusters and abs(x - clusters[-1][-1]) <= tolerance:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    return [sum(cluster) / len(cluster) for cluster in clusters if len(cluster) >= minimum]


def detect_years(words: Sequence[Word]) -> list[tuple[float, int]]:
    """找表头里的年份词，返回 [(x 中心, 年份)]，按 x 升序。

    同一个年份出现多次时取最靠上的那一处（表头总在正文数字之上）。
    """
    found: dict[int, tuple[float, float]] = {}
    for word in words:
        text = str(word["text"]).strip()
        match = _YEAR.fullmatch(text) or _CN_DATE.fullmatch(text)
        if match is not None:
            year = int(match.group(1))
        else:
            cjk_match = _CJK_YEAR.fullmatch(text)
            if cjk_match is None:
                continue
            converted = cjk_digits_to_year(cjk_match.group(1))
            if converted is None:
                continue
            year = converted
        top = float(word["top"])
        center = (float(word["x0"]) + float(word["x1"])) / 2
        if year not in found or top < found[year][0]:
            found[year] = (top, center)
    return sorted((center, year) for year, (_, center) in found.items())


def pair_years(
    years: Sequence[tuple[float, int]],
    anchors: Sequence[float],
    *,
    max_distance: float = 90.0,
) -> dict[int, float]:
    """把年份配到列上：按 x 距离就近配对，且一一对应（不会两个年份抢一列）。"""
    candidates = sorted(
        (abs(center - anchor), year, anchor)
        for center, year in years
        for anchor in anchors
    )
    paired: dict[int, float] = {}
    used: set[float] = set()
    for distance, year, anchor in candidates:
        if distance > max_distance or year in paired or anchor in used:
            continue
        paired[year] = anchor
        used.add(anchor)
    return paired


def _cells(words: Sequence[Word], anchors: Sequence[float], year_of: Mapping[float, int], *, max_distance: float = 60.0) -> dict[int, Word]:
    """把一批词里的金额按 x 归属到各列，返回 {年份: 词}。

    距离超过 max_distance 的直接丢掉 —— 那是「附注」列或表格外的数字。
    """
    cells: dict[int, Word] = {}
    refs = [w for w in words if word_amount(str(w["text"])) is not None]
    # 跳过形如「14」的附注编号：只有当这一行还有别的金额时才跳，
    # 否则「借款 5」这种真实的小金额会被误杀。
    if len(refs) >= 2 and _NOTE_REF.fullmatch(str(refs[0]["text"]).strip()):
        refs = refs[1:]
    for word in refs:
        x = float(word["x1"])
        anchor = min(anchors, key=lambda a: abs(x - a))
        if abs(x - anchor) > max_distance:
            continue
        year = year_of.get(anchor)
        if year is None or year in cells:
            continue
        cells[year] = word
    return cells


def extract_by_year(
    pages_text: Sequence[str],
    pages_words: Sequence[Sequence[Word]] | None,
    accounts: Sequence[str] | None = None,
) -> dict[str, dict[int, Hit]]:
    """逐页按列取数，返回 {科目: {年份: Hit}}。

    与单列扫描一样，同一科目在多个页面出现时按「置信度 → 是不是报表正文页 →
    页面信息密度 → 页码」挑最优的一处；`aggregate` 科目在同一页同一年内相加。
    """
    if not pages_words:
        return {}

    wanted = {
        account.name: account
        for account in schema.ACCOUNTS
        if accounts is None or account.name in accounts
    }
    candidates: dict[str, dict[int, list[Hit]]] = {}
    page_density: dict[int, int] = {}
    page_kinds: dict[int, set[str]] = {}
    page_consolidated: dict[int, bool] = {}

    for index, words in enumerate(pages_words):
        if not words:
            continue
        page_number = index + 1
        page_text = normalize_text(pages_text[index] if index < len(pages_text) else "")
        if is_summary_page(page_text):
            # 摘要页（財務概要/簡明…）口径不同，不参与多年取数
            continue
        page_kinds[page_number] = _page_kinds(page_text)
        scale, unit = detect_scale(page_text)
        consolidated = (
            "合并" in page_text
            or "综合" in page_text
            or "CONSOLIDATED" in page_text.upper()
        )
        page_consolidated[page_number] = consolidated
        confidence = 0.9 if consolidated else 0.6
        if unit == "未标注":
            confidence = min(confidence, 0.5)

        normalized_words = [dict(word, text=normalize_text(str(word["text"]))) for word in words]
        anchors = detect_anchors(normalized_words)
        if len(anchors) < 2:
            # 只有一两行的小表（示例、附注小表）：每一列只出现一次，会被
            # 「至少出现 2 次才算一列」的阈值滤掉。这时放宽到「出现一次也算」，
            # 后面还要与年份词一一配对，仍然挡得住正文里偶然出现的数字。
            relaxed = detect_anchors(normalized_words, minimum=1)
            if len(relaxed) > len(anchors):
                anchors = relaxed
        if not anchors:
            continue
        paired = pair_years(detect_years(normalized_words), anchors)
        if not paired:
            continue
        anchor_of: dict[int, float] = {year: anchor for year, anchor in paired.items()}
        year_of: dict[float, int] = {anchor: year for year, anchor in paired.items()}
        rows = rows_from_words(normalized_words)
        page_density[page_number] = sum(
            1 for row in rows if any(looks_like_amount(str(w["text"])) for w in row.words)
        )

        for name, account in wanted.items():
            # 只认「这张报表的页」。多年列示最容易出错的地方就在这里：
            # 五年摘要页、管理层讨论页、附注页都带年份列，但口径不同 ——
            # Apple 的资产负债表没有 2023 列，若不设限，现金流量表里
            # 「Accounts payable (1,889)」（应付账款的**变动额**）就会被当成
            # 2023 年的应付账款余额记进去。
            if account.section not in page_kinds[page_number]:
                continue
            for row_index, row in enumerate(rows):
                for label in account.labels:
                    position = match_label(row.text, label)
                    if position < 0:
                        continue
                    tail = row.text[position + len(label) :]
                    if any(key in tail[:6] for key in _DERIVED_TAIL):
                        continue
                    target_words = row.words_after(position + len(label))
                    # 注意判据是「这一行有没有数字」，而不是「有没有像金额的数字」：
                    # 腾讯的「股本 33」里 33 是个不带千分位的小整数，
                    # 若按金额形状判断就会误以为这一行没有数，转而去取下面
                    # 隔了好几行的合计（归母权益总额），把股本写成 1,154,152。
                    if not any(
                        any(ch.isdigit() for ch in str(w["text"])) for w in target_words
                    ) and not is_dash_row(tail):
                        # 科目名与合计分行（腾讯的收入）：取下方「整行都是数字」的那一行
                        totals_index = _totals_index_below([r.text for r in rows], row_index)
                        if totals_index is None:
                            continue
                        target_words = [
                            w
                            for w in rows[totals_index].words
                            if looks_like_amount(str(w["text"]))
                        ]
                        if not target_words:
                            continue
                    cells = _cells(
                        target_words, anchors, year_of
                    )
                    if not cells:
                        continue
                    for year, word in cells.items():
                        amount = word_amount(str(word["text"]))
                        if amount is None:
                            continue
                        candidates.setdefault(name, {}).setdefault(year, []).append(
                            Hit(
                                account=name,
                                label=label,
                                value_yuan=amount * scale,
                                raw_text=row.text[:160],
                                page=page_number,
                                unit=unit,
                                scale=scale,
                                confidence=confidence,
                                note="" if consolidated else "本页没有『合并』二字，可能是母公司报表",
                            )
                        )
                    if not account.aggregate:
                        break
            _apply_context_rules(name, account, rows, anchors, year_of, scale, unit, confidence, page_number, candidates)

    # 每个 (科目, 年份) 挑最优的一处；aggregate 科目在同一页同一年内的多处相加
    best: dict[str, dict[int, Hit]] = {}
    for name, by_year in candidates.items():
        section = schema.ACCOUNT_BY_NAME[name].section
        chosen: dict[int, Hit] = {}
        for year, hits in by_year.items():
            def rank(hit: Hit, _section: str = section) -> tuple:
                return (
                    hit.confidence,
                    _section in page_kinds.get(hit.page, set()),
                    page_density.get(hit.page, 0),
                    -hit.page,
                )

            winner = max(hits, key=rank)
            # 只有「拆成多段列示」的科目才把同页同年的多处相加（借款、租赁负债、Term debt）。
            # 别的科目同一页出现两次是「同一个数的另一处写法」，相加会翻倍。
            # 这些科目还可能分在**不同页**（腾讯的借款：非流动在 133 页、流动在 134 页），
            # 所以按「同类报表页」聚合，而不是只看同一页。
            # 同一张报表 = 同样的报表类型 + 同样的合并口径（母公司资产负债表不能并进来）
            def statement_of(page: int) -> tuple:
                return (
                    tuple(sorted(page_kinds.get(page, set()))),
                    page_consolidated.get(page, False),
                )

            winner_kinds = statement_of(winner.page)
            same_page = (
                [hit for hit in hits if statement_of(hit.page) == winner_kinds]
                if schema.ACCOUNT_BY_NAME[name].aggregate
                else [winner]
            )
            if len(same_page) > 1:
                parts = " + ".join(f"{hit.value_yuan:,.0f}" for hit in same_page)
                total = sum(hit.value_yuan for hit in same_page)
                winner = Hit(
                    account=winner.account,
                    label=winner.label,
                    value_yuan=total,
                    raw_text=" + ".join(hit.raw_text for hit in same_page)[:200],
                    page=winner.page,
                    unit=winner.unit,
                    scale=winner.scale,
                    confidence=winner.confidence,
                    note=(winner.note + "；" if winner.note else "")
                    + f"本页同一年出现 {len(same_page)} 次，已相加（{parts}）",
                )
            chosen[year] = winner
        if chosen:
            best[name] = chosen
    return best


def _apply_context_rules(
    name: str,
    account: "schema.Account",
    rows: Sequence[Row],
    anchors: Sequence[float],
    year_of: Mapping[float, int],
    scale: float,
    unit: str,
    confidence: float,
    page_number: int,
    candidates: dict[str, dict[int, list[Hit]]],
) -> None:
    """跨行科目（科目名与数字不在同一行）也要按年取数。"""
    for rule in schema.CONTEXT_RULES:
        if rule.account != name:
            continue
        for index, row in enumerate(rows):
            if rule.trigger not in row.text:
                continue
            for follow in rows[index + 1 : index + 1 + rule.lookahead]:
                position = match_label(follow.text, rule.value_label)
                if position < 0:
                    continue
                cells = _cells(
                    follow.words_after(position + len(rule.value_label)), anchors, year_of
                )
                for year, word in cells.items():
                    amount = word_amount(str(word["text"]))
                    if amount is None:
                        continue
                    candidates.setdefault(name, {}).setdefault(year, []).append(
                        Hit(
                            account=name,
                            label=f"{rule.trigger} / {rule.value_label}",
                            value_yuan=amount * scale,
                            raw_text=follow.text[:160],
                            page=page_number,
                            unit=unit,
                            scale=scale,
                            confidence=rule.confidence,
                            note="由版面规则抽出（跨行科目，按列归属到这一年）",
                        )
                    )
                return
