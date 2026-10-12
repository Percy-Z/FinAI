"""配置层：把 data/ 下的文本文件翻译成算法模块认识的对象。

这一层只做三件事：
    1. 读文件
    2. 检查数据是否合法
    3. 构造成 algorithm/finai 里的数据类对象

它明确不做三件事：
    不算任何数、不换算单位、不写任何产物。

设计要点
--------
字段名与算法模块里的数据类完全一致（收入基数就叫 revenue_base），
因此构造对象时可以整个字典展开。好处是字段名拼错会立刻抛出
TypeError，而不是被静默忽略成一个默认值。

格式选择
--------
假设文件用 TOML 而不是 YAML。原因：本项目坚持零第三方依赖，
而 YAML 需要 PyYAML，tomllib 是 Python 3.11+ 的标准库。
注意 README.md 和 algorithm/shuoming.md 里目前仍写着「读 YAML 假设文件」，
那是还没同步的旧说法，以本文件为准（2026-09-28 决定）。

将来 [opening_balance] 会改由抽取层从年报生成。届时只需替换
load_opening_balance 的内部实现，函数签名和下游代码都不用动。

运行要求
--------
必须在仓库根目录（FinAI/）下运行模块，例如：

    cd ~/Desktop/FinAI
    python3 -m pipeline.config
"""

from __future__ import annotations

import csv
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from algorithm.finai.comps import CompanyMetrics
from algorithm.finai.forecast import (
    BalanceSheetState,
    ForecastDrivers,
    as_series,
)
from algorithm.output.narrative import NarrativeRequest

from .console import safe_console

# 默认配置路径。用 __file__ 往上退两级定位仓库根目录，
# 这样不管当前工作目录在哪都能找对文件。
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DEFAULT_CONFIG_PATH = DATA_DIR / "assumptions.toml"
DEFAULT_MARKET_PATH = DATA_DIR / "market.csv"
DEFAULT_PEER_PATH = DATA_DIR / "peers.csv"

# 可比公司财务文件允许出现的列。多出别的列就报错，能挡住表头拼错。
PEER_FIELDS = frozenset(
    {
        "code",
        "revenue",
        "net_profit_attributable",
        "book_equity_attributable",
        "ebit",
        "ebitda",
        "as_of",
    }
)

PEER_FLOAT_FIELDS = (
    "revenue",
    "net_profit_attributable",
    "book_equity_attributable",
    "ebit",
    "ebitda",
)

# 市场快照允许出现的列。多出别的列就报错 —— 这能挡住表头拼错。
MARKET_FIELDS = frozenset(
    {
        "code",
        "name",
        "role",
        "price",
        "diluted_shares",
        "interest_bearing_debt",
        "cash",
        "minority_interest",
        "preferred_stock",
        "non_operating_assets",
        "as_of",
    }
)

# 需要转成数字的列
MARKET_FLOAT_FIELDS = (
    "price",
    "diluted_shares",
    "interest_bearing_debt",
    "cash",
    "minority_interest",
    "preferred_stock",
    "non_operating_assets",
)

# 这些列允许留空，留空表示 0；其余数字列留空算错误
MARKET_ZERO_IF_EMPTY = (
    "interest_bearing_debt",
    "cash",
    "minority_interest",
    "preferred_stock",
    "non_operating_assets",
)


class ConfigError(Exception):
    """配置文件本身有问题。

    单独定义一个异常类型，是为了让上层能把它和「代码 bug」区分开：
    配置错属于硬失败，流水线应该立刻停下并把这句话原样打印给用户；
    而别的异常可能是 bug，需要保留完整调用栈去排查。
    """


@dataclass(frozen=True)
class CompanyInfo:
    """[company] 小节：标识信息，不是假设也不是事实，只是元数据。"""

    name: str
    code: str
    source: str = ""


@dataclass(frozen=True)
class WaccParams:
    """WACC 的输入参数。

    刻意不包含三样东西：
        equity_weight / debt_weight   它们是「推导型参数」，由市值和债务算出来，
                                      人填了就等于给了两个可能互相矛盾的来源
        tax_rate                      它来自 [drivers]，全项目只有一个来源。
                                      构造 WACC 时显式写成 tax_rate=drivers.tax_rate，
                                      让「用的是同一个税率」这件事在代码里看得见
    """

    risk_free: float
    beta: float
    equity_risk_premium: float
    cost_of_debt: float


@dataclass(frozen=True)
class TerminalParams:
    """终值假设：显式预测期结束之后，公司还值多少。

    两种算法选一种，由 terminal_method 决定：
        perpetuity      永续增长法，用 terminal_growth
        exit_multiple   退出倍数法，用 exit_multiple

    mid_year 是折现惯例：为真时假设现金流在年中发生，折现期数减 0.5。
    它不影响现金流，只影响折现因子。

    这里的两条校验算法模块也会做一遍。重复是有意的：在这里能说清
    「是哪个文件的哪个字段」，在算法层只能说「参数不对」。
    """

    terminal_method: str = "perpetuity"
    terminal_growth: float = 0.0
    exit_multiple: float | None = None
    mid_year: bool = False

    def __post_init__(self) -> None:
        if self.terminal_method not in ("perpetuity", "exit_multiple"):
            raise ValueError(
                "terminal_method 只能是 'perpetuity' 或 'exit_multiple'，"
                f"收到 {self.terminal_method!r}"
            )
        if self.terminal_method == "exit_multiple" and self.exit_multiple is None:
            raise ValueError(
                "终值用退出倍数法（terminal_method = 'exit_multiple'）时，"
                "必须提供 exit_multiple"
            )


@dataclass(frozen=True)
class SensitivityParams:
    """敏感性分析的浮动幅度。

    只放「上下浮动多少」，基准值一律取自 [drivers] / [wacc] / [terminal]。
    理由与 WaccParams 不放权重一样：同一个数字有两个来源，迟早会打架。

    这几个幅度会影响龙卷风图的排序，所以它们本身也是需要解释的判断，
    不是随手填的常数。
    """

    wacc_delta: float = 0.01
    terminal_growth_delta: float = 0.01
    revenue_growth_delta: float = 0.03
    ebit_margin_delta: float = 0.03
    tax_rate_delta: float = 0.05
    grid_points: int = 5

    def __post_init__(self) -> None:
        for name in (
            "wacc_delta",
            "terminal_growth_delta",
            "revenue_growth_delta",
            "ebit_margin_delta",
            "tax_rate_delta",
        ):
            value = getattr(self, name)
            if value <= 0:
                raise ValueError(f"{name} 必须为正数，收到 {value}")
        if self.grid_points < 2:
            raise ValueError(f"grid_points 至少为 2，收到 {self.grid_points}")


@dataclass(frozen=True)
class PeerFinancials:
    """一家可比公司的年度财务数据。

    性质和 [opening_balance] 一样属于「历史事实」，不是我们的假设。
    现在手填，将来由抽取层从可比公司年报生成。
    """

    code: str
    revenue: float
    net_profit_attributable: float
    book_equity_attributable: float
    ebit: float
    ebitda: float
    as_of: str = ""


def read_toml(path: str | Path) -> dict[str, Any]:
    """读一个 TOML 文件，返回普通字典。

    注意 tomllib.load 要求二进制模式打开（"rb"），传文本模式会报错。
    """
    target = Path(path)
    if not target.exists():
        raise ConfigError(f"配置文件不存在：{target}")
    with target.open("rb") as handle:
        return tomllib.load(handle)


def _section(cfg: dict[str, Any], name: str, path: str | Path) -> dict[str, Any]:
    """取出一个小节。缺了就说清楚缺哪个，而不是让 KeyError 冒出去。"""
    if name not in cfg:
        raise ConfigError(f"{path} 里缺少 [{name}] 小节")
    if not isinstance(cfg[name], dict):
        raise ConfigError(f"{path} 里的 [{name}] 应该是一个小节（方括号开头）")
    return cfg[name]


def _build(cls: type, payload: dict[str, Any], path: str | Path, section: str):
    """把字典构造成数据类对象，并把两类错误翻译成人话。

    TypeError  —— 字段名拼错、或者漏了必填字段
    ValueError —— 字段名对，但取值不合法（数据类自己的校验拦下来的）
    """
    try:
        return cls(**payload)
    except TypeError as err:
        raise ConfigError(
            f"{path} 的 [{section}] 字段对不上：{err}\n"
            f"    提示：字段名必须与算法模块的数据类一致，且必填字段不能少。"
        ) from err
    except ValueError as err:
        raise ConfigError(f"{path} 的 [{section}] 取值不合法：{err}") from err


def load_company(path: str | Path = DEFAULT_CONFIG_PATH) -> CompanyInfo:
    """读 [company] 小节。"""
    cfg = read_toml(path)
    return _build(CompanyInfo, _section(cfg, "company", path), path, "company")


def load_drivers(path: str | Path = DEFAULT_CONFIG_PATH) -> ForecastDrivers:
    """读 [drivers] 小节 —— 经营假设，也就是「我们判断出来的部分」。"""
    cfg = read_toml(path)
    return _build(ForecastDrivers, _section(cfg, "drivers", path), path, "drivers")


def _opening_from(cfg: dict[str, Any], path: str | Path) -> BalanceSheetState:
    """构造基期资产负债表，并立刻检查它是否配平。

    为什么这个检查放在配置层：资产负债表不平属于「输入数据自相矛盾」，
    不是代码问题。如果不在这里拦，它会一路走到三表预测里才炸，报错是
    一段 Python 堆栈，只说「差额 45」，不说是哪个文件的哪几个科目。

    容差与算法层保持一致（1e-6）：这里通过了，三表预测就一定能通过。
    """
    opening = _build(
        BalanceSheetState,
        _section(cfg, "opening_balance", path),
        path,
        "opening_balance",
    )
    gap = opening.balance_gap
    if abs(gap) > 1e-6:
        raise ConfigError(
            f"{path} 的 [opening_balance] 不平：\n"
            f"    资产总计  {opening.total_assets:>16,.2f}\n"
            f"    负债合计  {opening.total_liabilities:>16,.2f}\n"
            f"    权益合计  {opening.total_equity:>16,.2f}\n"
            f"    差额      {gap:>16,.2f}   （应为 0）\n"
            f"    检查资产侧的 cash / accounts_receivable / inventory /\n"
            f"    fixed_assets / other_non_current_assets，是否等于\n"
            f"    负债侧的 accounts_payable / interest_bearing_debt /\n"
            f"    other_non_current_liabilities 加权益侧的 share_capital /\n"
            f"    retained_earnings / minority_equity。"
        )
    return opening


def load_opening_balance(path: str | Path = DEFAULT_CONFIG_PATH) -> BalanceSheetState:
    """读 [opening_balance] 小节 —— 基期资产负债表，性质是「历史事实」。

    现在从 TOML 读。等抽取层做好之后，这里改成从 financials.json 读，
    函数签名不变，调用方不用改。
    """
    return _opening_from(read_toml(path), path)


def load_case(
    path: str | Path = DEFAULT_CONFIG_PATH,
) -> tuple[CompanyInfo, ForecastDrivers, BalanceSheetState]:
    """一次读齐三样东西。只是为了调用方便，三个 load_* 仍然是各自独立的。"""
    cfg = read_toml(path)
    company = _build(CompanyInfo, _section(cfg, "company", path), path, "company")
    drivers = _build(ForecastDrivers, _section(cfg, "drivers", path), path, "drivers")
    opening = _opening_from(cfg, path)
    return company, drivers, opening


def load_wacc_params(path: str | Path = DEFAULT_CONFIG_PATH) -> WaccParams:
    """读 [wacc] 小节 —— WACC 的四个输入参数。"""
    cfg = read_toml(path)
    return _build(WaccParams, _section(cfg, "wacc", path), path, "wacc")


def load_terminal_params(path: str | Path = DEFAULT_CONFIG_PATH) -> TerminalParams:
    """读 [terminal] 小节 —— 终值假设。"""
    cfg = read_toml(path)
    return _build(
        TerminalParams, _section(cfg, "terminal", path), path, "terminal"
    )


def load_sensitivity_params(
    path: str | Path = DEFAULT_CONFIG_PATH,
) -> SensitivityParams:
    """读 [sensitivity] 小节 —— 敏感性分析的浮动幅度。"""
    cfg = read_toml(path)
    return _build(
        SensitivityParams, _section(cfg, "sensitivity", path), path, "sensitivity"
    )


def load_report_params(
    path: str | Path = DEFAULT_CONFIG_PATH,
) -> NarrativeRequest:
    """读 [report] 小节 —— 报告叙述生成的参数。

    [report] 小节可以不存在：这时用全默认值（启用、deepseek-v4-pro）。
    老配置文件不会因为多了个新功能就读不出来。

    真正决定跑不跑得成大模型的是环境变量里有没有 API key —— 没有 key 时
    叙述层自己降级，报告照常出，流水线不会失败。
    """
    cfg = read_toml(path)
    if "report" not in cfg:
        return NarrativeRequest()
    return _build(NarrativeRequest, _section(cfg, "report", path), path, "report")


def load_evidence(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, str]:
    """读 [evidence] 小节 —— 每条假设的依据与状态。

    通知把「假设依据」列为选题 4 的考察点之一。依据是人写进去的判断说明，
    不是代码能推导出来的东西，所以它属于配置层，由组员逐条填。

    做成自由格式的键值对（参数名 -> 依据说明）而不是逐字段的结构化定义，
    是因为每条依据的写法差别很大：有的引年报页码，有的引可比公司，
    有的直接写「⚠️ 待查」。用一张表把它们登记下来，比强行统一格式更实际。

    [evidence] 可以不存在（返回空字典），报告里会说明依据台账为空。
    """
    cfg = read_toml(path)
    if "evidence" not in cfg:
        return {}
    section = _section(cfg, "evidence", path)
    return {str(key): str(value) for key, value in section.items()}


def load_peer_financials(
    path: str | Path = DEFAULT_PEER_PATH,
) -> dict[str, PeerFinancials]:
    """读可比公司财务数据，返回 {代码: PeerFinancials}。

    与 load_market_snapshot 分开读，因为两个文件的基准日不同：
    市场快照是估值基准日的，这份是最近一个完整年度的。
    合并时按 code 对齐，对不上的公司由适配层提示出来，不在这里报错。
    """
    rows = read_csv(path)
    if not rows:
        raise ConfigError(f"{path} 里没有任何数据行")

    unknown = set(rows[0]) - PEER_FIELDS
    if unknown:
        raise ConfigError(
            f"{path} 的表头里有无法识别的列：{sorted(unknown)}\n"
            f"    可用的列是：{sorted(PEER_FIELDS)}"
        )

    result: dict[str, PeerFinancials] = {}
    for row_number, row in enumerate(rows, start=2):
        code = (row.get("code") or "").strip()
        if not code:
            raise ConfigError(f"{path} 第 {row_number} 行缺少 code")
        if code in result:
            raise ConfigError(
                f"{path} 第 {row_number} 行的 code={code!r} 重复，"
                "一家公司只能出现一次"
            )
        payload: dict[str, Any] = {
            "code": code,
            "as_of": (row.get("as_of") or "").strip(),
        }
        for field in PEER_FLOAT_FIELDS:
            payload[field] = _to_float(row.get(field), field, row_number, path)
        result[code] = _build(PeerFinancials, payload, path, f"第 {row_number} 行")
    return result


def read_csv(path: str | Path) -> list[dict[str, str]]:
    """读一个 CSV，返回「每行一个字典」的列表。第一行是表头。

    三个细节：
        1. encoding 用 utf-8-sig：Excel 另存为 CSV 时会在开头加一个隐藏的
           BOM 字符，用普通 utf-8 读会让第一个列名变成 "\\ufeffcode"，
           然后所有取值都报「没有 code 这一列」。utf-8-sig 会自动去掉它。
        2. newline=""：csv 模块的官方要求，不写的话 Windows 换行会读错。
        3. 以 # 开头的行当注释跳过 —— CSV 本身没有注释语法，但数据文件
           需要能写说明，所以这里人为约定一条。
    """
    target = Path(path)
    if not target.exists():
        raise ConfigError(f"数据文件不存在：{target}")

    with target.open(newline="", encoding="utf-8-sig") as handle:
        lines = [line for line in handle if not line.lstrip().startswith("#")]

    reader = csv.DictReader(lines)
    if reader.fieldnames is None:
        raise ConfigError(f"{target} 是空文件，缺少表头")
    return [row for row in reader]


def _to_float(
    value: Any,
    field: str,
    row_number: int,
    path: str | Path,
    default: float | None = None,
) -> float:
    """把 CSV 里的字符串转成数字。CSV 读出来全是字符串，这一步不能省。

    default 不为 None 时，空单元格取这个默认值；否则空单元格算错误。
    """
    text = ("" if value is None else str(value)).strip()
    if text == "":
        if default is None:
            raise ConfigError(f"{path} 第 {row_number} 行缺少必填的 {field}")
        return default
    try:
        return float(text)
    except ValueError:
        raise ConfigError(
            f"{path} 第 {row_number} 行的 {field} 不是数字：{value!r}"
        ) from None


def load_market_snapshot(
    path: str | Path = DEFAULT_MARKET_PATH,
) -> tuple[CompanyMetrics, list[CompanyMetrics]]:
    """读市场快照，返回 (目标公司, 可比公司列表)。

    role 列指定身份：role=target 的是被估值公司（应该正好一行），
    role=peer 的是可比公司。

    返回的是 CompanyMetrics，但利润类指标（净利润、净资产、收入、EBITDA）
    一律为空 —— 那些不属于市场快照，来自年报或三表预测，由适配层补上。
    它们为空时相对估值会跳过对应的倍数，不会崩。
    """
    rows = read_csv(path)
    if not rows:
        raise ConfigError(f"{path} 里没有任何数据行")

    # 表头拼错是最难查的错误，这里提前拦住
    unknown = set(rows[0]) - MARKET_FIELDS
    if unknown:
        raise ConfigError(
            f"{path} 的表头里有无法识别的列：{sorted(unknown)}\n"
            f"    可用的列是：{sorted(MARKET_FIELDS)}"
        )

    targets: list[CompanyMetrics] = []
    peers: list[CompanyMetrics] = []
    for row_number, row in enumerate(rows, start=2):  # 第 1 行是表头
        role = (row.get("role") or "").strip().lower()
        if role not in ("target", "peer"):
            raise ConfigError(
                f"{path} 第 {row_number} 行的 role 只能是 target 或 peer，"
                f"收到 {role!r}"
            )

        payload: dict[str, Any] = {
            "code": (row.get("code") or "").strip(),
            "name": (row.get("name") or "").strip(),
            "as_of": (row.get("as_of") or "").strip(),
        }
        for field in MARKET_FLOAT_FIELDS:
            payload[field] = _to_float(
                row.get(field),
                field,
                row_number,
                path,
                default=0.0 if field in MARKET_ZERO_IF_EMPTY else None,
            )

        company = _build(CompanyMetrics, payload, path, f"第 {row_number} 行")
        (targets if role == "target" else peers).append(company)

    if len(targets) != 1:
        raise ConfigError(
            f"{path} 里 role=target 的行应该正好一行，实际有 {len(targets)} 行"
        )
    if not peers:
        raise ConfigError(f"{path} 里没有任何 role=peer 的行，相对估值无法进行")

    return targets[0], peers


def _describe_series(values: Sequence[float] | float) -> str:
    """把逐年字段打印成人看得懂的样子。

    入参可能是标量（配置文件里写单个数字，会被广播到每一年），
    所以先用 as_series 统一成序列再判断。
    这里判断「每年都一样」时只显示一个数，避免刷出一串重复值。
    """
    series = as_series(values)
    if len(set(series)) == 1:
        return f"{series[0]:.4f}（每年相同）"
    return "(" + ", ".join(f"{v:.4f}" for v in series) + ")"


def _demo() -> None:
    """自检：读默认配置并把结果打印出来。python3 -m pipeline.config"""
    safe_console()
    print(f"配置文件：{DEFAULT_CONFIG_PATH}")

    # 只接住 ConfigError —— 配置错是「预料之中的失败」，打印人话就够了。
    # 其他异常一律不接，让它带着完整堆栈冒出去，因为那属于代码 bug，需要排查。
    try:
        company, drivers, opening = load_case()
    except ConfigError as err:
        print(f"\n[配置有误，已停止]\n{err}")
        raise SystemExit(1) from None

    print(f"\n[company]        {company.name}（{company.code}）来源 {company.source}")

    print("\n[drivers] 经营假设")
    print(f"    收入基数 {drivers.revenue_base:,.0f} 元，基期 {drivers.base_year}，"
          f"预测 {drivers.years} 年")
    print(
        f"    收入增速 {tuple(round(g, 4) for g in as_series(drivers.revenue_growth))}"
    )
    print(f"    毛利率 {_describe_series(drivers.gross_margin)}，"
          f"费用率 {_describe_series(drivers.opex_pct_revenue)}")
    print(f"    折旧摊销率 {_describe_series(drivers.da_pct_revenue)}，"
          f"资本开支率 {_describe_series(drivers.capex_pct_revenue)}")
    print(f"    所得税率 {drivers.tax_rate:.2%}，股利支付率 {drivers.dividend_payout_ratio:.2%}")

    print("\n[opening_balance] 基期资产负债表")
    print(f"    资产总计 {opening.total_assets:,.2f}")
    print(f"    负债合计 {opening.total_liabilities:,.2f}")
    print(f"    权益合计 {opening.total_equity:,.2f}")
    print(f"    平衡差额 {opening.balance_gap:,.6f}（应为 0）")

    wacc = load_wacc_params()
    print("\n[wacc] 估值参数")
    print(f"    无风险利率 {wacc.risk_free:.2%}，贝塔 {wacc.beta:.2f}")
    print(f"    风险溢价 {wacc.equity_risk_premium:.2%}，"
          f"税前债务成本 {wacc.cost_of_debt:.2%}")
    print(f"    税率 {drivers.tax_rate:.2%}（来自 [drivers]，与预测共用同一个值）")
    print("    股权/债务权重不在这里 —— 它们由市值和债务推导")

    target, peers = load_market_snapshot()
    print(f"\n[market.csv] 市场快照，基准日 {target.as_of}")
    print(f"    目标公司  {target.code} {target.name}  "
          f"股价 {target.price:,.2f}  股数 {target.diluted_shares:,.2f}  "
          f"市值 {target.market_cap:,.2f}  净债务 {target.net_debt:,.2f}")
    for peer in peers:
        print(f"    可比公司  {peer.code} {peer.name}  "
              f"股价 {peer.price:,.2f}  股数 {peer.diluted_shares:,.2f}  "
              f"市值 {peer.market_cap:,.2f}  净债务 {peer.net_debt:,.2f}")
    print(f"    利润类指标留空：净利润={target.net_profit_attributable}，"
          f"净资产={target.book_equity_attributable}")

    peer_financials = load_peer_financials()
    print("\n[peers.csv] 可比公司财务数据")
    for code, data in peer_financials.items():
        print(
            f"    {code}  收入 {data.revenue:,.2f}  "
            f"归母净利润 {data.net_profit_attributable:,.2f}  "
            f"归母净资产 {data.book_equity_attributable:,.2f}  "
            f"EBITDA {data.ebitda:,.2f}  口径 {data.as_of}"
        )
    # 对不上的公司要在这里就发现，而不是等跑到相对估值阶段才发现倍数一片空白
    missing = [peer.code for peer in peers if peer.code not in peer_financials]
    if missing:
        print(f"    ⚠️ 以下可比公司在 peers.csv 里没有财务数据，倍数会算不出来：{missing}")

    # 数组长度与预测期年数必须一致，这里顺带验一下
    assert len(as_series(drivers.revenue_growth)) == drivers.years, (
        "收入增速的个数与 years 不一致"
    )
    assert abs(opening.balance_gap) < 1e-6, "基期资产负债表不平"
    print("\n自检通过。")


if __name__ == "__main__":
    _demo()
