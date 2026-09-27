"""财务勾稽校验。

这一层回答一个问题：抽出来的数字彼此自洽吗。
竞赛通知里「财务勾稽关系」是明写的考察点，也是判断一份模型能不能被信任的分水岭。

四种结论，含义不同，不能混用：
    pass     该平的平了，或者差异在容差内
    warning  有差异，但存在合理解释空间；或者是信号类检查被触发
    fail     该平的没平，属于数据错误，必须查
    skipped  输入缺失，检查无法进行（不是通过）

前置检查（排在最前面，不通过后面的结论都不可信）
    核心数字不能全为零或缺失（抽取失败时最常见的就是全零）
    资产总额必须为正数
    参与比较的数字必须是有限值（NaN 参与任何比较都是 False，普通的范围校验拦不住）

核心恒等式
    资产 = 负债 + 所有者权益
    所有者权益 = 归母权益 + 少数股东权益
    期末未分配利润 = 期初 + 归母净利润 - 提取盈余公积 - 宣告股利
    期末现金 = 期初现金 + 经营 + 投资 + 筹资 + 汇率影响

信号类检查（不是恒等式，触发只说明值得追查）
    应收账款增速显著快于营业收入增速
    净利润为正但经营现金流长期跟不上
    非经常性损益占归母净利润比重过高
    收入与总资产的量级关系异常（单位混用的典型症状）
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from .statements import ReportSnapshot

# 信号类检查的默认阈值
AR_DIVERGENCE_THRESHOLD = 0.20  # 应收增速超出收入增速 20 个百分点
NON_RECURRING_THRESHOLD = 0.30  # 非经常性损益占归母净利润 30%
CASH_CONVERSION_THRESHOLD = 0.50  # 经营现金流 / 净利润 低于 0.5
CASH_DEFINITION_TOLERANCE = 0.10  # 现金口径差异超过货币资金 10% 判 fail
SCALE_MIN_RATIO = 0.01  # 收入 / 总资产的合理下界
SCALE_MAX_RATIO = 100.0  # 合理上界


@dataclass(frozen=True)
class Tolerance:
    """容差。取绝对容差与相对容差中的较大者。"""

    absolute: float = 1.0
    relative: float = 1e-6

    def __post_init__(self) -> None:
        for name in ("absolute", "relative"):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(f"Tolerance.{name} 必须是有限数字，收到 {value!r}")
            if value < 0:
                raise ValueError(f"Tolerance.{name} 不能为负，收到 {value}")

    def limit_for(self, scale: float) -> float:
        if not math.isfinite(scale):
            raise ValueError(f"容差的参考量必须是有限数字，收到 {scale!r}")
        return max(self.absolute, self.relative * abs(scale))


@dataclass
class CheckResult:
    """一条检查的结论。字段与 validation_report.json 保持一致。

    两个数值字段的单位是分开的，不要混用：
        difference  金额差异（元），只有恒等式类检查会填
        metric      信号类检查算出的指标值（比率、占比、增速差），
                    对应的 tolerance 是触发阈值而不是容差
    expected 与 actual 表示被比较的两个原始数字，单位随检查而定。
    """

    check_id: str
    category: str
    description: str
    status: str
    expected: float | None = None
    actual: float | None = None
    difference: float | None = None
    metric: float | None = None
    tolerance: float | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ValidationReport:
    """一次校验的全部结论。"""

    fiscal_year: int
    source: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        counts = {"pass": 0, "warning": 0, "fail": 0, "skipped": 0}
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        counts["total"] = len(self.checks)
        return counts

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if check.status == "fail"]

    @property
    def warnings(self) -> list[CheckResult]:
        return [check for check in self.checks if check.status == "warning"]

    def by_id(self, check_id: str) -> CheckResult:
        for check in self.checks:
            if check.check_id == check_id:
                return check
        raise KeyError(f"没有编号为 {check_id} 的检查")

    def to_dict(self) -> dict:
        return {
            "fiscal_year": self.fiscal_year,
            "source": self.source,
            "summary": self.summary,
            "checks": [check.to_dict() for check in self.checks],
        }


def _tie(
    check_id: str,
    category: str,
    description: str,
    expected: float,
    actual: float,
    tolerance: Tolerance,
    note: str = "",
    scale: float | None = None,
) -> CheckResult:
    """恒等式类检查：差的绝对值在容差内算通过。"""
    if not (math.isfinite(expected) and math.isfinite(actual)):
        # 否则容差会被 inf 撑成无穷大，任何差额都能「通过」
        return CheckResult(
            check_id=check_id,
            category=category,
            description=description,
            status="fail",
            expected=None if not math.isfinite(expected) else expected,
            actual=None if not math.isfinite(actual) else actual,
            note=(
                f"参与比较的数字不是有限值（期望 {expected}，实际 {actual}），"
                "多半是抽取时读到了空值或异常值"
            ),
        )
    difference = actual - expected
    limit = tolerance.limit_for(
        scale if scale is not None else max(abs(expected), abs(actual))
    )
    return CheckResult(
        check_id=check_id,
        category=category,
        description=description,
        status="pass" if abs(difference) <= limit else "fail",
        expected=expected,
        actual=actual,
        difference=difference,
        tolerance=limit,
        note=note,
    )


def _skipped(check_id: str, category: str, description: str, missing: str) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        category=category,
        description=description,
        status="skipped",
        note=f"缺少输入：{missing}",
    )


def check_data_presence(snapshot: ReportSnapshot) -> CheckResult:
    """DQ-01 这份数据看起来是不是空的。

    必须放在所有恒等式检查之前：资产全为零时「资产 = 负债 + 权益」恒成立，
    会报出一条假通过，让读者以为数据没问题。抽取失败时最常见的表现就是全零。
    """
    balance = snapshot.balance
    income = snapshot.income
    cashflow = snapshot.cashflow

    core = {
        "资产总计": balance.total_assets,
        "营业收入": income.revenue,
        "净利润": income.net_profit,
        "经营活动现金流": cashflow.operating,
    }
    usable = {
        label: value
        for label, value in core.items()
        if value not in (None, 0) and math.isfinite(value)
    }
    empty = [label for label in core if label not in usable]

    check_id = "DQ-01"
    category = "data_quality"
    description = "核心数字不应全部为零或缺失，资产总计必须为正"

    if not usable:
        return CheckResult(
            check_id=check_id,
            category=category,
            description=description,
            status="fail",
            note=(
                "资产总计、营业收入、净利润、经营活动现金流全部为零或缺失，"
                "这份数据看起来什么都没抽到，下面的恒等式检查结果都不可信"
            ),
        )

    if not (balance.total_assets and balance.total_assets > 0):
        return CheckResult(
            check_id=check_id,
            category=category,
            description=description,
            status="fail",
            expected=1.0,
            actual=balance.total_assets,
            note=(
                f"资产总计为 {balance.total_assets}，真实的公司资产总额必须为正数，"
                "检查是不是抽错了数或者单位不对"
            ),
        )

    note = ""
    if empty:
        note = "以下核心数字为零或缺失：" + "、".join(empty) + "，注意核对"
    return CheckResult(
        check_id=check_id,
        category=category,
        description=description,
        status="pass",
        note=note,
    )


def check_balance_sheet_tie(
    snapshot: ReportSnapshot, tolerance: Tolerance
) -> CheckResult:
    """BS-01 资产 = 负债 + 所有者权益。"""
    balance = snapshot.balance
    expected = balance.total_liabilities + balance.total_equity
    return _tie(
        "BS-01",
        "balance_sheet",
        "资产总计 = 负债合计 + 所有者权益合计",
        expected,
        balance.total_assets,
        tolerance,
    )


def check_equity_composition(
    snapshot: ReportSnapshot, tolerance: Tolerance
) -> CheckResult:
    """BS-02 所有者权益 = 归母权益 + 少数股东权益。"""
    balance = snapshot.balance
    if balance.equity_attributable is None or balance.minority_equity is None:
        return _skipped(
            "BS-02",
            "balance_sheet",
            "所有者权益 = 归母权益 + 少数股东权益",
            "equity_attributable 或 minority_equity",
        )
    expected = balance.equity_attributable + balance.minority_equity
    return _tie(
        "BS-02",
        "balance_sheet",
        "所有者权益 = 归母权益 + 少数股东权益",
        expected,
        balance.total_equity,
        tolerance,
    )


def check_retained_earnings_rollforward(
    snapshot: ReportSnapshot, tolerance: Tolerance
) -> CheckResult:
    """BS-03 期末未分配利润 = 期初 + 归母净利润 - 提取盈余公积 - 宣告股利。

    分红与提取盈余公积若没有提供，就按 0 计算，但结论降级为 warning，
    因为差额完全可能由这两项解释。输入不全时不该给出 fail。
    """
    balance = snapshot.balance
    income = snapshot.income
    begin = snapshot.balance_begin

    if (
        begin is None
        or begin.retained_earnings is None
        or balance.retained_earnings is None
        or income.net_profit_attributable is None
    ):
        return _skipped(
            "BS-03",
            "balance_sheet",
            "期末未分配利润 = 期初 + 归母净利润 - 提取盈余公积 - 宣告股利",
            "期初/期末未分配利润或归母净利润",
        )

    reserve = income.reserve_appropriation
    dividends = income.dividends_declared
    inputs_complete = reserve is not None and dividends is not None

    expected = (
        begin.retained_earnings
        + income.net_profit_attributable
        - (reserve or 0.0)
        - (dividends or 0.0)
    )
    result = _tie(
        "BS-03",
        "balance_sheet",
        "期末未分配利润 = 期初 + 归母净利润 - 提取盈余公积 - 宣告股利",
        expected,
        balance.retained_earnings,
        tolerance,
    )

    if result.status == "fail" and not inputs_complete:
        result.status = "warning"
        result.note = (
            "未提供提取盈余公积或宣告股利数据，差额可能由这两项、"
            "会计政策变更或其他权益变动解释，需核对所有者权益变动表"
        )
    return result


def check_assets_floor(
    snapshot: ReportSnapshot, tolerance: Tolerance
) -> CheckResult:
    """BS-04 明细科目不应超过资产总额。

    这一条专抓抽取错误：货币资金或应收账款比总资产还大，一定是取错数或单位不对。
    """
    balance = snapshot.balance
    offenders: list[str] = []
    for name, value, label in (
        ("cash_and_equivalents", balance.cash_and_equivalents, "货币资金"),
        ("accounts_receivable", balance.accounts_receivable, "应收账款"),
        ("inventory", balance.inventory, "存货"),
    ):
        if value is None:
            continue
        if value > balance.total_assets + tolerance.limit_for(balance.total_assets):
            offenders.append(f"{label} {value:,.0f} > 资产总计 {balance.total_assets:,.0f}")

    if offenders:
        return CheckResult(
            check_id="BS-04",
            category="balance_sheet",
            description="货币资金、应收账款、存货均不应超过资产总计",
            status="fail",
            note="；".join(offenders),
        )
    return CheckResult(
        check_id="BS-04",
        category="balance_sheet",
        description="货币资金、应收账款、存货均不应超过资产总计",
        status="pass",
    )


def check_cash_rollforward(
    snapshot: ReportSnapshot, tolerance: Tolerance
) -> CheckResult:
    """CF-01 期末现金 = 期初现金 + 经营 + 投资 + 筹资 + 汇率影响。"""
    cashflow = snapshot.cashflow
    if cashflow.cash_begin is None or cashflow.cash_end is None:
        return _skipped(
            "CF-01",
            "cash_flow",
            "期末现金 = 期初现金 + 经营 + 投资 + 筹资 + 汇率影响",
            "cash_begin 或 cash_end",
        )
    expected = (
        cashflow.cash_begin
        + cashflow.operating
        + cashflow.investing
        + cashflow.financing
        + cashflow.fx_effect
    )
    return _tie(
        "CF-01",
        "cash_flow",
        "期末现金 = 期初现金 + 经营 + 投资 + 筹资 + 汇率影响",
        expected,
        cashflow.cash_end,
        tolerance,
    )


def check_cash_definition(
    snapshot: ReportSnapshot, tolerance: Tolerance
) -> CheckResult:
    """CF-02 现金流量表期末现金 与 资产负债表货币资金 的口径差异。

    这两个数字本来就不相等：货币资金可能包含受限资金、保证金等不属于
    现金及现金等价物的部分。所以差异小的时候判 warning 并说明原因，
    只有差异大到无法用受限资金解释时才判 fail。
    """
    cashflow = snapshot.cashflow
    balance = snapshot.balance
    if cashflow.cash_end is None or balance.cash_and_equivalents is None:
        return _skipped(
            "CF-02",
            "cash_flow",
            "现金流量表期末现金 与 资产负债表货币资金 的口径差异",
            "cashflow.cash_end 或 balance.cash_and_equivalents",
        )

    difference = cashflow.cash_end - balance.cash_and_equivalents
    limit = tolerance.limit_for(balance.cash_and_equivalents)
    if abs(difference) <= limit:
        return CheckResult(
            check_id="CF-02",
            category="cash_flow",
            description="现金流量表期末现金 与 资产负债表货币资金 的口径差异",
            status="pass",
            expected=balance.cash_and_equivalents,
            actual=cashflow.cash_end,
            difference=difference,
            tolerance=limit,
        )

    ratio = abs(difference) / abs(balance.cash_and_equivalents)
    status = "warning" if ratio <= CASH_DEFINITION_TOLERANCE else "fail"
    note = (
        f"差异占货币资金 {ratio:.1%}。"
        "货币资金包含受限资金、保证金等不属于现金及现金等价物的部分，"
        "差异需在报告中说明；占比过大则说明口径或数据有误"
    )
    return CheckResult(
        check_id="CF-02",
        category="cash_flow",
        description="现金流量表期末现金 与 资产负债表货币资金 的口径差异",
        status=status,
        expected=balance.cash_and_equivalents,
        actual=cashflow.cash_end,
        difference=difference,
        tolerance=limit,
        note=note,
    )


def check_revenue_receivable_divergence(
    snapshot: ReportSnapshot, threshold: float = AR_DIVERGENCE_THRESHOLD
) -> CheckResult:
    """X-01 应收账款增速是否显著快于营业收入增速。

    收入增长但钱收不回来，是收入确认激进或放宽账期的典型信号。
    这不是恒等式，触发只说明值得追查，所以结论是 warning。
    """
    balance = snapshot.balance
    begin = snapshot.balance_begin
    income_prior = snapshot.income_prior

    if (
        begin is None
        or balance.accounts_receivable is None
        or begin.accounts_receivable is None
        or income_prior is None
    ):
        return _skipped(
            "X-01",
            "signal",
            "应收账款增速与营业收入增速的背离",
            "上期应收/收入数据",
        )

    if begin.accounts_receivable <= 0 or income_prior.revenue <= 0:
        return _skipped(
            "X-01",
            "signal",
            "应收账款增速与营业收入增速的背离",
            "上期应收或收入为非正数，增速无定义",
        )

    revenue_growth = snapshot.income.revenue / income_prior.revenue - 1.0
    receivable_growth = (
        balance.accounts_receivable / begin.accounts_receivable - 1.0
    )
    gap = receivable_growth - revenue_growth

    trigger = gap > threshold
    note = (
        f"营业收入增速 {revenue_growth:+.1%}，应收账款增速 {receivable_growth:+.1%}，"
        f"相差 {gap:+.1%}"
    )
    if trigger:
        note += (
            "。可能的原因：放宽信用政策、渠道压货、收入确认偏激进，"
            "也可能是并表范围变化，需要结合现金流与账期一起看"
        )

    return CheckResult(
        check_id="X-01",
        category="signal",
        description="应收账款增速不应显著快于营业收入增速",
        status="warning" if trigger else "pass",
        expected=revenue_growth,
        actual=receivable_growth,
        metric=gap,
        tolerance=threshold,
        note=note,
    )


def check_profit_cash_conversion(
    snapshot: ReportSnapshot, threshold: float = CASH_CONVERSION_THRESHOLD
) -> CheckResult:
    """X-02 经营活动现金流 与 净利润 的转化关系。

    比率用净利润而不是归母净利润，因为经营现金流包含了少数股东那部分。
    """
    net_profit = snapshot.income.net_profit
    operating = snapshot.cashflow.operating

    if net_profit <= 0:
        return CheckResult(
            check_id="X-02",
            category="signal",
            description="经营活动现金流 与 净利润 的转化关系",
            status="pass",
            expected=net_profit,
            actual=operating,
            difference=operating - net_profit,
            note="净利润为非正数，现金转化比率不适用",
        )

    ratio = operating / net_profit
    trigger = ratio < threshold
    note = f"经营现金流 {operating:,.0f}，净利润 {net_profit:,.0f}，比率 {ratio:.2f}"
    if trigger:
        note += (
            "。利润含金量偏低，可能是应收或存货占用了资金，"
            "也可能存在不产生现金的收入"
        )

    return CheckResult(
        check_id="X-02",
        category="signal",
        description="经营活动现金流 与 净利润 的转化关系",
        status="warning" if trigger else "pass",
        expected=net_profit,
        actual=operating,
        metric=ratio,
        tolerance=threshold,
        note=note,
    )


def check_non_recurring_share(
    snapshot: ReportSnapshot, threshold: float = NON_RECURRING_THRESHOLD
) -> CheckResult:
    """X-03 非经常性损益占归母净利润的比重。"""
    income = snapshot.income
    if income.non_recurring_gains is None:
        return _skipped(
            "X-03",
            "signal",
            "非经常性损益占归母净利润的比重",
            "non_recurring_gains",
        )
    if income.net_profit_attributable is None or income.net_profit_attributable <= 0:
        return _skipped(
            "X-03",
            "signal",
            "非经常性损益占归母净利润的比重",
            "归母净利润为非正数，占比无意义",
        )

    share = income.non_recurring_gains / income.net_profit_attributable
    trigger = share > threshold
    note = f"非经常性损益 {income.non_recurring_gains:,.0f}，占比 {share:.1%}"
    if trigger:
        note += "。当期利润有较大比例不来自主营业务，估值时不应把这块外推"

    return CheckResult(
        check_id="X-03",
        category="signal",
        description="非经常性损益占归母净利润的比重不应过高",
        status="warning" if trigger else "pass",
        expected=income.net_profit_attributable,
        actual=income.non_recurring_gains,
        metric=share,
        tolerance=threshold,
        note=note,
    )


def check_scale_sanity(snapshot: ReportSnapshot) -> CheckResult:
    """X-04 营业收入与资产总计的量级关系是否合理。

    这是单位混用的主要探测器。收入比总资产大出两个数量级，
    通常是某张表用了万元而另一张用了元。
    """
    revenue = snapshot.income.revenue
    total_assets = snapshot.balance.total_assets

    if total_assets <= 0:
        return _skipped(
            "X-04", "signal", "营业收入与资产总计的量级关系", "资产总计为非正数"
        )

    ratio = revenue / total_assets
    trigger = ratio < SCALE_MIN_RATIO or ratio > SCALE_MAX_RATIO
    note = f"营业收入 / 资产总计 = {ratio:.4f}"
    if trigger:
        note += (
            f"，超出合理区间 [{SCALE_MIN_RATIO}, {SCALE_MAX_RATIO}]。"
            "检查两张表的单位是否一致（元 / 万元 / 千元）"
        )

    return CheckResult(
        check_id="X-04",
        category="signal",
        description="营业收入与资产总计的量级关系应落在合理区间",
        status="fail" if trigger else "pass",
        expected=revenue,
        actual=total_assets,
        metric=ratio,
        note=note,
    )


def check_restatement(
    account: str,
    value_in_latest_report: float,
    value_in_prior_report: float,
    fiscal_year: int,
    tolerance: Tolerance,
) -> CheckResult:
    """X-05 上年年报的本期数 与 本年报的上期数 是否一致。

    两个数不一致，说明公司做了追溯调整或重分类。这不一定是错误，
    但必须查清并在报告里披露，否则同期对比会失真。
    """
    return _tie(
        "X-05",
        "cross_period",
        f"{fiscal_year} 年 {account}：本年报上期数与上年报本期数应一致",
        value_in_prior_report,
        value_in_latest_report,
        tolerance,
        note="不一致说明存在追溯调整或重分类，需查明原因并在报告中披露",
    )


def run_checks(
    snapshot: ReportSnapshot,
    tolerance: Tolerance | None = None,
    ar_divergence_threshold: float = AR_DIVERGENCE_THRESHOLD,
    cash_conversion_threshold: float = CASH_CONVERSION_THRESHOLD,
    non_recurring_threshold: float = NON_RECURRING_THRESHOLD,
) -> ValidationReport:
    """跑完全部不需要跨期数据的检查。

    X-05 需要两份年报才能做，用 check_restatement 单独调用。
    """
    tol = tolerance or Tolerance()
    report = ValidationReport(
        fiscal_year=snapshot.fiscal_year, source=snapshot.source
    )
    report.checks = [
        check_data_presence(snapshot),
        check_balance_sheet_tie(snapshot, tol),
        check_equity_composition(snapshot, tol),
        check_retained_earnings_rollforward(snapshot, tol),
        check_assets_floor(snapshot, tol),
        check_cash_rollforward(snapshot, tol),
        check_cash_definition(snapshot, tol),
        check_revenue_receivable_divergence(snapshot, ar_divergence_threshold),
        check_profit_cash_conversion(snapshot, cash_conversion_threshold),
        check_non_recurring_share(snapshot, non_recurring_threshold),
        check_scale_sanity(snapshot),
    ]
    return report
