"""假设草稿：把「能从数据算出来的」与「只能人判断的」分开摆出来。

为什么生成的是**草稿**而不是直接写 assumptions.toml
    那个文件里装着两类性质完全不同的东西：一类能从年报算出来（基期、营运资本比例、
    债务成本），一类只能由人判断（收入增速、资本开支口径、永续增长率）。直接把算出来
    的写进去，等于把判断悄悄替人做了 —— 而且代价很大：长春高新按实际资本开支
    （占收入 18%）建模，预测期自由现金流为负、DCF 每股 −10.79 元；按维持性口径
    （= 折旧摊销率）才是 76.30 元。同一条数据、两种口径，结论一正一负。

    所以流程是：程序算出草稿 → 界面上逐项标注来源 → 人确认（可改）→ 才落盘。

四类状态（确认屏上按这个分组显示）
    derived  从抽取结果或行情算出来，附算法与出处
    default  保守规则默认，标黄提示复核
    kept     上一版文件里有、这次算不出来 —— 沿用旧值，不静默丢
    missing  取不到，也不该拿默认值蒙混 —— 要求人填

覆盖旧文件时的两条硬规矩（见 render_toml）
    1. 先备份成 assumptions.<时间戳>.bak 再写新的；
    2. 人写的 [evidence] 条目原样保留，自动条目的**追加**在后面，不覆盖同名键。
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import DEFAULT_CONFIG_PATH

DRAFT_NAME = "assumptions.draft.json"
ASSUMPTIONS_NAME = "assumptions.toml"

# 归一的触发阈值：最近一年相对上一年，归母净利润跌幅超过这个比例，
# 或者 EBIT 利润率不到上一年的这个比例，就**建议**把盈利能力归一到上一年。
# 注意是「建议」—— 程序不会自己改口径，只会把理由写在草稿里让人看。
PROFIT_DROP_THRESHOLD = -0.40
EBIT_MARGIN_FLOOR = 1.0 / 3.0

# 外生市场参数（无风险利率、股权风险溢价）。它们不属于公司自身的年报，
# 查一次缓存起来，缓存里记着查询日期 —— 报告的 [evidence] 要能说清是哪天的数。
DEFAULT_MARKET_PARAMS = {
    "risk_free": 0.0168,
    "equity_risk_premium": 0.0514,
}
MARKET_PARAMS_SOURCE = (
    "沿用 2026-10-12 查证值（10 年期国债到期收益率 1.683%；Damodaran 中国总股权风险溢价 5.14%），"
    "⚠️ 基准日变了要重新查一次"
)

DEFAULT_TERMINAL_GROWTH = 0.025
DEFAULT_YEARS = 5
STATUTORY_TAX_RATE = 0.25
# 债务成本：抽不到利息费用时的兜底（标成「规则默认」，并要求按年报附注复核）。
# 取无风险利率 + 1.5% 的信用利差 —— 给「报表上不单独披露利息费用」的公司
# （美国 10-K 常把利息并进 Other income）一个可解释的起点。依据写在 source 里，
# 评审看得出这是规则而不是数据。
DEFAULT_CREDIT_SPREAD = 0.015

DERIVED = "derived"
DEFAULT = "default"
KEPT = "kept"
MISSING = "missing"

TITLE_YEAR = re.compile(r"(19|20)\d{2}\s*年\s*年度报告")
RECORD_NAME = re.compile(r"[A-Za-z0-9]+_\d{4}")


class DraftError(ValueError):
    """草稿生成不起来（缺抽取结果、缺行情表、缺基期数据）。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    """草稿里的一个字段：值 + 状态 + 出处。"""

    key: str
    value: Any
    status: str
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "status": self.status, "source": self.source}

    @classmethod
    def from_dict(cls, key: str, raw: Mapping[str, Any]) -> "Field":
        return cls(
            key=key,
            value=raw.get("value"),
            status=str(raw.get("status") or DERIVED),
            source=str(raw.get("source") or ""),
        )


@dataclass
class Draft:
    """一份假设草稿。字段用 `小节.字段名` 作键，和人看的 TOML 一一对应。"""

    company: dict[str, Any]
    fields: dict[str, Field]
    normalized: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    generated_at: str = ""

    def value(self, key: str, fallback: Any = None) -> Any:
        item = self.fields.get(key)
        return fallback if item is None else item.value

    def set_value(self, key: str, value: Any, *, source: str = "人工确认") -> None:
        """人改过或确认过的值。

        状态一律标成 human —— 不管它原来是自动推导还是留白：确认屏上出现过人手的，
        报告里就该说清那是人定的，而不是让「自动推导」这个标签一直挂在那里。
        """
        self.fields[key] = Field(key=key, value=value, status="human", source=source)

    def by_status(self, status: str) -> dict[str, Field]:
        return {
            key: item for key, item in self.fields.items() if item.status == status
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "generated_at": self.generated_at,
            "company": self.company,
            "normalized": self.normalized,
            "notes": self.notes,
            "fields": {key: item.to_dict() for key, item in self.fields.items()},
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Draft":
        fields = raw.get("fields")
        if not isinstance(fields, Mapping):
            raise DraftError("草稿里没有 fields")
        return cls(
            company=dict(raw.get("company") or {}),
            fields={str(key): Field.from_dict(str(key), value) for key, value in fields.items()},
            normalized=dict(raw.get("normalized") or {}),
            notes=[str(item) for item in (raw.get("notes") or [])],
            generated_at=str(raw.get("generated_at") or ""),
        )

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return target

    @classmethod
    def load(cls, path: str | Path) -> "Draft":
        source = Path(path)
        if not source.is_file():
            raise DraftError(f"草稿不存在：{source}")
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise DraftError(f"{source} 不是合法 JSON：{err}") from err
        return cls.from_dict(raw)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 从抽取记录里取数
# ---------------------------------------------------------------------------


def load_records(company_dir: str | Path) -> list[dict[str, Any]]:
    """读某家公司目录下的抽取记录（`extracted/<代码>_<年份>.json`）。

    直接读 JSON 而不是 import 取数层：这一层只需要「记录长什么样」这个约定，
    不需要取数层的代码。少一条依赖，pipeline 就能在没装取数层时照常跑。
    """
    folder = Path(company_dir) / "extracted"
    if not folder.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.json")):
        if not RECORD_NAME.fullmatch(path.stem):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise DraftError(f"{path} 不是合法 JSON：{err}") from err
        if isinstance(record, dict):
            records.append(record)
    return records


def latest_record(
    records: Sequence[Mapping[str, Any]], *, code: str = ""
) -> Mapping[str, Any]:
    """最新的一份记录。**必须按公司代码筛**。

    踩过的坑（2026-10-15，贵州茅台实测）：一个公司目录里同时躺着目标公司与可比公司的
    记录，`max(year)` 会把可比公司的那份当成目标的 —— 目标公司的收入、毛利率、折旧率
    全变成可比公司的数，而报告上看不出来。当时的结果是「茅台归母净利润同比 −87.4%」，
    因为它读的是泸州老窖的利润表。
    """
    wanted = str(code or "").strip()
    items = [
        item
        for item in records
        if not wanted or str(item.get("code") or "") == wanted
    ]
    if not items:
        raise DraftError(
            f"没有 {wanted or '这家公司'} 的抽取记录 —— 先跑一次取数"
        )
    return max(items, key=lambda item: int(item.get("year") or 0))


def fiscal_year(record: Mapping[str, Any], *, manifest_titles: Mapping[str, str] | None = None) -> int:
    """报表所属年度（不是披露年份）。

    从年报标题里取（「2025年年度报告」→ 2025）最准；标题拿不到时退回
    「披露年份 − 1」—— A 股年报都在次年披露，这条经验rule 成立，但要写进 notes
    让人知道这是推出来的。
    """
    code = str(record.get("code") or "")
    year = int(record.get("year") or 0)
    title = (manifest_titles or {}).get(f"{code}_{year}") or ""
    match = TITLE_YEAR.search(title)
    if match:
        return int(match.group(0)[:4])
    return year - 1


def manifest_titles(company_dir: str | Path) -> dict[str, str]:
    """读下载清单，把 `代码_年份 → 公告标题` 拎出来（用来定报表年度）。"""
    path = Path(company_dir) / "manifest.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    entries = raw.get("entries") if isinstance(raw, Mapping) else None
    if isinstance(entries, Mapping):
        entries = list(entries.values())
    titles: dict[str, str] = {}
    for entry in entries or []:
        if not isinstance(entry, Mapping):
            continue
        code = str(entry.get("code") or "")
        year = entry.get("year")
        title = str(entry.get("title") or "")
        if code and year and title:
            titles[f"{code}_{year}"] = title
    return titles


def manifest_entry(
    company_dir: str | Path, code: str, year: int
) -> dict[str, Any]:
    """下载清单里「这家公司这一年」的那一条（没有就返回空字典）。

    用途：报告里要写清数据来源 —— 从巨潮下的，和队友给的本地 PDF，
    是两件不同的事，不能都写成 cninfo。
    """
    path = Path(company_dir) / "manifest.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    entries = raw.get("entries") if isinstance(raw, Mapping) else None
    if isinstance(entries, Mapping):
        entries = list(entries.values())
    for entry in entries or []:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("code") or "") == str(code) and str(entry.get("year") or "") == str(year):
            return dict(entry)
    return {}


def _account(record: Mapping[str, Any], name: str) -> float | None:
    """取一个科目（元）。抽不到返回 None —— 不返回 0，那会被当成真值参与计算。"""
    accounts = record.get("accounts") or {}
    entry = accounts.get(name)
    if not isinstance(entry, Mapping):
        return None
    value = entry.get("value_yuan")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _page(record: Mapping[str, Any], name: str) -> str:
    accounts = record.get("accounts") or {}
    entry = accounts.get(name)
    if not isinstance(entry, Mapping):
        return ""
    page = entry.get("page")
    return f"（年报第 {page} 页）" if page else ""


def payout_ratio_from_record(
    record: Mapping[str, Any], facts: Mapping[str, Any]
) -> tuple[float | None, str]:
    """分红比例 = 现金流量表的「分配股利、利润或偿付利息支付的现金」÷ 归母净利润。

    这一格以前只能靠人手算（000661 数据集的依据条目就是这么写的：「人工核对年报
    现金流量表第 66 页：分配股利、利润或偿付利息支付的现金 67,241,733,345.45 ÷
    归母净利润 = 81.97%」）。口径差异要写在说明里，别让人以为它等于分红预案：

        A 股     「分配股利、利润或偿付利息支付的现金」—— 把偿付利息也算进去了，
                 有息负债大的公司会偏高
        港交所   「已付股息」
        美股     10-K 的 Payments for dividends and dividend equivalents —— 最干净

    比例超过 1.5 倍净利润时不当它是分红：那种数多半是抽错了行（例如把筹资活动的
    其他流出当成了分红），宁可留空让人核对，也不要给一个看着像样的错数。
    """
    paid = _account(record, "dividends_paid")
    profit = facts.get("net_profit")
    which = f"归母净利润"
    if profit is None:
        # 美国 10-K 通常只写「Net income」，没有「归属于母公司」那一行 ——
        # 那边多数公司没有少数股东权益，两者本来就是同一个数（adapter 也这么兜）。
        profit = _account(record, "net_profit")
        which = "合并净利润（年报没有单独披露归母净利润）"
    if paid is None or not profit or float(profit) <= 0:
        return None, ""
    ratio = abs(float(paid)) / float(profit)
    if ratio > 1.5:
        return None, ""
    return ratio, (
        f"现金流量表「分配股利」{abs(float(paid)):,.0f} 元 ÷ {which} "
        f"{float(profit):,.0f} 元 = {ratio:.2%}{_page(record, 'dividends_paid')}"
        "（这一行含偿付利息，口径会略高于分红预案；年报有预案时以预案为准）"
    )


def year_facts(
    record: Mapping[str, Any], *, fiscal_year: int | None = None
) -> dict[str, Any]:
    """一份年报能算出来的东西。缺的字段是 None，不是 0。

    `fiscal_year` 是**报表所属年度**（2025 年年报披露于 2026 年，报表年度是 2025）。
    记录里的 `year` 字段是披露年份，两者差一年 —— 注释与报告里要写报表年度，
    否则「按 2026 年的利润表算」这种话会让人以为用的是 2026 年的数。
    """
    revenue = _account(record, "revenue")
    operating_cost = _account(record, "operating_cost")
    total_profit = _account(record, "total_profit")
    net_profit = _account(record, "net_profit_attributable")
    interest = _account(record, "interest_expense")
    if interest is None:
        interest = _account(record, "finance_expense")
    da_parts = [
        _account(record, name)
        for name in (
            "depreciation_fixed_assets",
            "amortization_intangible",
            "amortization_lt_prepaid",
        )
    ]
    da = sum(part for part in da_parts if part is not None) if any(
        part is not None for part in da_parts
    ) else None

    # 费用率优先用三个费用科目直接算；科目表里还没有它们时（2026-10 之前抽的记录）
    # 退回倒挤：EBIT = 利润总额 + 利息费用，毛利率 − EBIT 利润率 − 折旧摊销率
    # 剩下的就是期间费用与税金及附加。倒挤的口径比直接加总宽一点，所以在 source 里写明。
    #
    # 两个坑：
    #   ① 财务费用**单独存在**不能算「抽到了费用明细」—— 茅台的财务费用是负数
    #      （利息收入），只认它会把期间费用率算成 −0.47%（2026-10-15 实测）。
    #   ② 为什么加进口径不如倒挤准：茅台这类公司的「税金及附加」占收入 15%（消费税），
    #      如果只把销售/管理/研发费用加总、漏掉税金及附加，EBIT 利润率会被算高十几个点。
    #      倒挤是**按定义**保证 EBIT 利润率与年报一致的做法，所以它是首选。
    opex = None
    if revenue:
        parts = [
            _account(record, name)
            for name in ("selling_expense", "admin_expense", "research_expense")
        ]
        if any(part is not None for part in parts):
            opex = sum(part or 0.0 for part in parts) + (
                _account(record, "finance_expense") or 0.0
            )

    ebit = None
    if total_profit is not None:
        ebit = total_profit + (interest or 0.0)

    return {
        "fiscal_year": (
            fiscal_year
            if fiscal_year is not None
            else int(record.get("year") or 0) - 1
        ),
        "revenue": revenue,
        "operating_cost": operating_cost,
        "total_profit": total_profit,
        "net_profit": net_profit,
        "interest_expense": interest,
        "da": da,
        "opex": opex,
        "ebit": ebit,
        "gross_margin": (1.0 - operating_cost / revenue)
        if revenue and operating_cost is not None
        else None,
        "da_pct": (da / revenue) if revenue and da is not None else None,
        "opex_pct": (opex / revenue) if revenue and opex is not None else None,
        "ebit_margin": (ebit / revenue) if revenue and ebit is not None else None,
    }


def normalization_advice(
    latest: Mapping[str, Any],
    prior: Mapping[str, Any] | None,
    *,
    titles: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """要不要把盈利能力归一到上一年？只给建议，不改口径。

    两条触发条件（任一命中即建议）：归母净利润同比跌幅超过 40%，
    或最近一年 EBIT 利润率不到上一年的三分之一。两条都写在报告里 ——
    它是启发式，评审有权问「凭什么」。
    """
    facts_now = year_facts(
        latest,
        fiscal_year=fiscal_year(latest, manifest_titles=titles) if titles is not None else None,
    )
    if prior is None:
        return {
            "applied": False,
            "year": None,
            "reason": "只取到一份年报，没有上一年可比，无法判断是否需要归一",
        }
    facts_prior = year_facts(
        prior,
        fiscal_year=fiscal_year(prior, manifest_titles=titles) if titles is not None else None,
    )
    profit_now, profit_prior = facts_now["net_profit"], facts_prior["net_profit"]
    margin_now, margin_prior = facts_now["ebit_margin"], facts_prior["ebit_margin"]
    drop = None
    if profit_now is not None and profit_prior and profit_prior > 0:
        drop = profit_now / profit_prior - 1.0
    reasons: list[str] = []
    if drop is not None and drop <= PROFIT_DROP_THRESHOLD:
        reasons.append(f"归母净利润同比 {drop:+.1%}（低于阈值 {PROFIT_DROP_THRESHOLD:.0%}）")
    if (
        margin_now is not None
        and margin_prior is not None
        and margin_prior > 0
        and margin_now < margin_prior * EBIT_MARGIN_FLOOR
    ):
        reasons.append(
            f"EBIT 利润率 {margin_now:.2%} 不到上一年的三分之一（{margin_prior:.2%}）"
        )
    if not reasons:
        return {
            "applied": False,
            "year": None,
            "reason": "最近一年与上一年相比没有出现盈利塌陷，按最近一年的实际盈利能力建模",
        }
    return {
        "applied": True,
        "year": facts_prior["fiscal_year"],
        "reason": "、".join(reasons) + "；建议把盈利能力归一到上一年，基期资产负债表仍用最新一年",
    }


# ---------------------------------------------------------------------------
# 生成草稿
# ---------------------------------------------------------------------------


def load_previous(path: str | Path | None) -> dict[str, Any]:
    """读上一版的 assumptions.toml（不存在返回空字典）。"""
    if path is None:
        return {}
    target = Path(path)
    if not target.is_file():
        return {}
    with target.open("rb") as handle:
        return tomllib.load(handle)


def _section(cfg: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = cfg.get(name) if isinstance(cfg, Mapping) else None
    return dict(value) if isinstance(value, Mapping) else {}


def _field(
    key: str,
    *,
    derived: Any = None,
    derived_source: str = "",
    previous: Any = None,
    keep_source: str = "沿用上一版文件里的值",
    default: Any = None,
    default_source: str = "",
    missing_source: str = "",
) -> Field:
    """按优先级定一个字段：能算就算 → 沿用上一版 → 规则默认 → 真的没有（missing）。

    顺序不是随手定的：「能算的」必须压过「上一版的值」，否则基期数据永远更新不了；
    而「上一版的值」必须压过「规则默认」—— 上一版里的数要么是人填的、要么是人确认过的
    （税率可能用了优惠税率、资本开支可能按实际口径定的），被一条通用规则顶掉就白填了。

    「算不出来」还必须能沿用，否则每次生成都会把上次人填好的数弄丢
    （分红比例就是这样的字段 —— 它写在年报的分红预案里，不在我们抽的科目表里）。
    """
    if derived is not None:
        return Field(key=key, value=derived, status=DERIVED, source=derived_source)
    if previous is not None:
        return Field(key=key, value=previous, status=KEPT, source=keep_source)
    if default is not None:
        return Field(key=key, value=default, status=DEFAULT, source=default_source)
    return Field(
        key=key,
        value=None,
        status=MISSING,
        source=missing_source or "年报里没有这个数，需要人填",
    )


def _ratio(numerator: Any, denominator: Any) -> float | None:
    if numerator is None or not denominator:
        return None
    return float(numerator) / float(denominator)


def history_growth(payload: Mapping[str, Any]) -> tuple[float | None, str]:
    """从年报的多年列示里算历史收入增速，返回 (采用值, 说明)。

    原先只有「上一年的年报也下了」才有同比可算，否则只能说「只有一份年报」。
    取数层的多年列示把这个限制去掉了：同一份年报里就带着连续几年的数
    （`payload["history"]` 是 `adapter.history_from` 写进去的）。

    只做算术，不做判断：历史增速能不能当预测增速用仍要人看（行业、管理层指引），
    所以这里沿用与「两份年报」那条路径完全相同的保守规则（不超过 3%、不为负），
    并在说明里把两个年度的原始数字写出来，评审能直接对。
    """
    history = payload.get("history") or {}
    revenue = history.get("revenue") or {}
    series: list[tuple[int, float]] = []
    for year, value in revenue.items():
        try:
            series.append((int(year), float(value)))
        except (TypeError, ValueError):
            continue
    series.sort()
    if len(series) < 2:
        return None, ""
    (prior_year, prior_value), (now_year, now_value) = series[-2], series[-1]
    if not prior_value:
        return None, ""
    raw = now_value / prior_value - 1.0
    applied = min(max(raw, 0.0), 0.03)
    return applied, (
        f"年报多年列示：{prior_year} 年收入 {prior_value:,.0f} → {now_year} 年 "
        f"{now_value:,.0f}，同比 {raw:+.1%}；规则取「不超过 3%、不为负」"
        f"→ 预测期按 {applied:.1%} 平推（保守）"
    )


def build_draft(
    company_dir: str | Path,
    *,
    profile: Any = None,
    payload: Mapping[str, Any] | None = None,
    beta: float | None = None,
    market_params: Mapping[str, float] | None = None,
    previous_path: str | Path | None = None,
) -> Draft:
    """从一家公司的取数结果生成假设草稿。

    需要的东西全在 `company_dir` 里：`company.json`、`extracted/*.json`、
    `extracted/pipeline_input.json`、`manifest.json`。
    `beta` 要联网算（回归），由调用方传进来 —— 这一层保持纯粹，测试才好写。
    """
    base = Path(company_dir)
    # 先定「这是哪家公司」，再挑记录 —— 顺序反了就会把可比公司的财报当成目标的
    # （见 latest_record 的注释：贵州茅台那次就是这么错的）。
    if profile is None:
        company_json = base / "company.json"
        if not company_json.is_file():
            raise DraftError(f"没有公司清单：{company_json}")
        raw_profile = json.loads(company_json.read_text(encoding="utf-8"))
        target = dict(raw_profile.get("target") or {})
    else:
        target = dict(profile.target.to_dict())
    target_code = str(target.get("code") or "")

    records = load_records(base)
    latest = latest_record(records, code=target_code)
    titles = manifest_titles(base)

    prior_year = int(latest.get("year") or 0) - 1
    prior = next(
        (
            item
            for item in records
            if int(item.get("year") or 0) == prior_year
            and str(item.get("code") or "") == target_code
        ),
        None,
    )

    if payload is None:
        payload_path = base / "extracted" / "pipeline_input.json"
        if not payload_path.is_file():
            raise DraftError(
                f"没有流水线输入：{payload_path} —— 先跑完取数（下载 + 抽取 + 汇总）"
            )
        payload = json.loads(payload_path.read_text(encoding="utf-8"))

    opening = payload.get("opening_balance") or {}
    if not opening:
        raise DraftError("流水线输入里没有 opening_balance —— 基期资产负债表抽不出来")

    # 默认读本目录里的 assumptions.toml —— 那是这家公司上一次确认过的假设。
    # 让调用方少传一个参数，也少一类「忘了传、于是把上次人定的值全丢了」的错。
    previous = Path(previous_path) if previous_path is not None else base / ASSUMPTIONS_NAME
    cfg = load_previous(previous if previous.is_file() else None)
    prev_drivers = _section(cfg, "drivers")
    prev_wacc = _section(cfg, "wacc")
    prev_company = _section(cfg, "company")

    fiscal = fiscal_year(latest, manifest_titles=titles)
    facts = year_facts(latest, fiscal_year=fiscal)
    advice = normalization_advice(latest, prior, titles=titles)
    prior_facts = (
        year_facts(prior, fiscal_year=fiscal_year(prior, manifest_titles=titles))
        if advice["applied"] and prior is not None
        else None
    )
    basis = prior_facts or facts  # 盈利能力取哪一年，由归一建议决定（建议不等于执行）

    title = titles.get(f"{latest.get('code')}_{latest.get('year')}") or "年度报告"

    fields: dict[str, Field] = {}

    def put(key: str, item: Field) -> None:
        fields[key] = item

    # ── [company] 元数据 ────────────────────────────────────────────
    put(
        "company.name",
        Field(
            key="company.name",
            # 上一版优先：人可能在 assumptions.toml 里把简称补成了工商全称，
            # 那属于「人写的东西」，不能被这次取到的简称冲掉（有测试盯着这条）。
            value=prev_company.get("name") or target.get("name") or target.get("code"),
            status=KEPT if prev_company.get("name") else DERIVED,
            source=(
                "沿用上一版文件里的公司全称"
                if prev_company.get("name")
                else "公司清单 company.json 里的名称（巨潮检索或年报入库时填的）"
            ),
        ),
    )
    put("company.code", Field("company.code", target.get("code"), DERIVED, "巨潮检索"))
    # 数据来源要说实话：从巨潮下的、和队友/使用者给的本地 PDF，是两件不同的事
    report_entry = manifest_entry(
        base, str(latest.get("code") or ""), int(latest.get("year") or 0)
    )
    imported = str(report_entry.get("method") or "") == "imported"
    put(
        "company.source",
        Field(
            "company.source",
            (
                f"本地年报 PDF/{title}（披露年份 {latest.get('year')}）"
                if imported
                else f"cninfo/{title}（披露年份 {latest.get('year')}）"
            ),
            DERIVED,
            (
                "工作台的年报入库：文件由使用者提供，sha256 记在 manifest.json"
                if imported
                else "下载清单 manifest.json 里记着的公告标题"
            ),
        ),
    )

    # ── [drivers] ──────────────────────────────────────────────────
    put(
        "drivers.revenue_base",
        _field(
            "drivers.revenue_base",
            derived=facts["revenue"],
            derived_source=f"年报抽取的营业总收入（{fiscal} 年）{_page(latest, 'revenue')}",
            previous=prev_drivers.get("revenue_base"),
        ),
    )
    put(
        "drivers.base_year",
        _field(
            "drivers.base_year",
            derived=fiscal,
            derived_source=f"报表年度，取自公告标题「{title}」",
            previous=prev_drivers.get("base_year"),
        ),
    )
    put(
        "drivers.years",
        _field(
            "drivers.years",
            default=int(prev_drivers.get("years") or DEFAULT_YEARS),
            default_source="默认五年显式预测期（可以在确认屏上改）",
            previous=prev_drivers.get("years"),
        ),
    )

    growth = None
    growth_source = ""
    if prior is not None:
        revenue_now = facts["revenue"]
        revenue_prior = year_facts(
            prior, fiscal_year=fiscal_year(prior, manifest_titles=titles)
        )["revenue"]
        if revenue_now and revenue_prior and revenue_prior > 0:
            raw_growth = revenue_now / revenue_prior - 1.0
            growth = min(max(raw_growth, 0.0), 0.03)
            growth_source = (
                f"上一财报年度收入同比 {raw_growth:+.1%}，规则取「不超过 3%、不为负」"
                f"→ 预测期按 {growth:.1%} 平推（保守）"
            )
    if growth is None:
        # 没有上一年的抽取记录时，看同一份年报的多年列示（取数层从报表的年份列取出来的）
        growth, growth_source = history_growth(payload)
    put(
        "drivers.revenue_growth",
        _field(
            "drivers.revenue_growth",
            derived=growth,
            derived_source=growth_source,
            previous=prev_drivers.get("revenue_growth"),
            default=0.03,
            default_source="保守取 3%：年报里没有上一年的列（也没下上一年的年报），算不出同比",
        ),
    )
    # 费用明细（销售/管理/研发费用）从 2026-10 起进了科目表；更早抽的记录里没有，
    # 那就倒挤：期间费用与税金及附加 = 毛利率 − EBIT 利润率 − 折旧摊销率。
    # 倒挤的口径比直接加总宽一点，所以在 source 里写明用了哪种，别让人以为是一回事。
    opex_value = basis.get("opex_pct")
    opex_label = "期间费用率（销售 + 管理 + 研发 + 财务费用 ÷ 收入）"
    if opex_value is None:
        gross = basis.get("gross_margin")
        margin = basis.get("ebit_margin")
        da_pct = basis.get("da_pct")
        if gross is not None and margin is not None and da_pct is not None:
            opex_value = gross - margin - da_pct
            opex_label = "期间费用率（倒挤：毛利率 − EBIT 利润率 − 折旧摊销率，含税金及附加）"

    for key, value, label, account in (
        ("gross_margin", basis.get("gross_margin"), "毛利率（1 − 营业成本 ÷ 收入）", "operating_cost"),
        ("opex_pct_revenue", opex_value, opex_label, "selling_expense"),
        (
            "da_pct_revenue",
            basis.get("da_pct"),
            "折旧摊销率（固定资产折旧 + 无形资产摊销 + 长期待摊摊销 ÷ 收入）",
            "depreciation_fixed_assets",
        ),
    ):
        source = ""
        if value is not None:
            year_label = (
                f"{basis['fiscal_year']} 年（归一建议命中的上一年）"
                if prior_facts is not None
                else f"{basis['fiscal_year']} 年"
            )
            source = f"{label}，按 {year_label} 的利润表算{_page(latest, account)}"
        put(
            f"drivers.{key}",
            _field(
                f"drivers.{key}",
                derived=value,
                derived_source=source,
                previous=prev_drivers.get(key),
            ),
        )

    da_value = fields["drivers.da_pct_revenue"].value
    put(
        "drivers.capex_pct_revenue",
        _field(
            "drivers.capex_pct_revenue",
            default=da_value if isinstance(da_value, (int, float)) else 0.045,
            default_source=(
                "维持性资本开支 ≈ 折旧摊销率。"
                "⚠️ 若按年报里的实际资本开支建模，扩产高峰年份的自由现金流会转负，结论会翻向"
            ),
            previous=prev_drivers.get("capex_pct_revenue"),
        ),
    )
    for key, account, label in (
        ("ar_pct_revenue", "accounts_receivable", "应收账款"),
        ("inventory_pct_revenue", "inventory", "存货"),
        ("ap_pct_revenue", "accounts_payable", "应付账款"),
    ):
        balance = _account(latest, account)
        put(
            f"drivers.{key}",
            _field(
                f"drivers.{key}",
                derived=_ratio(balance, facts["revenue"]),
                derived_source=(
                    f"{label}期末余额 ÷ {fiscal} 年营业总收入{_page(latest, account)}"
                ),
                previous=prev_drivers.get(key),
            ),
        )

    interest = facts["interest_expense"]
    debt = opening.get("interest_bearing_debt")
    cost_of_debt = _ratio(interest, debt)
    debt_rate_default = float(DEFAULT_MARKET_PARAMS["risk_free"]) + DEFAULT_CREDIT_SPREAD
    debt_rate_default_source = (
        f"规则默认：无风险利率 {DEFAULT_MARKET_PARAMS['risk_free']:.2%} + 信用利差 "
        f"{DEFAULT_CREDIT_SPREAD:.1%} = {debt_rate_default:.2%} —— 年报里没有单独的利息费用，"
        "反推不出实际利率。请用年报附注里的加权平均利率复核（或直接改成实际利率）"
    )
    put(
        "drivers.interest_rate_on_debt",
        _field(
            "drivers.interest_rate_on_debt",
            derived=cost_of_debt,
            derived_source=(
                f"利息支出 {interest:,.0f} 元 ÷ 有息负债 {debt:,.0f} 元"
                if interest is not None and debt
                else ""
            ),
            previous=prev_drivers.get("interest_rate_on_debt"),
            default=debt_rate_default,
            default_source=debt_rate_default_source,
            missing_source=(
                "有息负债为 0（或没抽到），算不出债务利率。没有有息负债时填 0 即可；"
                "有债但报表没单独披露利息费用时，用年报附注的加权平均利率。"
            ),
        ),
    )
    put(
        "drivers.tax_rate",
        _field(
            "drivers.tax_rate",
            default=float(prev_drivers.get("tax_rate") or STATUTORY_TAX_RATE),
            default_source="法定税率 25%（《企业所得税法》第四条）",
            previous=prev_drivers.get("tax_rate"),
        ),
    )
    payout, payout_source = payout_ratio_from_record(latest, facts)
    put(
        "drivers.dividend_payout_ratio",
        _field(
            "drivers.dividend_payout_ratio",
            derived=payout,
            derived_source=payout_source,
            previous=prev_drivers.get("dividend_payout_ratio"),
            keep_source="沿用上一版：分红预案不在抽取的科目表里，需要人工核对年报「每 10 股派 X 元」",
            missing_source=(
                "年报里没有能直接算出分红比例的数。两条路：看「利润分配预案」"
                "（每 10 股派 X 元 × 股数 ÷ 归母净利润），或用现金流量表里"
                "「分配股利、利润或偿付利息支付的现金」÷ 归母净利润（这一行含利息，会略偏高）。"
            ),
        ),
    )
    put(
        "drivers.net_debt_repayment",
        _field(
            "drivers.net_debt_repayment",
            default=float(prev_drivers.get("net_debt_repayment") or 0.0),
            default_source="默认为 0：假设不额外还债（可在确认屏上改）",
            previous=prev_drivers.get("net_debt_repayment"),
        ),
    )
    put(
        "drivers.minority_share_of_profit",
        _field(
            "drivers.minority_share_of_profit",
            default=float(prev_drivers.get("minority_share_of_profit") or 0.0),
            default_source="默认为 0：少数股东损益不参与预测分配",
            previous=prev_drivers.get("minority_share_of_profit"),
        ),
    )

    # ── [wacc] ─────────────────────────────────────────────────────
    params = dict(DEFAULT_MARKET_PARAMS)
    params.update(market_params or {})
    put(
        "wacc.risk_free",
        _field(
            "wacc.risk_free",
            previous=prev_wacc.get("risk_free"),
            keep_source="沿用上一版文件里查证过的无风险利率（这次没有重新查）",
            default=float(params["risk_free"]),
            default_source=MARKET_PARAMS_SOURCE,
        ),
    )
    put(
        "wacc.beta",
        _field(
            "wacc.beta",
            derived=beta,
            derived_source=(
                f"60 个月月度收益率对沪深300 回归 = {beta:.4f}" if beta is not None else ""
            ),
            previous=prev_wacc.get("beta"),
            keep_source="沿用上一版：这次没算 beta（需要联网取行情）",
            missing_source=(
                "这次没算出 beta（要联网取行情）。A 股公司重跑一次取数就能回归出来；"
                "港股/美股的公司**不要**用沪深300 那条口径 —— 请填你在 Wind / Choice / "
                "官网查到的 5 年月度 beta（对比本地市场指数），并把出处写进「依据」一栏。"
            ),
        ),
    )
    put(
        "wacc.equity_risk_premium",
        _field(
            "wacc.equity_risk_premium",
            previous=prev_wacc.get("equity_risk_premium"),
            keep_source="沿用上一版文件里查证过的股权风险溢价（这次没有重新查）",
            default=float(params["equity_risk_premium"]),
            default_source=MARKET_PARAMS_SOURCE,
        ),
    )
    put(
        "wacc.cost_of_debt",
        _field(
            "wacc.cost_of_debt",
            derived=cost_of_debt,
            derived_source=(
                f"利息支出 ÷ 有息负债 = {cost_of_debt:.4f}"
                if cost_of_debt is not None
                else ""
            ),
            previous=prev_wacc.get("cost_of_debt"),
            default=debt_rate_default,
            default_source=debt_rate_default_source,
            missing_source=(
                "有息负债为 0（或没抽到），算不出债务成本。没有有息负债时填 0 即可。"
            ),
        ),
    )

    # ── [terminal] ─────────────────────────────────────────────────
    prev_terminal = _section(cfg, "terminal")
    put(
        "terminal.terminal_method",
        _field(
            "terminal.terminal_method",
            previous=prev_terminal.get("terminal_method"),
            keep_source="沿用上一版选择的终值算法",
            default="perpetuity",
            default_source="默认永续增长法（另一种是退出倍数法）",
        ),
    )
    put(
        "terminal.terminal_growth",
        _field(
            "terminal.terminal_growth",
            previous=prev_terminal.get("terminal_growth"),
            keep_source="沿用上一版确认过的永续增长率",
            default=DEFAULT_TERMINAL_GROWTH,
            default_source="保守取 2.5%：低于 IMF 对中国的中期增速预测，并留出向长期名义 GDP 收敛的空间",
        ),
    )
    put(
        "terminal.mid_year",
        _field(
            "terminal.mid_year",
            previous=prev_terminal.get("mid_year"),
            keep_source="沿用上一版选择的折现惯例",
            default=False,
            default_source="默认不采用年中折现惯例（更保守；True 更贴近现实）",
        ),
    )

    # ── [opening_balance] 11 个科目 ─────────────────────────────────
    for name, value in opening.items():
        put(
            f"opening_balance.{name}",
            Field(
                key=f"opening_balance.{name}",
                value=float(value),
                status=DERIVED,
                source=f"年报抽取（{fiscal} 年期末）；每个科目的页码与原文见 extracted/pipeline_input.json",
            ),
        )

    notes = [
        f"报表年度 {fiscal} 年，披露年份 {latest.get('year')}；"
        f"共取到 {len(records)} 份年报"
        + ("（含上一份，可用于归一）" if prior is not None else "（缺上一份，无法判断是否需要归一）"),
    ]
    if advice["applied"]:
        notes.append("归一建议：" + str(advice["reason"]))
    elif advice.get("reason"):
        notes.append(str(advice["reason"]))
    missing = [key for key, item in fields.items() if item.status == MISSING]
    if missing:
        notes.append("这些字段需要人工填：" + "、".join(missing))

    return Draft(
        company={
            "name": fields["company.name"].value,
            "code": fields["company.code"].value,
            "source": fields["company.source"].value,
        },
        fields=fields,
        normalized=advice,
        notes=notes,
        generated_at=now_iso(),
    )


# ---------------------------------------------------------------------------
# 渲染成 assumptions.toml
# ---------------------------------------------------------------------------

STATUS_LABEL = {
    DERIVED: "自动推导",
    DEFAULT: "规则默认",
    KEPT: "沿用上一版",
    MISSING: "⚠️ 需人工填写",
    "human": "人工确认",
}

DRIVERS_ORDER = (
    "revenue_base",
    "base_year",
    "years",
    "revenue_growth",
    "gross_margin",
    "opex_pct_revenue",
    "da_pct_revenue",
    "capex_pct_revenue",
    "ar_pct_revenue",
    "inventory_pct_revenue",
    "ap_pct_revenue",
    "interest_rate_on_debt",
    "tax_rate",
    "dividend_payout_ratio",
    "net_debt_repayment",
    "minority_share_of_profit",
)
WACC_ORDER = ("risk_free", "beta", "equity_risk_premium", "cost_of_debt")
TERMINAL_ORDER = ("terminal_method", "terminal_growth", "mid_year")
OPENING_ORDER = (
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

SENSITIVITY_DEFAULTS: dict[str, Any] = {
    "wacc_delta": 0.01,
    "terminal_growth_delta": 0.01,
    "revenue_growth_delta": 0.03,
    "ebit_margin_delta": 0.03,
    "tax_rate_delta": 0.05,
    "grid_points": 5,
}
REPORT_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "model": "deepseek-v4-pro",
    "base_url": "https://api.deepseek.com",
    "api_key_env": "DEEPSEEK_API_KEY",
    "api_key_file": "apikey.txt",
    "temperature": 0.3,
    "max_tokens": 32000,
    "timeout_s": 120.0,
    "effort": "high",
}

# 只给这些字段写 [evidence] 台账：它们是「判断」，评审会问凭什么。
# 其余字段是机械的公式套用，写进台账只会把真正的判断淹掉。
EVIDENCE_KEYS = (
    "drivers.revenue_growth",
    "drivers.capex_pct_revenue",
    "drivers.tax_rate",
    "drivers.interest_rate_on_debt",
    "wacc.beta",
    "wacc.risk_free",
    "wacc.equity_risk_premium",
    "wacc.cost_of_debt",
    "terminal.terminal_growth",
)


def _render_value(value: Any) -> str:
    """Python 值 → TOML 字面量。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_render_value(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def _comment(item: Field) -> str:
    """行尾注释：先说这个数是怎么来的，再说来源。"""
    label = STATUS_LABEL.get(item.status, item.status)
    source = str(item.source or "").replace("\n", " ").strip()
    if item.status == MISSING:
        return f"{label}：{source}"
    return f"{label}：{source}" if source else label


def render_toml(draft: Draft, *, previous: Mapping[str, Any] | None = None) -> str:
    """把草稿渲染成 assumptions.toml 的文本。

    两条规矩：
      1. `[evidence]` 里人写的条目**原样保留**，自动条目只补空缺的键
         （人的说明比机器的话有用，不能让后者顶掉前者）；
      2. `[sensitivity]` / `[report]` 整节沿用上一版 ——
         那是人调过的运行参数（模型、温度、浮动幅度），不是这次要重算的东西。
    """
    prev = previous or {}
    lines: list[str] = [
        "# ============================================================================\n",
        "# FinAI 经营假设与基期数据（**由工作台自动生成**）\n",
        f"# 生成时间：{draft.generated_at}\n",
        "#\n",
        "# 每个字段行尾的注释说明它是怎么来的：\n",
        "#    自动推导    从年报抽取结果或行情算出来的\n",
        "#    规则默认    保守规则给的默认值 —— 应当复核，尤其是资本开支口径\n",
        "#    沿用上一版  这次算不出来，沿用上一版文件里的值\n",
        "#    ⚠️ 需人工填写  年报里没有这个数，程序不猜\n",
        "#\n",
        "# 覆盖旧文件前会自动备份成 assumptions.<时间戳>.bak。\n",
        "# ============================================================================\n",
        "\n",
    ]

    def emit(section: str, order: Sequence[str], *, title: str = "") -> None:
        if title:
            lines.append(f"# --- {title} ---\n")
        lines.append(f"[{section}]\n")
        emitted = False
        for name in order:
            item = draft.fields.get(f"{section}.{name}")
            if item is None:
                continue
            if item.value is None:
                # 抽不到的字段**不写值**，只留一行注释。写 0 或 "None" 会被下游
                # 当成真值算进去（分红比例 0 的意思是「一分钱不分」，那是另一个假设）；
                # 留白则会让配置层在必填字段缺失时直接报错，逼人来填。
                lines.append(f"# {name} = ???   # {_comment(item)}\n")
                emitted = True
                continue
            lines.append(f"{name} = {_render_value(item.value)}".ljust(46) + f"# {_comment(item)}\n")
            emitted = True
        if not emitted:
            del lines[-1]  # 空小节不写出来：留个光杆 [section] 会让人以为配置漏了
            if title:
                lines.pop()
        else:
            lines.append("\n")

    lines.append("# ---------------------------------------------------------------------------\n")
    lines.append("# 一、标识\n")
    lines.append("# ---------------------------------------------------------------------------\n\n")
    emit("company", ("name", "code", "source"))

    lines.append("# ---------------------------------------------------------------------------\n")
    lines.append("# 二、三表预测的驱动假设（对应 ForecastDrivers）\n")
    lines.append("# ---------------------------------------------------------------------------\n\n")
    emit("drivers", DRIVERS_ORDER)

    lines.append("# ---------------------------------------------------------------------------\n")
    lines.append("# 三、估值参数与终值\n")
    lines.append("# ---------------------------------------------------------------------------\n\n")
    emit("wacc", WACC_ORDER)
    emit("terminal", TERMINAL_ORDER)

    # [sensitivity] 与 [report]：整节沿用上一版，没有就用默认值。
    for section, order, defaults in (
        ("sensitivity", tuple(SENSITIVITY_DEFAULTS), SENSITIVITY_DEFAULTS),
        ("report", tuple(REPORT_DEFAULTS), REPORT_DEFAULTS),
    ):
        saved = _section(prev, section)
        values = {**defaults, **saved}
        note = "沿用上一版文件里的运行参数" if saved else "取默认值（可在这里改）"
        lines.append(f"[{section}]   # {note}\n")
        for name in order:
            if name in values:
                lines.append(f"{name} = {_render_value(values[name])}\n")
        lines.append("\n")

    # [evidence]：人写的原样保留，自动条目只补空缺
    previous_evidence = {
        str(key): str(value) for key, value in _section(prev, "evidence").items()
    }
    added: list[tuple[str, str]] = []
    for key in EVIDENCE_KEYS:
        item = draft.fields.get(key)
        if item is None or key in previous_evidence or not item.source:
            continue
        added.append((key, f"{STATUS_LABEL.get(item.status, item.status)}：{item.source}"))
    if draft.normalized.get("applied"):
        if "drivers.profitability_normalized" not in previous_evidence:
            added.append(
                ("drivers.profitability_normalized", str(draft.normalized.get("reason") or ""))
            )
    if "opening_balance" not in previous_evidence:
        added.append(
            (
                "opening_balance",
                "年报抽取：11 个科目都来自抽取记录，每个科目的页码与原文见 "
                "extracted/pipeline_input.json 的 provenance（read 表示直接读到，derived 表示倒挤）",
            )
        )
    lines.append("# ---------------------------------------------------------------------------\n")
    lines.append("# 四、假设依据台账（人写的条目原样保留，下面补的是这次自动生成的）\n")
    lines.append("# ---------------------------------------------------------------------------\n\n")
    lines.append("[evidence]\n")
    # 键必须加引号：在 TOML 里 `drivers.tax_rate = "..."` 是**点号键**，
    # 会被解析成嵌套表 drivers = {tax_rate = ...}，台账整段结构就散了、
    # 报告里也读不出来。仓库里人写的那几条依据一直是带引号的写法。
    for key, text in previous_evidence.items():
        lines.append(f"{_render_value(key)} = {_render_value(text)}\n")
    for key, text in added:
        lines.append(f"{_render_value(key)} = {_render_value(text)}\n")
    lines.append("\n")

    lines.append("# ---------------------------------------------------------------------------\n")
    lines.append("# 五、基期资产负债表（历史事实，来自年报抽取）\n")
    lines.append("# 恒等式：资产 = 负债 + 权益；资产 = 现金 + 应收 + 存货 + 固定资产 + 其他非流动\n")
    lines.append("# ---------------------------------------------------------------------------\n\n")
    emit("opening_balance", OPENING_ORDER)

    return "".join(lines)


def write_assumptions(
    draft: Draft,
    path: str | Path,
    *,
    previous_path: str | Path | None = None,
) -> tuple[Path, Path | None]:
    """落盘 assumptions.toml，覆盖前先备份。返回 (新文件, 备份文件或 None)。"""
    target = Path(path)
    source = Path(previous_path) if previous_path is not None else target
    previous = load_previous(source if source.is_file() else None)
    text = render_toml(draft, previous=previous)

    backup: Path | None = None
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup = target.with_name(f"{target.stem}.{stamp}.bak")
        target.replace(backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target, backup


def default_paths(company_dir: str | Path) -> tuple[Path, Path]:
    """某家公司目录下的（草稿, 假设文件）两个默认路径。"""
    base = Path(company_dir)
    return base / DRAFT_NAME, base / ASSUMPTIONS_NAME


def draft_for(company_dir: str | Path) -> Draft:
    """按默认路径读草稿（工作台读的就是这一份）。"""
    draft_path, _ = default_paths(company_dir)
    return Draft.load(draft_path)


def assumptions_path(company_dir: str | Path) -> Path:
    return Path(company_dir) / ASSUMPTIONS_NAME


def missing_fields(draft: Draft) -> list[str]:
    """值为 None 的字段：没填之前流水线跑不起来（必填字段缺一个就报错）。

    单独抽成函数是给「确认并估值」那一步用的 —— 与其让配置层抛一句
    「字段对不上」，不如在点按钮的时候就告诉人「这几项还没填」。
    """
    return [key for key, item in draft.fields.items() if item.value is None]


def require_complete(draft: Draft) -> None:
    """确认屏点「确认并估值」时先过这一关。"""
    gaps = missing_fields(draft)
    if gaps:
        raise DraftError("这些字段还没填，填完才能跑流水线：" + "、".join(gaps))


def fallback_config_path() -> Path:
    """没有公司目录时用的默认假设文件（仓库里的演示夹具）。"""
    return DEFAULT_CONFIG_PATH
