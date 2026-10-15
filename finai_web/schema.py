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

    aggregate=True 表示这个科目在同一页上可能出现多次、且应当相加：
    报表会把同一个标题拆成「非流动」与「流动」两段列示（华为的「借款」
    就是非流动 219,861 + 流动 19,423），只取一处会漏掉另一处。
    """

    name: str
    labels: tuple[str, ...]
    section: str  # balance / income / cashflow
    note: str = ""
    aggregate: bool = False
    # True 表示这个科目允许从「没有报表标题的页」取数（附注、管理层讨论）。
    # 折旧摊销与利息费用通常只在附注里分项列示，现金流量表正文反而没有；
    # 而收入、资产总计这类科目必须来自报表正文，否则会串到摘要页或母公司报表。
    allow_notes: bool = False


# 这些标签必须出现在行首才算命中。
# 理由：「收入」这种两字标签太通用，「其他收入」「利息收入」「递延收入」都会撞上，
# 只有行首的「收入 880,941」才是利润表的收入行。
# 做成标签级的集合而不是科目级的开关 —— 同一个科目下「营业总收入」是安全的
# （A 股报表里它前面可能还有「一、」这类序号，不能要求行首），只有裸标签要加锚。
# 加进来的是「短标签」，它们在正文里到处都是，只有出现在行首才是报表科目：
#   收入      —— 其他收入 / 利息收入 / 合同收入
#   所得税    —— 递延所得税资产 / 应交所得税
#   负债合计  —— 流动负债合计 / 非流动负债合计（这两个只差两个字，值差 3 倍）
#   股本      —— 总股本 / 股本溢价（华为第 111 页「总出资相当于公司总股本的比例约0.59%」
#               就被当成「股本 = 0.59 元」抽进来过）
# 英文短标签同理：
#   Net income    —— Net income attributable to noncontrolling interests
#   Total assets  —— Total assets held for sale
#   Total liabilities —— Total liabilities and shareholders' equity（这两个差一个「且」，值差 26%）
ANCHORED_LABELS: frozenset[str] = frozenset(
    {"收入", "负债合计", "所得税", "股本", "Net income", "Total assets", "Total liabilities"}
)

# 这些标签必须是**完整科目名**（后面不能再接汉字）。
# 与 ANCHORED_LABELS 的区别：锚定只管行首，这里还管结尾。
#   「应收账款及应收票据」要能被「应收账款」命中（延续是合理的，它是应收账款的细分），
#   但「股本溢价」不能被「股本」命中 —— 那是完全不同的科目（腾讯就这么写）。
EXACT_LABELS: frozenset[str] = frozenset({"股本", "实收资本"})


# ---------------------------------------------------------------------------
# 科目表
#
# 与 algorithm/finai/extract.py 里的 ACCOUNT_LABELS 保持同一套内部命名
# （revenue、cash、total_assets…），这样两套抽取结果可以直接比对，
# 不会出现「同一个科目两个名字」的对账地狱。
# ---------------------------------------------------------------------------
ACCOUNTS: tuple[Account, ...] = (
    # ── 资产负债表：资产侧 ────────────────────────────────────────────
    Account(
        "cash",
        ("货币资金", "现金及现金等价物", "Cash and cash equivalents"),
        "balance",
        "A 股写「货币资金」；华为这类 IFRS 口径写「现金及现金等价物」",
    ),
    Account(
        "accounts_receivable",
        ("应收账款", "Accounts receivable"),
        "balance",
        "含「应收账款及应收票据」；流动与非流动两段都要算",
        aggregate=True,
    ),
    Account(
        "inventory",
        ("存货", "Inventories"),
        "balance",
        "含「存货及其他合同成本」",
        aggregate=True,
    ),
    Account(
        "fixed_assets",
        (
            "固定资产",
            "物业、厂房及设备",
            "物业、设备及器材",          # 港交所口径（腾讯）
            "Property, plant and equipment",
        ),
        "balance",
        "IFRS 口径叫「物业、厂房及设备」",
    ),
    Account(
        "total_assets",
        ("资产总计", "总资产", "资产总额", "Total assets"),  # 港交所写「资产总额」
        "balance",
    ),
    # ── 资产负债表：负债侧 ────────────────────────────────────────────
    Account("accounts_payable", ("应付账款", "Accounts payable"), "balance"),
    Account("short_term_debt", ("短期借款",), "balance"),
    Account(
        "current_portion_of_debt",
        ("一年内到期的非流动负债", "一年内到期的长期借款"),
        "balance",
        "有息负债的一部分",
    ),
    Account("long_term_debt", ("长期借款",), "balance"),
    Account("bonds_payable", ("应付债券",), "balance"),
    Account(
        "borrowings",
        ("借款",),
        "balance",
        "IFRS 口径的「借款」（不分长短，报表分两段列示，须相加）",
        aggregate=True,
    ),
    Account(
        "lease_liabilities",
        ("租赁负债",),
        "balance",
        "租赁负债也算有息负债；同样分两段列示",
        aggregate=True,
    ),
    Account(
        "commercial_paper",
        ("Commercial paper",),
        "balance",
        "美国 10-K 口径：商业票据是最短期的一档有息负债",
        aggregate=True,
    ),
    Account(
        "term_debt",
        ("Term debt",),
        "balance",
        "美国 10-K 口径：Term debt 会分流动与非流动两段列示，须相加",
        aggregate=True,
    ),
    Account(
        "total_liabilities",
        ("负债合计", "负债总额", "Total liabilities"),
        "balance",
    ),
    # ── 资产负债表：权益侧 ────────────────────────────────────────────
    Account(
        "equity_attributable",
        (
            "归属于母公司所有者权益合计",
            "归属于母公司股东权益合计",
            "归属于母公司所有者权益（或股东权益）合计",
            "归属于上市公司股东的所有者权益",
            "本公司所有者应占权益",
            "本公司权益持有人应占权益",      # 港交所口径（腾讯）
            "本公司权益持有人应占权益总额",
            "Total shareholders' equity",
            "Total stockholders' equity",
            # 有的年报不写「合计」两个字：茅台 2025 年年度报告的合并资产负债表就是
            # 「归属于母公司所有者权益 244,637,811,032.18 233,105,984,399.47」。
            # **必须排在最后**：它是上面几条的前缀，排前面会把「合计」留进尾巴里，
            # 于是那一行反而不匹配（行首锚定之后紧跟数字才算命中）。
            "归属于母公司所有者权益",
        ),
        "balance",
        "归母净资产；可比公司 PB 的分母",
    ),
    Account(
        "minority_equity",
        ("少数股东权益", "非控制性权益", "Noncontrolling interests", "Non-controlling interests"),
        "balance",
    ),
    Account(
        "total_equity",
        (
            "所有者权益合计",
            "股东权益合计",
            "所有者权益（或股东权益）合计",
            "权益合计",
            "权益总额",
            "Total equity",
        ),
        "balance",
    ),
    Account(
        "share_capital",
        ("股本", "实收资本", "实收资本（或股本）", "实收资本(或股本)"),
        "balance",
        "股本 / 实收资本；年报里两种写法都有",
    ),
    # ── 利润表 ───────────────────────────────────────────────────────
    Account(
        "revenue",
        ("营业总收入", "营业收入", "收入", "Total net sales", "Net sales", "Revenue"),
        "income",
        "IFRS 口径直接写「收入」；该标签要求出现在行首，避免命中「其他收入」",
    ),
    Account(
        "operating_cost",
        ("营业成本", "Total cost of sales", "Cost of sales", "Cost of revenue"),
        "income",
    ),
    Account(
        "net_profit_attributable",
        (
            "归属于母公司股东的净利润",
            "归属于母公司所有者的净利润",
            "归属于上市公司股东的净利润",
            "归属于母公司所有者的综合收益总额",
            "Net income attributable to",
        ),
        "income",
        "美国公司多数没有少数股东权益，归母净利润就是 Net income（由 CONTEXT_RULES 兜底）",
    ),
    Account(
        "net_profit",
        ("净利润", "年度盈利", "年度溢利", "Net income", "Net earnings"),
        "income",
    ),
    # 利润总额：A 股写「利润总额」，IFRS 式报表写「税前利润」
    Account(
        "total_profit",
        (
            "利润总额",
            "税前利润",
            "Income before provision for income taxes",
            "Income before income taxes",
            "Pretax income",
            "除税前盈利",                     # 港交所口径（腾讯）
            "税前盈利",
        ),
        "income",
    ),
    # 所得税：「所得税」要行首锚定，否则会命中「递延所得税资产/负债」那两行
    Account(
        "income_tax",
        ("所得税费用", "所得税", "Provision for income taxes", "Income tax expense"),
        "income",
    ),
    Account(
        "interest_expense",
        ("利息费用", "利息支出"),
        "income",
        "EBIT 的加回项（通常只在附注里分项披露）",
        allow_notes=True,
    ),
    Account(
        "finance_expense",
        (
            "财务费用",
            # IFRS 式利润表常把这一行写成「财务收入及财务费用」（净额）。
            # 注意标签匹配规则会拒绝「前面还连着汉字」的匹配（那是另一个科目），
            # 所以这种完整标题必须显式列出来，否则会被当成「不是财务费用」。
            "财务收入及财务费用",
            "财务收入及财务费用净额",
            "财务成本",                       # 港交所口径（腾讯）
        ),
        "income",
        "没有单独披露利息费用时的替代品",
    ),
    Account(
        "operating_income",
        ("营业利润", "经营盈利", "Operating income"),
        "income",
        "利息费用无法单独取到时，用营业利润近似 EBIT（美国 10-K 常见：利息混在 Other income 里）",
    ),
    Account(
        "minority_profit",
        (
            "少数股东损益",
            "非控制性权益",                   # 港交所口径（腾讯）
            "Net income attributable to noncontrolling interests",
        ),
        "income",
        "少数股东损益；用来从合并净利润倒算归母净利润",
    ),
    # ── 现金流量表 / 附注：折旧摊销 ───────────────────────────────────
    Account(
        "depreciation_fixed_assets",
        (
            "固定资产折旧、油气资产折耗、生产性生物资产折旧",
            "固定资产折旧",
            "折旧及摊销",                    # 港交所口径
            # 港交所口径按「性质」列示的附注（腾讯附注：按性质划分的开支）
            "物业、设备及器材、投资物业以及使用权资产折旧",
            "物业、厂房及设备折旧",
            "使用权资产折旧",
            "Depreciation and amortization",
            "Depreciation",
        ),
        "cashflow",
        allow_notes=True,
    ),
    Account(
        "amortization_intangible",
        ("无形资产摊销", "Amortization"),
        "cashflow",
        allow_notes=True,
    ),
    Account(
        "amortization_lt_prepaid",
        ("长期待摊费用摊销",),
        "cashflow",
        allow_notes=True,
    ),
    Account(
        "operating_cash_flow",
        (
            "经营活动产生的现金流量净额",
            "经营活动所得现金流量净额",        # 港交所口径（腾讯）
            "经营活动所得现金净额",
            "Cash generated by operating activities",
            "Net cash provided by operating activities",
        ),
        "cashflow",
    ),
    Account(
        "dividends_paid",
        (
            # A 股现金流量表：这一行把「偿付利息」也算进去了，分母用净利润时略有偏差
            "分配股利、利润或偿付利息支付的现金",
            "分配股利、利润或偿付利息所支付的现金",
            # 美国 10-K：只含股息，是分母（分红比例）最干净的口径
            "Payments for dividends and dividend equivalents",
            "Payments of dividends",
            "Dividends paid",
            # 港交所口径
            "已付股息",
            "已付股利",
            "派付股息",
        ),
        "cashflow",
        "用来算分红比例：这个数 ÷ 归母净利润（现金流量表里的流出通常写成正数或括号负数，取绝对值）",
        allow_notes=True,
    ),
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
#
# 两套口径，**只能选一套**：混着加会把同一笔债算两遍。
#   A 股口径   短期借款 + 一年内到期的非流动负债 + 长期借款 + 应付债券
#   IFRS 口径  借款（不分长短，报表分两段列示）+ 租赁负债
# 华为 2025 年报用的是后一套（且租赁负债也计入负债），所以按「哪种抽到用哪种」。
INTEREST_BEARING_PARTS_ASHARE: tuple[str, ...] = (
    "short_term_debt",
    "current_portion_of_debt",
    "long_term_debt",
    "bonds_payable",
)

INTEREST_BEARING_PARTS_IFRS: tuple[str, ...] = (
    "borrowings",
    "lease_liabilities",
)

# 美国 10-K 口径：商业票据 + Term debt（Term debt 分流动与非流动两段列示）
INTEREST_BEARING_PARTS_US: tuple[str, ...] = (
    "commercial_paper",
    "term_debt",
)

# 三套口径，按「哪套抽到用哪套」的顺序排列（顺序即优先级）。
INTEREST_BEARING_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("美国 10-K 口径（商业票据 + Term debt）", INTEREST_BEARING_PARTS_US),
    ("IFRS 口径（借款 + 租赁负债）", INTEREST_BEARING_PARTS_IFRS),
    (
        "A 股口径（短期借款 + 一年内到期的非流动负债 + 长期借款 + 应付债券）",
        INTEREST_BEARING_PARTS_ASHARE,
    ),
)

# 兼容旧名字（外部若引用了 INTEREST_BEARING_PARTS，行为是 A 股口径）
INTEREST_BEARING_PARTS: tuple[str, ...] = INTEREST_BEARING_PARTS_ASHARE

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
    # 虽然没直接抽到、但已经通过推导补上的科目。
    # 用途：抽取记录里报「缺哪些科目」时要排除它们 —— 否则会出现
    # 「⚠️ 必需科目 net_profit_attributable 没抽到」这种误报（其实已经从净利润算出来了）。
    resolved: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "values": self.values,
            "provenance": self.provenance,
            "notes": self.notes,
            "resolved": sorted(self.resolved),
        }


@dataclass(frozen=True)
class ContextRule:
    """跨行取数规则：有些报表把科目名和数字拆在两行。

    华为的合并利润表就是这样（2025 年报第 70 页）：

        净利润归属于：
        本公司所有者            67,104    62,491
        非控制性权益               932        83

    「归属于母公司股东的净利润」这个名字在表里根本没出现 —— 数字在
    下一行的「本公司所有者」后面。单行匹配永远抽不到这种版式，
    只能按「先看到触发词，再在接下来的几行里找取值标签」来找。

    规则刻意写得窄：只在触发词真的出现时才生效，避免误伤别的报表。
    """

    trigger: str
    account: str
    value_label: str
    lookahead: int = 3
    confidence: float = 0.8


# 目前只有一条，是从真实年报里踩出来的。以后遇到新写法往这里加。
CONTEXT_RULES: tuple[ContextRule, ...] = (
    ContextRule(
        trigger="净利润归属于",
        account="net_profit_attributable",
        value_label="本公司所有者",
    ),
    # 港交所口径（腾讯）：年度盈利 / 下列人士应占：/ 本公司权益持有人 …
    ContextRule(
        trigger="下列人士应占",
        account="net_profit_attributable",
        value_label="本公司权益持有人",
    ),
)


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
    for name in ("cash", "accounts_receivable", "inventory", "fixed_assets", "accounts_payable"):
        v[name] = float(values[name])
        p[name] = "read"

    # 股本：有些主体（例如华为投资控股有限公司）不是股份公司，报表上根本没有
    # 「股本」这一行。这时按 0 处理、把全部归母权益记进留存收益 —— 对
    # 三表预测的用途（share_capital + retained_earnings = 归母权益）没有影响，
    # 只是把「股本 / 留存收益」这条内部切分换了个位置，并且会留提示。
    share_capital = _number(values, "share_capital")
    if share_capital in (None, 0.0):
        v["share_capital"] = 0.0
        p["share_capital"] = "default(0)"
        built.notes.append(
            "没有取到可靠的「股本 / 实收资本」（非股份公司本来就没有这一行；"
            "美国 10-K 则常把面值、股数与金额拆成两行，单行匹配取不到），"
            "按 0 处理，全部归母权益计入留存收益。报告里若需要区分股本，请从附注补数。"
        )
    else:
        v["share_capital"] = share_capital
        p["share_capital"] = "read"

    minority = _number(values, "minority_equity")
    v["minority_equity"] = 0.0 if minority is None else minority
    p["minority_equity"] = "read" if minority is not None else "default(0)"
    if minority is None:
        built.notes.append(
            "年报里没有「少数股东权益」这一行，按 0 处理（没有少数股东的公司本来就没有这一行）"
        )

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

    # 有息负债：按抽到的口径选一套，不混用（混用会把同一笔债算两遍）。
    style, parts = next(
        (
            (label, bucket)
            for label, bucket in INTEREST_BEARING_BUCKETS
            if any(values.get(name) is not None for name in bucket)
        ),
        ("A 股口径（短期借款 + 一年内到期的非流动负债 + 长期借款 + 应付债券）",
         INTEREST_BEARING_PARTS_ASHARE),
    )
    debt, used = _sum_present(values, parts)
    v["interest_bearing_debt"] = debt
    if used:
        p["interest_bearing_debt"] = f"derived({style}: " + " + ".join(used) + ")"
        if len(used) != len(parts):
            skipped = [name for name in parts if name not in used]
            built.notes.append(
                f"有息负债按{style}算，只含 " + "、".join(used)
                + "；年报里没抽到 " + "、".join(skipped)
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

    # 归母净利润：年报有三种写法
    #   1. 直接写「归属于母公司股东的净利润」→ 标签能抽到（A 股、华为）
    #   2. 写「Net income attributable to noncontrolling interests」→ 用净利润减掉它
    #   3. 压根没有少数股东权益（Apple 这类）→ 净利润就是归母净利润
    # 第 2、3 种都要在这里兜住，否则整条可比公司指标会因为没有「归母净利润」而失败。
    resolved = dict(values)
    if resolved.get("net_profit_attributable") is None:
        net_profit_value = resolved.get("net_profit")
        minority_profit = resolved.get("minority_profit")
        if net_profit_value is not None:
            if minority_profit is not None:
                resolved["net_profit_attributable"] = net_profit_value - abs(minority_profit)
                built.resolved.add("net_profit_attributable")
                built.notes.append(
                    "归母净利润 = 净利润 − 少数股东损益（年报没有单独写归母净利润这一行）"
                )
            else:
                resolved["net_profit_attributable"] = net_profit_value
                built.resolved.add("net_profit_attributable")
                built.notes.append(
                    "年报没有披露少数股东损益，归母净利润按合并净利润处理 —— "
                    "若公司确有少数股东，这里会高估归母净利润"
                )

    missing = _missing(resolved, REQUIRED_FOR_PEER)
    if missing:
        raise SchemaError(
            "年报里这些科目没抽到，可比公司指标算不出来："
            + "、".join(missing)
            + "\n    收入、归母净利润、归母净资产是相对估值的分母，缺一不可。"
        )

    v = built.values
    p = built.provenance
    v["revenue"] = float(resolved["revenue"])
    v["net_profit_attributable"] = float(resolved["net_profit_attributable"])
    v["book_equity_attributable"] = float(resolved["equity_attributable"])
    p["revenue"] = "read"
    p["book_equity_attributable"] = "read"
    if values.get("net_profit_attributable") is not None:
        p["net_profit_attributable"] = "read"
    elif values.get("minority_profit") is not None:
        p["net_profit_attributable"] = "derived(净利润 − 少数股东损益)"
    else:
        p["net_profit_attributable"] = "derived(按合并净利润)"

    # 利息加回：优先取利润表上的「财务费用」，取绝对值。
    #
    # 为什么不是优先附注里的「利息费用」：附注的利息费用通常只覆盖**部分**债务
    # （例如只算借款、不含租赁负债），拿它加回会低估 EBIT。利润表上的财务费用
    # 是同一口径的完整数（华为 2025 年报第 70 页：财务收入及财务费用 15,821；
    # 加回后 EBIT = 97,222，正好等于「营业利润 96,937 + 应占联合营公司业绩 285」，
    # 可交叉验证）。
    #
    # 取绝对值的原因：两张常见的列示方式都会踩坑 —— 利润表把财务费用放在括号里
    # （负数），附注里又把利息支出写成正数。用绝对值加回，两种写法都对。
    raw_interest = _number(values, "finance_expense")
    source = "财务费用"
    if raw_interest is not None:
        built.notes.append(
            "利息加回用的是利润表「财务费用」—— 里面含汇兑损益与手续费，"
            "EBIT / EBITDA 会略有偏差"
        )
    else:
        raw_interest = _number(values, "interest_expense")
        source = "利息费用"
        if raw_interest is not None:
            built.notes.append(
                "利润表没有「财务费用」，利息加回改用附注里的「利息费用」—— "
                "附注口径通常只覆盖部分债务，EBIT / EBITDA 可能偏低"
            )
    operating_income = _number(values, "operating_income")
    if raw_interest is None:
        interest = 0.0
        p["interest_expense"] = "default(0)"
        if operating_income is not None:
            built.notes.append(
                "利润表里没有单独的利息费用/财务费用（美国 10-K 常把利息并进 "
                "Other income/(expense)），EBIT 直接取「营业利润」"
            )
        else:
            built.notes.append("没有利息费用也没有财务费用，按 0 处理（可能低估 EBIT）")
    else:
        interest = abs(raw_interest)
        p["interest_expense"] = f"read({source}，加回时取绝对值)"

    total_profit = _number(values, "total_profit")
    income_tax = _number(values, "income_tax")
    net_profit = _number(values, "net_profit")

    if raw_interest is None and operating_income is not None:
        # 没有利息信息可加回时，营业利润本身就是最接近 EBIT 的口径
        ebit = operating_income
        p["ebit"] = "read(营业利润，作为 EBIT 的近似)"
    elif total_profit is not None:
        ebit = total_profit + interest
        p["ebit"] = "derived(利润总额 + 利息费用)"
        if income_tax is not None and net_profit is not None:
            # 所得税的符号两种列示方式相反：A 股把「所得税费用」写成正数，
            # IFRS 式报表把它放在括号里（负数）。取绝对值后两种都能对上。
            implied = net_profit + abs(income_tax)
            if abs(implied - total_profit) > max(1.0, abs(total_profit) * 0.01):
                built.notes.append(
                    f"利润总额 {total_profit:,.0f} 与「净利润 + 所得税」{implied:,.0f} 差得较多，"
                    "可能是抽到了母公司报表或不同年度的数，建议核对原文"
                )
    elif net_profit is not None and income_tax is not None:
        ebit = net_profit + abs(income_tax) + interest
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
