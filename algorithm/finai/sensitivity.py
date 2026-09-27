"""敏感性分析：龙卷风图、双因素网格、区间归因。

三种做法回答三个不同的问题，别混：
    龙卷风图    一次只动一个参数，其余不动，按估值变化幅度排序
                -> 回答「哪个假设最要紧，精力该花在哪」
    双因素网格  两个参数同时变，输出矩阵
                -> 回答「这两个假设的组合下，估值落在哪」
    区间归因    每个参数的估值变化幅度占区间总宽度的比例
                -> 回答「这个估值区间是被谁撑开的」

实现方式：把假设用 dataclasses.replace 换成新对象，再调一次估值函数。
估值引擎是纯函数，所以这里不需要任何特殊支持。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence

from .dcf import value_dcf
from .types import ForecastAssumptions, MarketData

# 可以按年设置、也可以整体广播的字段
VECTOR_FIELDS = frozenset({"revenue_growth", "ebit_margin"})


@dataclass(frozen=True)
class Variable:
    """一个待考察的参数。

    field 可以是 "wacc"，也可以是 ForecastAssumptions 上的任意字段名。
    对于 revenue_growth 和 ebit_margin 这两个逐年字段，取单个值时会广播到每一年。
    """

    name: str
    field: str
    low: float
    base: float
    high: float

    def __post_init__(self) -> None:
        if not self.low <= self.base <= self.high:
            raise ValueError(
                f"{self.name}：要求 low <= base <= high，"
                f"收到 low={self.low}, base={self.base}, high={self.high}"
            )

    @classmethod
    def bounds(
        cls, name: str, field: str, low: float, high: float, base: float | None = None
    ) -> "Variable":
        """直接给出上下界。base 省略时取中点。"""
        midpoint = (low + high) / 2
        return cls(
            name=name,
            field=field,
            low=low,
            base=midpoint if base is None else base,
            high=high,
        )

    @classmethod
    def absolute(cls, name: str, field: str, base: float, delta: float) -> "Variable":
        """在基准值上下各浮动一个绝对量。"""
        return cls(
            name=name, field=field, low=base - delta, base=base, high=base + delta
        )

    @classmethod
    def relative(cls, name: str, field: str, base: float, percent: float) -> "Variable":
        """在基准值上下各浮动一个百分比。"""
        delta = abs(base) * percent
        return cls(
            name=name, field=field, low=base - delta, base=base, high=base + delta
        )


@dataclass
class TornadoRow:
    """龙卷风图的一行。"""

    name: str
    field: str
    low: float
    base: float
    high: float
    per_share_at_low: float
    per_share_at_high: float
    per_share_base: float

    @property
    def swing(self) -> float:
        """估值变化幅度，用于排序和归因。"""
        return abs(self.per_share_at_high - self.per_share_at_low)


@dataclass
class AttributionRow:
    """区间归因的一行。"""

    name: str
    swing: float
    share: float


@dataclass
class GridResult:
    """双因素网格。cells[i][j] 对应 row_values[i] 与 col_values[j]。"""

    row_name: str
    col_name: str
    row_field: str
    col_field: str
    row_values: list[float]
    col_values: list[float]
    cells: list[list[float]]
    price: float

    def cell(self, row_value: float, col_value: float) -> float:
        if row_value not in self.row_values:
            raise KeyError(f"行参数没有取值 {row_value}，可选：{self.row_values}")
        if col_value not in self.col_values:
            raise KeyError(f"列参数没有取值 {col_value}，可选：{self.col_values}")
        i = self.row_values.index(row_value)
        j = self.col_values.index(col_value)
        return self.cells[i][j]

    @property
    def value_range(self) -> tuple[float, float]:
        flat = [value for row in self.cells for value in row]
        return min(flat), max(flat)


def apply_variable(
    assumptions: ForecastAssumptions,
    wacc: float,
    variable: Variable,
    value: float,
) -> tuple[ForecastAssumptions, float]:
    """把某个参数设成指定值，返回新的 (假设, WACC)。

    不修改传入的对象，假设类是不可变的。
    """
    if variable.field == "wacc":
        return assumptions, float(value)

    if (
        variable.field == "terminal_growth"
        and assumptions.terminal_method != "perpetuity"
    ):
        raise ValueError(
            "当前终值方法不是永续增长法，调整 terminal_growth 不会影响结果"
        )
    if (
        variable.field == "exit_multiple"
        and assumptions.terminal_method != "exit_multiple"
    ):
        raise ValueError(
            "当前终值方法不是退出倍数法，调整 exit_multiple 不会影响结果"
        )

    if variable.field in VECTOR_FIELDS:
        broadcast = tuple([float(value)] * assumptions.years)
        return replace(assumptions, **{variable.field: broadcast}), wacc

    if not hasattr(assumptions, variable.field):
        raise ValueError(f"ForecastAssumptions 上没有字段 {variable.field!r}")

    return replace(assumptions, **{variable.field: float(value)}), wacc


def _per_share_at(
    assumptions: ForecastAssumptions,
    market: MarketData,
    wacc: float,
    variable: Variable,
    value: float,
) -> float:
    """把参数设为 value 后算一次每股价值。"""
    modified, modified_wacc = apply_variable(assumptions, wacc, variable, value)
    return value_dcf(modified, market, modified_wacc).per_share


def tornado(
    assumptions: ForecastAssumptions,
    market: MarketData,
    wacc: float,
    variables: Iterable[Variable],
) -> list[TornadoRow]:
    """单因素敏感性，按估值变化幅度从大到小排序。"""
    base_per_share = value_dcf(assumptions, market, wacc).per_share

    rows: list[TornadoRow] = []
    for variable in variables:
        rows.append(
            TornadoRow(
                name=variable.name,
                field=variable.field,
                low=variable.low,
                base=variable.base,
                high=variable.high,
                per_share_at_low=_per_share_at(
                    assumptions, market, wacc, variable, variable.low
                ),
                per_share_at_high=_per_share_at(
                    assumptions, market, wacc, variable, variable.high
                ),
                per_share_base=base_per_share,
            )
        )

    rows.sort(key=lambda row: row.swing, reverse=True)
    return rows


def attribute(rows: Sequence[TornadoRow]) -> list[AttributionRow]:
    """把龙卷风图转成区间归因：每个参数贡献了估值区间宽度的多少。"""
    total = sum(row.swing for row in rows)
    if total <= 0:
        return [AttributionRow(row.name, row.swing, float("nan")) for row in rows]
    return [AttributionRow(row.name, row.swing, row.swing / total) for row in rows]


def two_way_grid(
    assumptions: ForecastAssumptions,
    market: MarketData,
    wacc: float,
    row_variable: Variable,
    col_variable: Variable,
    points: int = 5,
    row_values: Sequence[float] | None = None,
    col_values: Sequence[float] | None = None,
) -> GridResult:
    """双因素网格。默认在各自上下界之间等距取 5 个点。"""
    if row_variable.field == col_variable.field:
        raise ValueError("行与列不能是同一个参数，否则网格没有意义")

    rows = (
        list(row_values)
        if row_values
        else _linspace(row_variable.low, row_variable.high, points)
    )
    cols = (
        list(col_values)
        if col_values
        else _linspace(col_variable.low, col_variable.high, points)
    )

    cells: list[list[float]] = []
    for row_value in rows:
        line: list[float] = []
        for col_value in cols:
            modified, modified_wacc = apply_variable(
                assumptions, wacc, row_variable, row_value
            )
            modified, modified_wacc = apply_variable(
                modified, modified_wacc, col_variable, col_value
            )
            line.append(value_dcf(modified, market, modified_wacc).per_share)
        cells.append(line)

    return GridResult(
        row_name=row_variable.name,
        col_name=col_variable.name,
        row_field=row_variable.field,
        col_field=col_variable.field,
        row_values=rows,
        col_values=cols,
        cells=cells,
        price=market.price,
    )


def _linspace(start: float, stop: float, points: int) -> list[float]:
    """等距取点，包含两端。不依赖 numpy。"""
    if points < 2:
        raise ValueError("points 至少为 2")
    step = (stop - start) / (points - 1)
    return [start + step * i for i in range(points)]


def write_tornado_csv(path: str | Path, rows: Sequence[TornadoRow]) -> Path:
    """写出 tornado.csv，用 utf-8-sig 编码以便 Excel 直接打开中文。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "变量",
                "字段",
                "下界",
                "基准",
                "上界",
                "下界每股价值",
                "上界每股价值",
                "波动幅度",
                "基准每股价值",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.name,
                    row.field,
                    _fmt(row.low),
                    _fmt(row.base),
                    _fmt(row.high),
                    _fmt(row.per_share_at_low),
                    _fmt(row.per_share_at_high),
                    _fmt(row.swing),
                    _fmt(row.per_share_base),
                ]
            )
    return target


def write_grid_csv(path: str | Path, grid: GridResult) -> Path:
    """写出 sensitivity.csv，第一行第一格标出两个维度。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [f"{grid.row_name} \\ {grid.col_name}"]
            + [_fmt(value) for value in grid.col_values]
        )
        for row_value, line in zip(grid.row_values, grid.cells):
            writer.writerow([_fmt(row_value)] + [_fmt(value) for value in line])
    return target


def _fmt(value: float) -> str:
    """数值格式化：比率保留 4 位小数，其它保留 2 位。"""
    if abs(value) < 1.0:
        return f"{value:.4f}"
    return f"{value:.2f}"
