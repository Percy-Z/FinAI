"""年报抽取：从年报 PDF 里取出关键财务科目。

竞赛通知的第一条要求是系统能完成「信息检索」。对估值来说，信息检索的落点
就是从年报里把财务数字取出来 —— 而且**每个数字都要能说清从哪来**。

所以这个模块的产出不是「一个数字」，而是「数字 + 页码 + 原文片段」。
评审问「这个 1741 亿是哪儿来的」，答案是「2025 年年报第 42 页，合并利润表，
原文写的是 营业总收入 174,144,000,000.00」。

两件事必须诚实处理
    1. 扫描件抽不出文字。年报通常是电子版（有文字层），但公告、附件常是扫描件。
       检测到没有文字层时直接说明「这是扫描件，需要先 OCR」，而不是返回空结果
       让人以为文件损坏了。
    2. 同一科目会出现多次。合并报表、母公司报表、附注里都有。
       这里优先取「合并」口径，并把所有候选都记下来，让人能核对。

依赖
    pdfplumber 是可选的。没装时本模块仍可导入，调用会返回带说明的空结果。
    这与 matplotlib 的处理方式一致：能降级，但绝不假装成功。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .forecast import BalanceSheetState

try:  # 可选依赖：没装也能导入本模块
    import pdfplumber  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - 环境相关
    pdfplumber = None  # type: ignore[assignment]

HAS_PDFPLUMBER = pdfplumber is not None

# 单位换算：年报上的「单位：万元」要归一到元
UNIT_SCALES: tuple[tuple[str, float, str], ...] = (
    ("单位：万元", 10_000.0, "万元"),
    ("单位:万元", 10_000.0, "万元"),
    ("单位：千元", 1_000.0, "千元"),
    ("单位:千元", 1_000.0, "千元"),
    ("单位：元", 1.0, "元"),
    ("单位:元", 1.0, "元"),
)

# 科目表：内部字段名 → 年报里可能出现的写法（按优先级）
ACCOUNT_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("revenue", ("营业总收入", "营业收入")),
    (
        "net_profit_attributable",
        (
            "归属于母公司股东的净利润",
            "归属于母公司所有者的净利润",
            "归属于上市公司股东的净利润",
        ),
    ),
    ("net_profit", ("净利润",)),
    ("total_assets", ("资产总计", "总资产")),
    ("total_liabilities", ("负债合计",)),
    (
        "total_equity",
        ("所有者权益合计", "股东权益合计", "所有者权益（或股东权益）合计"),
    ),
    ("cash", ("货币资金",)),
    ("accounts_receivable", ("应收账款",)),
    ("inventory", ("存货",)),
    ("fixed_assets", ("固定资产",)),
    ("accounts_payable", ("应付账款",)),
    ("operating_cash_flow", ("经营活动产生的现金流量净额",)),
)

# 一行里的数字：支持千分位、小数、括号负数、负号
_NUMBER = re.compile(r"\(?-?\d[\d,]*\.?\d*\)?")


@dataclass(frozen=True)
class AccountMatch:
    """一个科目的抽取结果。不确定的地方写在 note 里，不猜。"""

    account: str
    label: str
    value_yuan: float | None
    raw_text: str
    page: int
    unit: str
    confidence: float
    note: str = ""

    def describe(self) -> str:
        value = "—" if self.value_yuan is None else f"{self.value_yuan:,.2f}"
        return (
            f"{self.account:<26} {value:>20} 元"
            f"  （第 {self.page} 页，{self.unit}，"
            f"置信度 {self.confidence:.2f}）"
        )


@dataclass
class ExtractionResult:
    """一次抽取的全部结果。"""

    pdf: str
    pages: int = 0
    has_text_layer: bool = False
    unit_label: str = ""
    matches: dict[str, AccountMatch] = field(default_factory=dict)
    candidates: dict[str, list[AccountMatch]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def found(self) -> list[str]:
        return sorted(self.matches)

    @property
    def missing(self) -> list[str]:
        return [name for name, _ in ACCOUNT_LABELS if name not in self.matches]


def _parse_number(text: str) -> float | None:
    """把一行里的数字读出来。括号表示负数（财报惯例）。"""
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


def _detect_unit(page_text: str) -> tuple[float, str]:
    """在页面文字里找「单位：X」。找不到就按「元」处理并标注。"""
    for marker, scale, name in UNIT_SCALES:
        if marker in page_text:
            return scale, name
    return 1.0, "未标注"


def _iter_lines(page_text: str) -> Iterable[str]:
    for raw in page_text.splitlines():
        line = raw.strip()
        if line:
            yield line


def extract_financials(
    pdf_path: str | Path,
    accounts: Sequence[str] | None = None,
) -> ExtractionResult:
    """从年报 PDF 抽关键科目。accounts 指定只抽哪几个，省略就是全部。"""
    path = Path(pdf_path)
    result = ExtractionResult(pdf=str(path))

    if not path.exists():
        result.notes.append(f"文件不存在：{path}")
        return result
    if not HAS_PDFPLUMBER:
        result.notes.append(
            "没有安装 pdfplumber，无法解析 PDF。"
            "安装：uv pip install pdfplumber（或 pip install pdfplumber）"
        )
        return result

    wanted = {
        name: labels
        for name, labels in ACCOUNT_LABELS
        if accounts is None or name in accounts
    }

    assert pdfplumber is not None  # 上面已经查过 HAS_PDFPLUMBER
    with pdfplumber.open(path) as pdf:
        result.pages = len(pdf.pages)
        text_chars = 0
        for page_number, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text() or ""
            text_chars += len(page_text)
            scale, unit_name = _detect_unit(page_text)
            # 「合并」出现在页面上时，这一页的匹配优先 —— 合并口径才是我们要的
            is_consolidated = "合并" in page_text

            for name, labels in wanted.items():
                for line in _iter_lines(page_text):
                    label = next((l for l in labels if l in line), None)
                    if label is None:
                        continue
                    # 排除「应收账款周转率」这类衍生指标行
                    tail = line.split(label, 1)[1]
                    if any(k in tail[:6] for k in ("率", "周转", "账龄")):
                        continue
                    value = _parse_number(tail)
                    if value is None:
                        continue
                    confidence = 0.9 if is_consolidated else 0.6
                    if unit_name == "未标注":
                        confidence = min(confidence, 0.5)
                    match = AccountMatch(
                        account=name,
                        label=label,
                        value_yuan=value * scale,
                        raw_text=line[:120],
                        page=page_number,
                        unit=unit_name,
                        confidence=confidence,
                        note="" if is_consolidated else "不确定是不是合并口径",
                    )
                    result.candidates.setdefault(name, []).append(match)
                    break  # 一页里同一科目只取第一处，避免附注重复

        result.has_text_layer = text_chars > 200

    # 每个科目挑一个最可信的：先看是不是合并口径，再看页码靠前
    for name, items in result.candidates.items():
        best = max(
            items,
            key=lambda m: (m.confidence, -m.page),
        )
        result.matches[name] = best
        if len(items) > 1:
            others = "、".join(f"第 {m.page} 页" for m in items if m is not best)
            result.matches[name] = AccountMatch(
                account=best.account,
                label=best.label,
                value_yuan=best.value_yuan,
                raw_text=best.raw_text,
                page=best.page,
                unit=best.unit,
                confidence=best.confidence,
                note=(best.note + "；" if best.note else "")
                    + f"另有 {len(items) - 1} 处出现（{others}），已取合并口径优先的一处",
            )

    if not result.has_text_layer:
        result.notes.append(
            f"这份 PDF 只抽到 {len(result.matches)} 个科目，而且整份文件几乎没有文字层 —— "
            "大概率是扫描件。扫描件需要先做 OCR，本模块不猜数字。"
        )
    missing = result.missing
    if result.has_text_layer and missing:
        result.notes.append(
            "这些科目没抽到：" + "、".join(missing) + "（可能是科目名写法不同，"
            "或在无法解析的表格里；不要手填一个数补上，要回去核对原文）"
        )
    return result


def format_extraction(result: ExtractionResult) -> str:
    """把抽取结果排成人能读的文本，供命令行与 MCP 工具返回。"""
    lines = [
        f"年报抽取：{result.pdf}",
        f"页数：{result.pages}    文字层：{'有' if result.has_text_layer else '没有（疑似扫描件）'}",
        f"抽到科目：{len(result.matches)} 个",
        "",
    ]
    for name, _ in ACCOUNT_LABELS:
        match = result.matches.get(name)
        if match is None:
            lines.append(f"{name:<26} {'（没抽到）':>20}")
            continue
        value = "—" if match.value_yuan is None else f"{match.value_yuan:,.2f}"
        lines.append(
            f"{name:<26} {value:>20} 元   第 {match.page} 页  "
            f"{match.unit}  置信度 {match.confidence:.2f}"
        )
        lines.append(f"{'':<26} 原文：{match.raw_text}")
        if match.note:
            lines.append(f"{'':<26} 说明：{match.note}")
    if result.notes:
        lines.append("")
        lines.extend(f"提示：{note}" for note in result.notes)
    return "\n".join(lines)


def compare_with_opening(
    result: ExtractionResult, opening: BalanceSheetState
) -> list[str]:
    """把抽取结果与手填的基期数据对账，返回差异描述。

    这是抽取层现阶段最有用的用法：`[opening_balance]` 是手填的过渡数据，
    拿年报抽出来的数去核它，能立刻发现手填错在哪一格。
    等抽取层成熟了，再让 opening_balance 直接由抽取结果生成。

    容差取 1 元：手填与年报都不该有比这更大的偏差。
    """
    mapping = {
        "cash": "cash",
        "accounts_receivable": "accounts_receivable",
        "inventory": "inventory",
        "fixed_assets": "fixed_assets",
        "accounts_payable": "accounts_payable",
        "total_assets": "total_assets",
        "total_liabilities": "total_liabilities",
        "total_equity": "total_equity",
    }
    differences: list[str] = []
    for account, attribute in mapping.items():
        match = result.matches.get(account)
        if match is None or match.value_yuan is None:
            continue
        declared = getattr(opening, attribute, None)
        if declared is None:
            continue
        delta = declared - match.value_yuan
        if abs(delta) > 1.0:
            differences.append(
                f"{account}：手填 {declared:,.2f}，年报第 {match.page} 页写着 "
                f"{match.value_yuan:,.2f}，差 {delta:+,.2f}"
            )
    return differences


def main(argv: Sequence[str] | None = None) -> int:
    import sys

    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        print("用法：python3 -m algorithm.finai.extract <年报.pdf>")
        return 2
    result = extract_financials(args[0])
    print(format_extraction(result))
    return 0 if result.matches else 1


if __name__ == "__main__":
    raise SystemExit(main())
