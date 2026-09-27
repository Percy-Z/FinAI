"""相对估值：可比公司倍数与隐含价值区间。

倍数公式
    市值        市值 = 每股价 x 摊薄股数
    净债务      净债务 = 有息负债 - 现金
    企业价值    企业价值 = 市值 + 净债务 + 少数股东权益 + 优先股 - 非经营性资产
    PE          市值 / 归母净利润
    PB          市值 / 归母净资产
    PS          市值 / 营业收入
    EV/EBITDA   企业价值 / 息税折旧摊销前利润
    EV/EBIT     企业价值 / 息税前利润
    PEG         PE / (盈利增速的百分数)

隐含价值的两条路径，绝不能混
    权益类倍数（PE、PB、PS）：倍数 x 目标公司的每股指标，直接得到隐含股价
    企业价值类倍数（EV/EBITDA、EV/EBIT）：
        隐含企业价值 = 倍数 x 目标公司指标
        隐含权益价值 = 隐含企业价值 - 净债务 - 少数股东权益 - 优先股 + 非经营性资产
        隐含股价     = 隐含权益价值 / 摊薄股数
    把 EV/EBITDA 直接乘到每股 EBITDA 上，是这一块最常见的错误。

分母为负时倍数没有意义：亏损公司的 PE 不是「便宜」，而是无解。
这类样本会被排除并单独列出，不参与统计。
"""

from __future__ import annotations

import csv
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean as _mean
from typing import Sequence

from .dcf import DCFResult
from .types import (
    require_finite,
    require_optional_finite,
    require_positive,
)

MULTIPLE_NAMES = ("pe", "pb", "ps", "ev_ebitda", "ev_ebit")
EQUITY_MULTIPLES = frozenset({"pe", "pb", "ps"})
EV_MULTIPLES = frozenset({"ev_ebitda", "ev_ebit"})
STAT_NAMES = ("q1", "median", "q3")
MULTIPLE_LABELS = {
    "pe": "PE",
    "pb": "PB",
    "ps": "PS",
    "ev_ebitda": "EV/EBITDA",
    "ev_ebit": "EV/EBIT",
    "peg": "PEG",
}

# 可比公司少于这个数量时给出样本偏小的提示
MIN_PEERS_FOR_STATS = 3


@dataclass(frozen=True)
class CompanyMetrics:
    """一家公司用于相对估值的输入。金额单位为元，股数为股。"""

    code: str
    name: str
    price: float
    diluted_shares: float
    net_profit_attributable: float | None = None  # 归母净利润
    book_equity_attributable: float | None = None  # 归母净资产
    revenue: float | None = None
    ebit: float | None = None
    ebitda: float | None = None
    earnings_growth: float | None = None  # 盈利增速（小数），用于 PEG
    interest_bearing_debt: float = 0.0
    cash: float = 0.0
    minority_interest: float = 0.0
    preferred_stock: float = 0.0
    non_operating_assets: float = 0.0
    as_of: str = ""

    def __post_init__(self) -> None:
        require_positive(self.price, f"{self.code}：price")
        require_positive(self.diluted_shares, f"{self.code}：diluted_shares")
        for name in (
            "net_profit_attributable",
            "book_equity_attributable",
            "revenue",
            "ebit",
            "ebitda",
            "earnings_growth",
        ):
            require_optional_finite(getattr(self, name), f"{self.code}：{name}")
        for name in (
            "interest_bearing_debt",
            "cash",
            "minority_interest",
            "preferred_stock",
            "non_operating_assets",
        ):
            require_finite(getattr(self, name), f"{self.code}：{name}")

    @property
    def market_cap(self) -> float:
        return self.price * self.diluted_shares

    @property
    def net_debt(self) -> float:
        return self.interest_bearing_debt - self.cash

    @property
    def enterprise_value(self) -> float:
        return (
            self.market_cap
            + self.net_debt
            + self.minority_interest
            + self.preferred_stock
            - self.non_operating_assets
        )

    @property
    def eps(self) -> float | None:
        if self.net_profit_attributable is None:
            return None
        return self.net_profit_attributable / self.diluted_shares

    @property
    def book_value_per_share(self) -> float | None:
        if self.book_equity_attributable is None:
            return None
        return self.book_equity_attributable / self.diluted_shares


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    """分母不存在或非正时返回 None：这类倍数没有意义，不能当便宜。"""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    if not (math.isfinite(numerator) and math.isfinite(denominator)):
        return None
    return numerator / denominator


def compute_multiples(company: CompanyMetrics) -> dict[str, float | None]:
    """算出一家公司的全部倍数。无意义的返回 None。"""
    ev = company.enterprise_value
    pe = _ratio(company.market_cap, company.net_profit_attributable)
    return {
        "pe": pe,
        "pb": _ratio(company.market_cap, company.book_equity_attributable),
        "ps": _ratio(company.market_cap, company.revenue),
        "ev_ebitda": _ratio(ev, company.ebitda),
        "ev_ebit": _ratio(ev, company.ebit),
        "peg": _ratio(pe, (company.earnings_growth or 0.0) * 100),
    }


def percentile(values: Sequence[float], p: float) -> float:
    """分位数，采用线性插值，与 Excel 的 QUARTILE.INC 和 numpy 的 linear 一致。

    位置 = (n - 1) x p，落在两个样本之间时按距离加权。
    """
    if not values:
        raise ValueError("空序列没有分位数")
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"p 必须落在 [0, 1]，收到 {p}")

    data = sorted(values)
    if len(data) == 1:
        return data[0]

    position = (len(data) - 1) * p
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return data[lower]
    weight = position - lower
    return data[lower] * (1.0 - weight) + data[upper] * weight


@dataclass
class MultipleStats:
    """某个倍数在可比公司样本上的分布。"""

    name: str
    values: list[float] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    excluded: list[tuple[str, str]] = field(default_factory=list)  # (代码, 原因)

    @property
    def count(self) -> int:
        return len(self.values)

    @property
    def label(self) -> str:
        return MULTIPLE_LABELS.get(self.name, self.name.upper())

    @property
    def median(self) -> float:
        return percentile(self.values, 0.5)

    @property
    def mean(self) -> float:
        return _mean(self.values)

    @property
    def q1(self) -> float:
        return percentile(self.values, 0.25)

    @property
    def q3(self) -> float:
        return percentile(self.values, 0.75)

    @property
    def minimum(self) -> float:
        return min(self.values)

    @property
    def maximum(self) -> float:
        return max(self.values)

    def value_of(self, stat_name: str) -> float:
        mapping = {
            "median": self.median,
            "mean": self.mean,
            "q1": self.q1,
            "q3": self.q3,
            "min": self.minimum,
            "max": self.maximum,
        }
        if stat_name not in mapping:
            raise ValueError(
                f"未知的统计口径 {stat_name!r}，可选：{sorted(mapping)}"
            )
        return mapping[stat_name]


def _interquartile_bounds(values: Sequence[float]) -> tuple[float, float]:
    """四分位距法的异常值边界：Q1 - 1.5xIQR 到 Q3 + 1.5xIQR。"""
    q1 = percentile(values, 0.25)
    q3 = percentile(values, 0.75)
    iqr = q3 - q1
    return q1 - 1.5 * iqr, q3 + 1.5 * iqr


def build_stats(
    companies: Sequence[CompanyMetrics],
    names: Sequence[str] = MULTIPLE_NAMES,
    exclude_outliers: bool = False,
) -> dict[str, MultipleStats]:
    """统计每个倍数在可比公司样本上的分布。

    exclude_outliers 默认关闭。可比公司通常只有三到五家，按四分位距剔除
    会显著改变结论，所以默认只用中位数来抗偏，不做自动剔除。
    """
    stats: dict[str, MultipleStats] = {}

    for name in names:
        entry = MultipleStats(name=name)
        for company in companies:
            value = compute_multiples(company)[name]
            if value is None:
                entry.excluded.append((company.code, "分母非正或数据缺失"))
                continue
            if not math.isfinite(value):
                entry.excluded.append((company.code, "倍数不是有限数字"))
                continue
            if value <= 0:
                entry.excluded.append((company.code, f"倍数非正（{value:.4f}）"))
                continue
            entry.values.append(value)
            entry.codes.append(company.code)

        if exclude_outliers and entry.count >= 4:
            lower, upper = _interquartile_bounds(entry.values)
            kept_values: list[float] = []
            kept_codes: list[str] = []
            for code, value in zip(entry.codes, entry.values):
                if value < lower or value > upper:
                    entry.excluded.append(
                        (code, f"超出四分位距边界 [{lower:.2f}, {upper:.2f}]")
                    )
                else:
                    kept_values.append(value)
                    kept_codes.append(code)
            entry.values = kept_values
            entry.codes = kept_codes

        stats[name] = entry

    return stats


@dataclass
class ImpliedValuation:
    """一种倍数、一个统计口径下的隐含估值。"""

    multiple: str
    stat_name: str
    peer_value: float
    implied_enterprise_value: float | None
    implied_equity_value: float | None
    implied_per_share: float | None
    note: str = ""

    @property
    def applicable(self) -> bool:
        return self.implied_per_share is not None

    @property
    def label(self) -> str:
        return MULTIPLE_LABELS.get(self.multiple, self.multiple.upper())


def implied_from_multiple(
    target: CompanyMetrics,
    multiple: str,
    peer_value: float,
    stat_name: str = "median",
) -> ImpliedValuation:
    """用可比公司倍数推算目标公司的每股价值。"""
    if multiple not in EQUITY_MULTIPLES and multiple not in EV_MULTIPLES:
        raise ValueError(
            f"倍数 {multiple!r} 不能用于推算隐含价值，"
            f"可用的是 {sorted(EQUITY_MULTIPLES | EV_MULTIPLES)}"
        )

    if multiple == "pe":
        metric = target.net_profit_attributable
    elif multiple == "pb":
        metric = target.book_equity_attributable
    elif multiple == "ps":
        metric = target.revenue
    elif multiple == "ev_ebitda":
        metric = target.ebitda
    else:
        metric = target.ebit

    if metric is None:
        return ImpliedValuation(
            multiple=multiple,
            stat_name=stat_name,
            peer_value=peer_value,
            implied_enterprise_value=None,
            implied_equity_value=None,
            implied_per_share=None,
            note="目标公司该指标缺失，无法推算",
        )
    if metric <= 0:
        return ImpliedValuation(
            multiple=multiple,
            stat_name=stat_name,
            peer_value=peer_value,
            implied_enterprise_value=None,
            implied_equity_value=None,
            implied_per_share=None,
            note=f"目标公司该指标为 {metric:,.0f}（非正），倍数法不适用",
        )

    if multiple in EQUITY_MULTIPLES:
        implied_equity = peer_value * metric
        return ImpliedValuation(
            multiple=multiple,
            stat_name=stat_name,
            peer_value=peer_value,
            implied_enterprise_value=None,
            implied_equity_value=implied_equity,
            implied_per_share=implied_equity / target.diluted_shares,
            note="权益类倍数：倍数 x 每股指标直接得到隐含股价",
        )

    # 企业价值类倍数：必须先走估值桥，再除以股数
    implied_ev = peer_value * metric
    implied_equity = (
        implied_ev
        - target.interest_bearing_debt
        + target.cash
        - target.minority_interest
        - target.preferred_stock
        + target.non_operating_assets
    )
    note = (
        f"企业价值类倍数：隐含企业价值 {implied_ev:,.0f}，"
        f"减净债务 {target.net_debt:,.0f} 后得到权益价值"
    )
    if implied_equity < 0:
        note += "。注意：隐含权益价值为负，这个倍数对应的企业价值还不够还债，结果不可用"
    return ImpliedValuation(
        multiple=multiple,
        stat_name=stat_name,
        peer_value=peer_value,
        implied_enterprise_value=implied_ev,
        implied_equity_value=implied_equity,
        implied_per_share=implied_equity / target.diluted_shares,
        note=note,
    )


@dataclass
class CompsResult:
    """相对估值的完整结果。"""

    target: CompanyMetrics
    peers: list[CompanyMetrics]
    peer_multiples: dict[str, dict[str, float | None]] = field(default_factory=dict)
    stats: dict[str, MultipleStats] = field(default_factory=dict)
    implied: list[ImpliedValuation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def price(self) -> float:
        return self.target.price

    @property
    def applicable(self) -> list[ImpliedValuation]:
        return [item for item in self.implied if item.applicable]

    @property
    def medians(self) -> list[ImpliedValuation]:
        return [
            item
            for item in self.applicable
            if item.stat_name == "median"
        ]

    @property
    def value_range(self) -> tuple[float, float]:
        values = [
            item.implied_per_share
            for item in self.medians
            if item.implied_per_share is not None
        ]
        if not values:
            raise ValueError("没有可用的隐含估值，无法给出区间")
        return min(values), max(values)

    @property
    def central_value(self) -> float:
        """各方法中位数隐含价值的中位数，作为相对估值的中心值。"""
        values = [
            item.implied_per_share
            for item in self.medians
            if item.implied_per_share is not None
        ]
        if not values:
            raise ValueError("没有可用的隐含估值，无法给出中心值")
        return percentile(values, 0.5)

    @property
    def implied_return(self) -> float:
        return self.central_value / self.price - 1.0

    def to_dict(self) -> dict:
        return {
            "target": self.target.code,
            "price": self.price,
            "peer_codes": [peer.code for peer in self.peers],
            "multiples": {
                name: {
                    "count": stat.count,
                    "q1": stat.q1 if stat.count else None,
                    "median": stat.median if stat.count else None,
                    "q3": stat.q3 if stat.count else None,
                    "excluded": stat.excluded,
                }
                for name, stat in self.stats.items()
            },
            "implied": [
                {
                    "multiple": item.multiple,
                    "stat": item.stat_name,
                    "peer_value": item.peer_value,
                    "implied_per_share": item.implied_per_share,
                }
                for item in self.implied
            ],
            "warnings": self.warnings,
        }


def comps_valuation(
    target: CompanyMetrics,
    peers: Sequence[CompanyMetrics],
    multiples: Sequence[str] = ("pe", "pb", "ev_ebitda"),
    stat_names: Sequence[str] = STAT_NAMES,
    exclude_outliers: bool = False,
) -> CompsResult:
    """主入口：用可比公司倍数给目标公司估值。"""
    if not peers:
        raise ValueError("可比公司列表不能为空")

    peer_list = list(peers)
    stats = build_stats(peer_list, names=tuple(multiples), exclude_outliers=exclude_outliers)
    peer_multiples = {peer.code: compute_multiples(peer) for peer in peer_list}

    implied: list[ImpliedValuation] = []
    for name in multiples:
        entry = stats[name]
        if entry.count == 0:
            continue
        for stat_name in stat_names:
            implied.append(
                implied_from_multiple(
                    target, name, entry.value_of(stat_name), stat_name=stat_name
                )
            )

    warnings: list[str] = []
    if len(peer_list) < MIN_PEERS_FOR_STATS:
        warnings.append(
            f"可比公司只有 {len(peer_list)} 家，样本偏小，中位数的代表性有限"
        )
    for name in multiples:
        entry = stats[name]
        usable = entry.count
        if usable == 0:
            warnings.append(
                f"{entry.label} 没有任何可比公司可用，该方法被跳过"
                + (f"（{len(entry.excluded)} 家被排除）" if entry.excluded else "")
            )
        elif usable < len(peer_list):
            warnings.append(
                f"{entry.label} 有 {len(peer_list) - usable} 家可比公司因"
                "分母非正或数据缺失被排除"
            )
    if not any(item.applicable for item in implied):
        warnings.append("没有任何倍数适用于目标公司，相对估值无法进行")

    return CompsResult(
        target=target,
        peers=peer_list,
        peer_multiples=peer_multiples,
        stats=stats,
        implied=implied,
        warnings=warnings,
    )


@dataclass
class FootballFieldBand:
    """足球场图里的一条横条。"""

    name: str
    low: float
    central: float
    high: float

    @property
    def width(self) -> float:
        return self.high - self.low

    def to_dict(self) -> dict:
        data = asdict(self)
        data["width"] = self.width
        return data


@dataclass
class FootballField:
    """把各方法的估值区间画在一起。"""

    bands: list[FootballFieldBand]
    price: float
    warnings: list[str] = field(default_factory=list)

    @property
    def overall_low(self) -> float:
        return min(band.low for band in self.bands)

    @property
    def overall_high(self) -> float:
        return max(band.high for band in self.bands)

    def band(self, name: str) -> FootballFieldBand:
        for band in self.bands:
            if band.name == name:
                return band
        raise KeyError(f"没有名为 {name} 的横条")

    def overlaps(self, first: str, second: str) -> bool:
        a, b = self.band(first), self.band(second)
        return max(a.low, b.low) <= min(a.high, b.high)

    def to_dict(self) -> dict:
        """供 football_field.json 使用。"""
        return {
            "price": self.price,
            "overall_low": self.overall_low,
            "overall_high": self.overall_high,
            "bands": [band.to_dict() for band in self.bands],
            "warnings": list(self.warnings),
        }


def comps_bands(result: CompsResult) -> list[FootballFieldBand]:
    """每个倍数一条横条：区间来自可比公司倍数的四分位距。"""
    bands: list[FootballFieldBand] = []
    for name in result.stats:
        entry = result.stats[name]
        if entry.count == 0:
            continue

        def per_share(stat_name: str) -> float | None:
            for item in result.implied:
                if item.multiple == name and item.stat_name == stat_name:
                    return item.implied_per_share
            return None

        low = per_share("q1")
        central = per_share("median")
        high = per_share("q3")
        if low is None or central is None or high is None:
            continue
        bands.append(
            FootballFieldBand(
                name=entry.label, low=min(low, high), central=central, high=max(low, high)
            )
        )
    return bands


def dcf_band(
    result: DCFResult,
    low: float | None = None,
    high: float | None = None,
    name: str = "DCF",
) -> FootballFieldBand:
    """从 DCF 结果构造一条横条。

    区间应来自敏感性分析（比如 WACC 与永续增长率的组合范围），
    没有提供时退化为一个点。
    """
    central = result.per_share
    low = central if low is None else low
    high = central if high is None else high
    return FootballFieldBand(
        name=name, low=min(low, high), central=central, high=max(low, high)
    )


def football_field(
    bands: Sequence[FootballFieldBand],
    price: float,
    check_pairs: Sequence[tuple[str, str]] = (),
) -> FootballField:
    """汇总各方法的区间，并检查它们的重叠情况。

    两条独立方法给出的区间不重叠，说明至少有一边的假设有问题，
    这是比「估值偏高还是偏低」更要紧的发现。
    """
    if not bands:
        raise ValueError("至少需要一条估值横条")

    field = FootballField(bands=list(bands), price=price)
    for first, second in check_pairs:
        # 某一侧不存在时不能直接报错：可比公司全部亏损时 PE 横条就不会生成，
        # 这时应该跳过重叠检查并说明原因，而不是让整条流水线崩掉
        available = {band.name for band in field.bands}
        missing = [name for name in (first, second) if name not in available]
        if missing:
            field.warnings.append(
                f"跳过 {first} 与 {second} 的重叠检查："
                f"没有生成 {'、'.join(missing)} 这条区间"
            )
            continue
        if not field.overlaps(first, second):
            a, b = field.band(first), field.band(second)
            field.warnings.append(
                f"{first} 区间 [{a.low:.2f}, {a.high:.2f}] 与 "
                f"{second} 区间 [{b.low:.2f}, {b.high:.2f}] 不重叠，"
                "两种方法给出的结论矛盾，需要检查假设"
            )
    if price < field.overall_low:
        field.warnings.append(
            f"当前股价 {price:.2f} 低于全部方法的估值下界 {field.overall_low:.2f}"
        )
    elif price > field.overall_high:
        field.warnings.append(
            f"当前股价 {price:.2f} 高于全部方法的估值上界 {field.overall_high:.2f}"
        )
    return field


def write_comps_csv(path: str | Path, result: CompsResult) -> Path:
    """写出 comps.csv：每家公司一行，含目标公司。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    header = [
        "类型",
        "代码",
        "名称",
        "股价",
        "摊薄股数",
        "市值",
        "净债务",
        "企业价值",
        "PE",
        "PB",
        "PS",
        "EV/EBITDA",
        "EV/EBIT",
    ]

    def row(kind: str, company: CompanyMetrics) -> list[str]:
        values = compute_multiples(company)
        return [
            kind,
            company.code,
            company.name,
            _fmt(company.price),
            _fmt(company.diluted_shares),
            _fmt(company.market_cap),
            _fmt(company.net_debt),
            _fmt(company.enterprise_value),
            _fmt(values["pe"]),
            _fmt(values["pb"]),
            _fmt(values["ps"]),
            _fmt(values["ev_ebitda"]),
            _fmt(values["ev_ebit"]),
        ]

    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerow(row("目标", result.target))
        for peer in result.peers:
            writer.writerow(row("可比", peer))

        writer.writerow([])
        writer.writerow(["统计口径", "样本数", "下四分位", "中位数", "上四分位", "均值"])
        for name in result.stats:
            entry = result.stats[name]
            if entry.count == 0:
                writer.writerow([entry.label, 0, "", "", "", ""])
                continue
            writer.writerow(
                [
                    entry.label,
                    entry.count,
                    _fmt(entry.q1),
                    _fmt(entry.median),
                    _fmt(entry.q3),
                    _fmt(entry.mean),
                ]
            )
    return target


def write_implied_csv(path: str | Path, result: CompsResult) -> Path:
    """写出 comps_implied.csv：每种倍数与口径的隐含每股价值。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["倍数", "统计口径", "可比公司倍数", "隐含每股价值", "相对现价", "说明"]
        )
        for item in result.implied:
            if item.applicable:
                relative = item.implied_per_share / result.price - 1.0  # type: ignore[operator]
                relative_text = f"{relative:+.2%}"
                per_share_text = _fmt(item.implied_per_share)
            else:
                relative_text = ""
                per_share_text = ""
            writer.writerow(
                [
                    item.label,
                    item.stat_name,
                    _fmt(item.peer_value),
                    per_share_text,
                    relative_text,
                    item.note,
                ]
            )
    return target


def _fmt(value: float | None) -> str:
    if value is None:
        return ""
    # 只在看起来像比率（非零且绝对值小于 1）时保留 4 位小数，
    # 否则像「净债务为 0」这样的金额会被印成 0.0000
    if value != 0 and abs(value) < 1.0:
        return f"{value:.4f}"
    return f"{value:.2f}"
