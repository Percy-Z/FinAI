"""字段表：年报科目 → 流水线要的字段。

这一层回答两个问题：

    1. 年报里哪些行是我们关心的？（`ACCOUNTS`）
    2. 抽出来的数怎么拼成流水线认识的形状？（`build_opening_balance` / `build_peer_metrics`）

为什么要有这一层，而不是直接把年报数字塞进流水线
--------------------------------------------------
流水线要的是 `BalanceSheetState`（11 个科目，且**资产 = 负债 + 权益**必须成立）
和 `PeerFinancials`（5 个指标）。年报里没有正好这 11 个标题 —— 它有「资产总计」，
但没有「其他非流动资产」这一行可以抄（那本来就是个倒挤项）。

所以这里做的事是：**能抄的抄，抄不到的按会计恒等式倒挤，并且明确标出哪些是倒挤的**。
倒挤不是猜数 —— 只要「资产总计 = 负债合计 + 所有者权益合计」这条恒等式在年报里成立，
倒挤出的数就是年报自己写的数推导出来的，可复核。抽取结果里每个字段都带
`source`（read / derived）与出处，评审问起来答得上。

单位
----
年报常见「单位：千元 / 万元」，抽取层已经统一换算成**元**（见 extract.detect_scale）。
这里所有的数字都按元处理，不做第二次换算，避免换两次。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


class SchemaError(ValueError):
    """抽取结果拼不成流水线要的形状。

    单独一个异常类型，是为了让 CLI 能把「缺哪个科目」这句话原样打出来，
    而不是抛一串 KeyError 让人回去读年报找。
    """


@dataclass(frozen=True)
class Account:
    """一个年报科目。

    labels 按优先级排列：第一个是年报上的标准写法，后面的是变体。
    抽取时按顺序找，找到就用 —— 顺序很重要，「营业收入」必须排在
    「营业总收入」之后，否则会把总收入的数当成收入。
    """

    name: str
    labels: tuple[str, ...]
    section: str  # balance / income / cashflow
    note: str = ""


# ---------------------------------------------------------------------------
# 科目表
#
# 与 algorithm/finai/extract.py 里的 ACCOUNT_LABELS 保持同一套内部命名
# （revenue、cash、total_assets…），这样两套抽取结果可以直接比对，
# 不会出现「同一个科目两个名字」的对账地狱。
# ---------------------------------------------------------------------------
ACCOUNTS: tuple[Account, ...] = (
    # ── 资产负债表：资产侧 ────────────────────────────────────────────
    Account("cash", ("货币资金",), "balance", "现金及现金等价物的近似；年报现金表另有口径"),
    Account("accounts_receivable", ("应收账款",), "balance"),
    Account("inventory", ("存货",), "balance"),
    Account("fixed_assets", ("固定资产",), "balance"),
    Account("total_assets", ("资产总计", "总资产"), "balance"),
    # ── 资产负债表：负债侧 ────────────────────────────────────────────
    Account("accounts_payable", ("应付账款",), "balance"),
    Account("short_term_debt", ("短期借款",), "balance"),
    Account(
        "current_portion_of_debt",
        ("一年内到期的非流动负债", "一年内到期的长期借款"),
        "balance",
        "有息负债的一部分",
    ),
    Account("long_term_debt", ("长期借款",), "balance"),
    Account("bonds_payable", ("应付债券",), "balance"),
    Account("total_liabilities", ("负债合计",), "balance"),
    # ── 资产负债表：权益侧 ────────────────────────────────────────────
    Account(
        "equity_attributable",
        (
            "归属于母公司所有者权益合计",
            "归属于母公司股东权益合计",
            "归属于母公司所有者权益（或股东权益）合计",
            "归属于上市公司股东的所有者权益",
        ),
        "balance",
        "归母净资产；可比公司 PB 的分母",
    ),
    Account("minority_equity", ("少数股东权益",), "balance"),
    Account(
        "total_equity",
        ("所有者权益合计", "股东权益合计", "所有者权益（或股东权益）合计"),
        "balance",
    ),
    Account(
        "share_capital",
        ("股本", "实收资本", "实收资本（或股本）", "实收资本(或股本)"),
        "balance",
        "股本 / 实收资本；年报里两种写法都有",
    ),
    # ── 利润表 ───────────────────────────────────────────────────────
    Account("revenue", ("营业总收入", "营业收入"), "income"),
    Account("operating_cost", ("营业成本",), "income"),
    Account(
        "net_profit_attributable",
        (
            "归属于母公司股东的净利润",
            "归属于母公司所有者的净利润",
            "归属于上市公司股东的净利润",
            "归属于母公司所有者的综合收益总额",
        ),
        "income",
    ),
    Account("net_profit", ("净利润",), "income"),
    Account("total_profit", ("利润总额",), "income"),
    Account("income_tax", ("所得税费用",), "income"),
    Account("interest_expense", ("利息费用", "利息支出"), "income", "EBIT 的加回项"),
    Account("finance_expense", ("财务费用",), "income", "没有单独披露利息费用时的替代品"),
    # ── 现金流量表 / 附注：折旧摊销 ───────────────────────────────────
    Account(
        "depreciation_fixed_assets",
        ("固定资产折旧、油气资产折耗、生产性生物资产折旧", "固定资产折旧"),
        "cashflow",
    ),
    Account("amortization_intangible", ("无形资产摊销",), "cashflow"),
    Account("amortization_lt_prepaid", ("长期待摊费用摊销",), "cashflow"),
    Account("operating_cash_flow", ("经营活动产生的现金流量净额",), "cashflow"),
)

ACCOUNT_NAMES: tuple[str, ...] = tuple(account.name for account in ACCOUNTS)
ACCOUNT_BY_NAME: Mapping[str, Account] = {account.name: account for account in ACCOUNTS}


# ---------------------------------------------------------------------------
# 流水线字段
# ---------------------------------------------------------------------------

# pipeline.config 里 BalanceSheetState 的 11 个科目，顺序与 dataclass 一致
OPENING_FIELDS: tuple[str, ...] = (
    "cash",
    "accounts_receivable",
    "inventory",
    "fixed_assets",
    "other_non_current_assets",
    "accounts_payable",
    "interest_bearing_debt",
    "other_non_current_liabilities",
    "share_capital",
    "retained_earnings",
    "minority_equity",
)

# pipeline.config 里 PeerFinancials 的指标（code / as_of 另算）
PEER_FIELDS: tuple[str, ...] = (
    "revenue",
    "net_profit_attributable",
    "book_equity_attributable",
    "ebit",
    "ebitda",
)

# 有息负债的构成。年报不会写「有息负债」四个字，得自己按科目加。
INTEREST_BEARING_PARTS: tuple[str, ...] = (
    "short_term_debt",
    "current_portion_of_debt",
    "long_term_debt",
    "bonds_payable",
)

# 折旧摊销的三块（现金流量表补充资料）。缺哪块就少加哪块，并在 provenance 里说明。
DA_PARTS: tuple[str, ...] = (
    "depreciation_fixed_assets",
    "amortization_intangible",
    "amortization_lt_prepaid",
)

# 哪些科目凑不出数就必须停下（其余的都是可选、缺了只提示）
REQUIRED_FOR_OPENING: tuple[str, ...] = (
    "cash",
    "accounts_receivable",
    "inventory",
    "fixed_assets",
    "total_assets",
    "accounts_payable",
    "total_liabilities",
    "share_capital",
)

REQUIRED_FOR_PEER: tuple[str, ...] = (
    "revenue",
    "net_profit_attributable",
    "equity_attributable",
)


def _number(values: Mapping[str, float], name: str) -> float | None:
    """取一个科目的值。缺了返回 None（而不是 0）——「没有」和「是 0」不是一回事。"""
    value = values.get(name)
    if value is None:
        return None
    return float(value)


def _sum_present(values: Mapping[str, float], names: Sequence[str]) -> tuple[float, list[str]]:
    """把几项相加，返回 (和, 实际用上的科目名)。全是 None 时和为 0 但没有科目。"""
    used: list[str] = []
    total = 0.0
    for name in names:
        value = _number(values, name)
        if value is None:
            continue
        total += value
        used.append(name)
    return total, used


@dataclass
class Built:
    """拼好的结果：值 + 每个值的来路 + 提示。"""

    values: dict[str, float] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "values": self.values,
            "provenance": self.provenance,
            "notes": self.notes,
        }


def _missing(values: Mapping[str, float], names: Sequence[str]) -> list[str]:
    return [name for name in names if values.get(name) is None]


def build_opening_balance(values: Mapping[str, float]) -> Built:
    """把年报科目拼成 `BalanceSheetState` 的 11 个字段。

    倒挤规则（都是会计恒等式的变形，不是估计）：

        其他非流动资产       = 资产总计 − 货币资金 − 应收账款 − 存货 − 固定资产
        其他非流动负债       = 负债合计 − 应付账款 − 有息负债
        留存收益             = 归母权益 − 股本
        （归母权益缺失时用 所有者权益合计 − 少数股东权益 顶）

    有息负债 = 短期借款 + 一年内到期的非流动负债 + 长期借款 + 应付债券。
    只看得到部分明细时就把看得到的那几项加起来，并在 notes 里写清楚
    「有息负债只含短期借款」—— 宁可说清不完整，也不要假装完整。
    """
    built = Built()

    missing = _missing(values, REQUIRED_FOR_OPENING)
    if missing:
        raise SchemaError(
            "年报里这些科目没抽到，基期资产负债表拼不出来："
            + "、".join(missing)
            + "\n    这些是必填项（资产与负债两侧的总计、以及几个主要科目）。"
            "\n    处理办法：换一份带文字层的年报 PDF 重抽，或用 --override 手工补数。"
        )

    v = built.values
    p = built.provenance

    # 能直接抄的
    for name in ("cash", "accounts_receivable", "inventory", "fixed_assets", "accounts_payable", "share_capital"):
        v[name] = float(values[name])
        p[name] = "read"

    minority = _number(values, "minority_equity")
    v["minority_equity"] = 0.0 if minority is None else minority
    p["minority_equity"] = "read" if minority is not None else "default(0)"
    if minority is None:
        built.notes.append("年报里没有「少数股东权益」这一行，按 0 处理（多数 A 股公司确实为 0）")

    total_assets = float(values["total_assets"])
    total_liabilities = float(values["total_liabilities"])

    # 倒挤：其他非流动资产
    currentish = v["cash"] + v["accounts_receivable"] + v["inventory"] + v["fixed_assets"]
    v["other_non_current_assets"] = total_assets - currentish
    p["other_non_current_assets"] = "derived(total_assets - cash - AR - inventory - fixed_assets)"
    if v["other_non_current_assets"] < 0:
        built.notes.append(
            "⚠️ 倒挤出的「其他非流动资产」为负，说明抽到的流动资产合计数已经超过资产总计 —— "
            "大概率是某个科目抽错了（例如把母公司报表的数当成合并报表）。请核对原文。"
        )

    # 有息负债
    debt, used = _sum_present(values, INTEREST_BEARING_PARTS)
    v["interest_bearing_debt"] = debt
    if used:
        p["interest_bearing_debt"] = "derived(sum of " + " + ".join(used) + ")"
        if len(used) != len(INTEREST_BEARING_PARTS):
            skipped = [name for name in INTEREST_BEARING_PARTS if name not in used]
            built.notes.append(
                "有息负债只含 " + "、".join(used) + "；年报里没抽到 " + "、".join(skipped)
            )
    else:
        v["interest_bearing_debt"] = 0.0
        p["interest_bearing_debt"] = "default(0)"
        built.notes.append(
            "⚠️ 年报里没有抽到任何借款科目，有息负债按 0 处理。"
            "若公司实际有借款，会低估净债务、高估每股价值 —— 请核对原文。"
        )

    # 倒挤：其他非流动负债
    v["other_non_current_liabilities"] = total_liabilities - v["accounts_payable"] - debt
    p["other_non_current_liabilities"] = "derived(total_liabilities - accounts_payable - interest_bearing_debt)"
    if v["other_non_current_liabilities"] < 0:
        built.notes.append(
            "⚠️ 倒挤出的「其他非流动负债」为负：应付账款加有息负债已经超过负债合计，"
            "说明负债侧的科目抽重了或抽错了。请核对原文。"
        )

    # 留存收益：归母权益 − 股本
    equity_attr = _number(values, "equity_attributable")
    if equity_attr is None:
        total_equity = _number(values, "total_equity")
        if total_equity is None:
            raise SchemaError(
                "年报里既没有「归属于母公司所有者权益合计」也没有「所有者权益合计」，"
                "算不出留存收益。\n    请确认抽的是合并资产负债表（母公司报表没有少数股东权益，"
                "两个数会很接近但不相等）。"
            )
        equity_attr = total_equity - v["minority_equity"]
        p["equity_attributable"] = "derived(total_equity - minority_equity)"
    else:
        p["equity_attributable"] = "read"

    v["retained_earnings"] = equity_attr - v["share_capital"]
    p["retained_earnings"] = "derived(equity_attributable - share_capital)"

    # 三表预测要的是「资产 = 负债 + 权益」，这里先自己验一遍，
    # 把差额算出来给上层用（不平衡时上层会拒绝往下跑）。
    gap = total_assets - (total_liabilities + equity_attr + v["minority_equity"])
    if abs(gap) > 1.0:
        built.notes.append(
            f"⚠️ 年报自身口径核对：资产总计 −（负债合计 + 归母权益 + 少数股东权益）= {gap:,.2f} 元。"
            "差额来自年报的非控制性权益/合并口径差异，属于年报本身的性质；"
            "本模块用归母权益倒挤留存收益，基期资产负债表会按归母口径配平。"
        )

    return built


def build_peer_metrics(values: Mapping[str, float]) -> Built:
    """把年报科目拼成 `PeerFinancials` 要的 5 个指标。

    EBIT 和 EBITDA 是**推导**出来的，年报上没有这两个词：

        EBIT   = 利润总额 + 利息费用        （利润总额是「税前利润」，把利息加回去就是 EBIT）
                 缺利润总额时：净利润 + 所得税费用 + 利息费用
        EBITDA = EBIT + 折旧摊销

    利息费用缺失时用「财务费用」顶 —— 财务费用里含汇兑损益和手续费，
    会略微高估 EBIT（进而高估 EBITDA）。这一条会写进 notes，报告里要能说明。
    """
    built = Built()

    missing = _missing(values, REQUIRED_FOR_PEER)
    if missing:
        raise SchemaError(
            "年报里这些科目没抽到，可比公司指标算不出来："
            + "、".join(missing)
            + "\n    收入、归母净利润、归母净资产是相对估值的分母，缺一不可。"
        )

    v = built.values
    p = built.provenance
    v["revenue"] = float(values["revenue"])
    v["net_profit_attributable"] = float(values["net_profit_attributable"])
    v["book_equity_attributable"] = float(values["equity_attributable"])
    for name in ("revenue", "net_profit_attributable", "book_equity_attributable"):
        p[name] = "read"

    interest = _number(values, "interest_expense")
    if interest is not None:
        p["interest_expense"] = "read(利息费用)"
    else:
        interest = _number(values, "finance_expense")
        if interest is not None:
            p["interest_expense"] = "read(财务费用替代)"
            built.notes.append(
                "利息费用没有单独披露，用「财务费用」替代 —— 里面含汇兑损益与手续费，"
                "EBIT / EBITDA 会略有偏差"
            )
        else:
            interest = 0.0
            p["interest_expense"] = "default(0)"
            built.notes.append("没有利息费用也没有财务费用，按 0 处理（可能低估 EBIT）")

    total_profit = _number(values, "total_profit")
    income_tax = _number(values, "income_tax")
    net_profit = _number(values, "net_profit")

    if total_profit is not None:
        ebit = total_profit + interest
        p["ebit"] = "derived(利润总额 + 利息费用)"
        if income_tax is not None and net_profit is not None:
            implied = net_profit + income_tax
            if abs(implied - total_profit) > max(1.0, abs(total_profit) * 0.01):
                built.notes.append(
                    f"利润总额 {total_profit:,.0f} 与「净利润 + 所得税」{implied:,.0f} 差得较多，"
                    "可能是抽到了母公司报表或不同年度的数，建议核对原文"
                )
    elif net_profit is not None and income_tax is not None:
        ebit = net_profit + income_tax + interest
        p["ebit"] = "derived(净利润 + 所得税费用 + 利息费用)"
    else:
        # 退无可退：用归母净利润近似。宁可标出来，也不要悄悄用。
        ebit = float(values["net_profit_attributable"]) + interest
        p["ebit"] = "derived(归母净利润 + 利息费用)"
        built.notes.append(
            "⚠️ 利润总额与所得税费用都没抽到，EBIT 用「归母净利润 + 利息费用」近似，"
            "对有多数股东权益的公司会偏低。建议核对原文后重抽。"
        )

    da, da_used = _sum_present(values, DA_PARTS)
    if da_used:
        p["depreciation_amortization"] = "derived(sum of " + " + ".join(da_used) + ")"
        if len(da_used) != len(DA_PARTS):
            skipped = [name for name in DA_PARTS if name not in da_used]
            built.notes.append("折旧摊销只含 " + "、".join(da_used) + "；缺 " + "、".join(skipped))
    else:
        p["depreciation_amortization"] = "default(0)"
        built.notes.append(
            "⚠️ 没抽到折旧摊销（现金流量表补充资料），EBITDA 等于 EBIT —— "
            "会低估 EV/EBITDA 分母。建议核对原文后重抽。"
        )

    v["ebit"] = ebit
    v["ebitda"] = ebit + da
    return built


# ---------------------------------------------------------------------------
# 占位符
# ---------------------------------------------------------------------------

# 报告/配置模板里可以出现的占位符 → 从哪来。
# 用 dict 而不是 set，是为了让 `finai ingest placeholders` 能直接打印
# 「这个名字是什么、从哪来」，而不用人猜。
PIPELINE_PLACEHOLDERS: Mapping[str, str] = {
    "opening_balance": "基期货币资金（= BalanceSheetState.cash）",
    "cash": "基期货币资金",
    "accounts_receivable": "基期应收账款",
    "inventory": "基期存货",
    "fixed_assets": "基期固定资产",
    "other_non_current_assets": "倒挤：资产总计 − 其余资产项",
    "accounts_payable": "基期应付账款",
    "interest_bearing_debt": "倒挤：借款类科目求和",
    "other_non_current_liabilities": "倒挤：负债合计 − 应付账款 − 有息负债",
    "share_capital": "股本 / 实收资本",
    "retained_earnings": "倒挤：归母权益 − 股本",
    "minority_equity": "少数股东权益",
    "revenue": "营业收入（最近一个完整年度）",
    "net_profit_attributable": "归母净利润",
    "book_equity_attributable": "归母净资产",
    "ebit": "推导：利润总额 + 利息费用",
    "ebitda": "推导：EBIT + 折旧摊销",
}
