"""market.csv：模板、校验、读取。

为什么这个文件必须由人从终端导出
    Wind / Choice 的行情数据是有版权的终端数据，不能进公开仓库，
    也没有免费接口能替代（股价、摊薄股数、有息负债、少数股东权益……）。
    所以本模块**只做三件事**：给模板、验格式、读进流水线。
    真正的数字由你在终端里导出来，填进 data/market.csv。

列（与 pipeline.config 的 MARKET_FIELDS 完全一致，改这里必须同步改那边）
    code                    代码（要和 config/targets.csv 的 ticker 对得上）
    name                    简称
    role                    target（被估值公司，正好一行）/ peer（可比公司）
    price                   股价（元）
    diluted_shares          摊薄股数（股）
    interest_bearing_debt   有息负债（元）
    cash                    货币资金（元）
    minority_interest       少数股东权益（元）
    preferred_stock         优先股（元）
    non_operating_assets    非经营性资产（元）
    as_of                   基准日（YYYY-MM-DD），全部行必须同一天

单位一律「元 / 股 / 小数」。从 Wind 导出的常常是「万元」「百万股」——
填表时先换算好，否则算出来的市值会差几个数量级，而且不会报错。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Sequence

from .config import PATHS
from .targets import Target

MARKET_COLUMNS: tuple[str, ...] = (
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
)

FLOAT_COLUMNS = (
    "price",
    "diluted_shares",
    "interest_bearing_debt",
    "cash",
    "minority_interest",
    "preferred_stock",
    "non_operating_assets",
)

ZERO_IF_EMPTY = (
    "interest_bearing_debt",
    "cash",
    "minority_interest",
    "preferred_stock",
    "non_operating_assets",
)

HEADER_COMMENT = """# FinAI 市场快照（估值基准日的市场数据）
#
# 这个文件的数据只能从 Wind / Choice 之类的终端导出：
# 股价、摊薄股数、有息负债、货币资金、少数股东权益、优先股、非经营性资产。
# 利润类指标（收入、净利润、EBITDA）不在这里 —— 那些来自年报抽取结果。
#
# 填表要求：
#   1. role=target 的行正好一行，就是被估值公司；其余行 role=peer。
#   2. 所有行的 as_of 必须是同一天 —— 估值基准日只有一个。
#   3. 金额单位元、股数单位股、比率用小数（不要填 12% 这种写法）。
#   4. 从终端导出常带「万元」「百万股」，请先换算；单位错了不会报错，
#      但市值会差几个数量级，报告里没人看得出来。
#
# 本文件不进 git（终端数据版权受限）。校验：python -m finai_web market --check
"""


class MarketError(ValueError):
    """行情表本身有问题。"""


@dataclass
class MarketReport:
    """校验结果。用报告而不是异常，是因为校验工具要能一次说清所有问题。"""

    path: Path
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)

    @property
    def target(self) -> dict[str, str]:
        return next(
            (row for row in self.rows if (row.get("role") or "").strip().lower() == "target"),
            {},
        )

    @property
    def peers(self) -> list[dict[str, str]]:
        return [row for row in self.rows if (row.get("role") or "").strip().lower() == "peer"]


def read_rows(path: str | Path | None = None) -> list[dict[str, str]]:
    """读数据行，# 开头的行当注释跳过（与 pipeline.config.read_csv 同一约定）。"""
    target = Path(path) if path is not None else PATHS.market_csv
    if not target.is_file():
        raise MarketError(f"行情表不存在：{target}")
    with target.open(newline="", encoding="utf-8-sig") as handle:
        lines = [line for line in handle if not line.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    if reader.fieldnames is None:
        raise MarketError(f"{target} 缺少表头")
    return [dict(row) for row in reader]


def _template_row(code: str, name: str, role: str, basis: str) -> str:
    row = {
        "code": code,
        "name": name,
        "role": role,
        "price": "",
        "diluted_shares": "",
        "interest_bearing_debt": "",
        "cash": "",
        "minority_interest": "0",
        "preferred_stock": "0",
        "non_operating_assets": "0",
        "as_of": basis,
    }
    return ",".join(str(row[column]) for column in MARKET_COLUMNS) + "\n"


def write_template(
    path: str | Path | None = None,
    *,
    targets: Sequence[Target] | None = None,
    as_of: str = "",
    overwrite: bool = True,
) -> Path:
    """生成行情表模板。

    有定标清单时把公司**预填**进去（第一家当目标公司，其余当可比公司）——
    省得手工敲代码和名字还敲错。价格等市场数据留空，等着从终端填。
    """
    target_path = Path(path) if path is not None else PATHS.market_csv
    if target_path.exists() and not overwrite:
        return target_path

    basis = as_of or date.today().isoformat()
    lines = [HEADER_COMMENT, ",".join(MARKET_COLUMNS) + "\n"]
    if targets:
        for index, item in enumerate(targets):
            lines.append(_template_row(item.ticker, item.name, "target" if index == 0 else "peer", basis))
    else:
        lines.append(_template_row("T", "目标公司", "target", basis))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("".join(lines), encoding="utf-8")
    return target_path


def validate(
    path: str | Path | None = None,
    *,
    targets: Sequence[Target] | None = None,
) -> MarketReport:
    """校验行情表。返回报告而不是抛异常 —— 校验工具要能一次说清所有问题。"""
    target = Path(path) if path is not None else PATHS.market_csv
    report = MarketReport(path=target, ok=False)

    if not target.is_file():
        report.errors.append(f"文件不存在：{target}（先跑 `python -m finai_web market --template`）")
        return report

    try:
        rows = read_rows(target)
    except MarketError as err:
        report.errors.append(str(err))
        return report
    report.rows = rows

    if not rows:
        report.errors.append("没有任何数据行（只有注释和表头）")
        return report

    header = list(rows[0].keys())
    missing = [column for column in MARKET_COLUMNS if column not in header]
    extra = [column for column in header if column not in MARKET_COLUMNS]
    if missing:
        report.errors.append(f"表头少了这些列：{missing}")
    if extra:
        report.errors.append(f"表头多了这些列：{extra}（pipeline 会拒绝读这张表）")
    if missing:
        return report

    basis_dates: set[str] = set()
    for index, row in enumerate(rows, start=2):
        role = (row.get("role") or "").strip().lower()
        code = (row.get("code") or "").strip()
        if role not in ("target", "peer"):
            report.errors.append(f"第 {index} 行 role={role!r} 不合法，只能是 target 或 peer")
        if not code:
            report.errors.append(f"第 {index} 行没有 code")
        for column in FLOAT_COLUMNS:
            text = (row.get(column) or "").strip()
            if text == "":
                if column in ZERO_IF_EMPTY:
                    continue
                report.errors.append(f"第 {index} 行 {code or '?'} 的 {column} 是空的（必填）")
                continue
            try:
                value = float(text)
            except ValueError:
                report.errors.append(f"第 {index} 行 {code or '?'} 的 {column} 不是数字：{text!r}")
                continue
            if column in ("price", "diluted_shares") and value <= 0:
                report.errors.append(f"第 {index} 行 {code or '?'} 的 {column}={value} 必须大于 0")
        basis = (row.get("as_of") or "").strip()
        if basis:
            basis_dates.add(basis)
        else:
            report.errors.append(f"第 {index} 行 {code or '?'} 缺 as_of（基准日）")

    targets_rows = [row for row in rows if (row.get("role") or "").strip().lower() == "target"]
    if len(targets_rows) != 1:
        report.errors.append(f"role=target 的行必须正好一行，现在是 {len(targets_rows)} 行")
    if not report.peers:
        report.errors.append("没有任何 role=peer 的行，相对估值做不了")

    if len(basis_dates) > 1:
        report.errors.append(
            f"as_of 不一致：{sorted(basis_dates)} —— 基准日只能有一天，"
            "不同天的价格放在一张表里算出来的倍数是混的"
        )
    if basis_dates:
        report.warnings.append(f"基准日 {sorted(basis_dates)[0]}，请确认这就是你要用的估值基准日")

    if targets:
        tickers = {item.ticker for item in targets}
        codes = {(row.get("code") or "").strip() for row in rows}
        absent = sorted(tickers - codes)
        if absent:
            report.warnings.append(f"定标清单里的这些公司不在行情表里：{absent}（它们不会出现在报告里）")
        strangers = sorted(codes - tickers)
        if strangers:
            report.warnings.append(
                f"行情表里有这些公司不在定标清单里：{strangers}"
                "（若是有意加进来的可比公司，请补进 config/targets.csv 以便抽取年报）"
            )

    report.ok = not report.errors
    return report


def peer_codes(path: str | Path | None = None) -> list[str]:
    """行情表里 role=peer 的代码 —— 抽取层据此知道要给哪些公司抽年报。"""
    return [
        (row.get("code") or "").strip()
        for row in read_rows(path)
        if (row.get("role") or "").strip().lower() == "peer"
    ]
