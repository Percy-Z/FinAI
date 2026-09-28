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

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from algorithm.finai.forecast import BalanceSheetState, ForecastDrivers

# 默认配置路径。用 __file__ 往上退两级定位仓库根目录，
# 这样不管当前工作目录在哪都能找对文件。
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "data" / "assumptions.toml"


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


def load_opening_balance(path: str | Path = DEFAULT_CONFIG_PATH) -> BalanceSheetState:
    """读 [opening_balance] 小节 —— 基期资产负债表，性质是「历史事实」。

    现在从 TOML 读。等抽取层做好之后，这里改成从 financials.json 读，
    函数签名不变，调用方不用改。
    """
    cfg = read_toml(path)
    return _build(
        BalanceSheetState, _section(cfg, "opening_balance", path), path, "opening_balance"
    )


def load_case(
    path: str | Path = DEFAULT_CONFIG_PATH,
) -> tuple[CompanyInfo, ForecastDrivers, BalanceSheetState]:
    """一次读齐三样东西。只是为了调用方便，三个 load_* 仍然是各自独立的。"""
    cfg = read_toml(path)
    company = _build(CompanyInfo, _section(cfg, "company", path), path, "company")
    drivers = _build(ForecastDrivers, _section(cfg, "drivers", path), path, "drivers")
    opening = _build(
        BalanceSheetState, _section(cfg, "opening_balance", path), path, "opening_balance"
    )
    return company, drivers, opening


def _describe_series(values: tuple[float, ...]) -> str:
    """把逐年字段打印成人看得懂的样子。

    单个数字会被 ForecastDrivers 广播成等长的元组，所以这里判断
    「每年都一样」时只显示一个数，避免刷出一串重复值。
    """
    if len(set(values)) == 1:
        return f"{values[0]:.4f}（每年相同）"
    return "(" + ", ".join(f"{v:.4f}" for v in values) + ")"


def _demo() -> None:
    """自检：读默认配置并把结果打印出来。python3 -m pipeline.config"""
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
    print(f"    收入增速 {tuple(round(g, 4) for g in drivers.revenue_growth)}")
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

    # 数组长度与预测期年数必须一致，这里顺带验一下
    assert len(drivers.revenue_growth) == drivers.years, "收入增速的个数与 years 不一致"
    assert abs(opening.balance_gap) < 1e-6, "基期资产负债表不平"
    print("\n自检通过。")


if __name__ == "__main__":
    _demo()
