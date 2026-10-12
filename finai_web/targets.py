"""定标公司清单：读 `config/targets.csv`。

这是整条取数链路的入口 —— 「定标的公司」写在这里，后面所有环节都靠它。

列
----
    ticker            代码。A 股就写 600519、000858 这种六位代码
    name              简称（用于搜索公告时兜底，也用于报告里核对）
    market            CN / HK / BJ …；决定去哪个板块查公告
    org_id            巨潮的内部机构号，可留空。留空时用代码搜索，多一次匹配
    years             要抽哪几年，分号隔开：2023;2024
    annual_report_url 已知 PDF 直链时可填，填了就跳过检索直接用（能省一次抓取）

为什么单独一个文件而不是写进 assumptions.toml
    假设文件是「这家公司怎么估值」，清单是「这次要处理哪些公司」。
    一次定标、多次复用，两者生命周期不同，混在一起会互相污染。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .config import PATHS

COLUMNS = ("ticker", "name", "market", "org_id", "years", "annual_report_url")

# market → 巨潮公告接口的 column 参数。查错板块会得到空结果，
# 所以这里给出默认映射，并允许在 CSV 里用 column 列覆盖。
MARKET_COLUMNS = {
    "CN": "",  # 按代码前缀判断沪深
    "SH": "sse",
    "SZ": "szse",
    "BJ": "bj",
    "HK": "hke",
}


class TargetError(ValueError):
    """清单文件本身有问题。"""


@dataclass(frozen=True)
class Target:
    ticker: str
    name: str = ""
    market: str = "CN"
    org_id: str = ""
    years: tuple[int, ...] = field(default_factory=tuple)
    annual_report_url: str = ""
    column: str = ""

    @property
    def cninfo_column(self) -> str:
        """巨潮接口要的板块值。A 股按代码前缀分流：6 开头是沪市，其余是深市。"""
        if self.column:
            return self.column
        market = self.market.upper()
        if market == "CN":
            return "sse" if self.ticker.startswith("6") else "szse"
        return MARKET_COLUMNS.get(market, "")

    @property
    def years_desc(self) -> tuple[int, ...]:
        """从新到旧 —— 下载与抽取都先做最近一年，出问题时先拿到最有用的那份。"""
        return tuple(sorted(self.years, reverse=True))

    def search_key(self) -> str:
        return f"{self.ticker},{self.org_id}" if self.org_id else self.ticker

    def to_dict(self) -> dict[str, str]:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "market": self.market,
            "org_id": self.org_id,
            "years": ";".join(str(year) for year in self.years),
            "annual_report_url": self.annual_report_url,
        }


def parse_years(raw: str) -> tuple[int, ...]:
    """`2023;2024` / `2023,2024` / `2024` 都认。"""
    years: list[int] = []
    for chunk in str(raw or "").replace(",", ";").split(";"):
        text = chunk.strip()
        if not text:
            continue
        try:
            year = int(text)
        except ValueError:
            raise TargetError(f"years 里有不是年份的内容：{text!r}") from None
        if not 1990 <= year <= 2100:
            raise TargetError(f"years 里的年份看着不对：{year}")
        if year not in years:
            years.append(year)
    return tuple(years)


def read_targets(path: str | Path | None = None, *, required: bool = True) -> list[Target]:
    """读清单。文件不存在时的处理由 required 决定：

    CLI 里 required=True（没有清单就该停下，而不是跑出一条空流水线）；
    测试与自检里可以 required=False，拿到空列表继续。
    """
    target_path = Path(path) if path is not None else PATHS.targets_csv
    if not target_path.is_file():
        if required:
            raise TargetError(
                f"定标公司清单不存在：{target_path}\n"
                f"    复制一份模板开始填：copy config\\targets.example.csv config\\targets.csv\n"
                f"    或者用 `python -m finai_web targets --init` 生成。"
            )
        return []

    rows = _read_csv(target_path)
    if not rows:
        raise TargetError(f"{target_path} 里没有任何数据行")

    header = set(rows[0].keys())
    unknown = header - set(COLUMNS) - {"column"}
    if unknown:
        raise TargetError(
            f"{target_path} 里有无法识别的列：{sorted(unknown)}\n"
            f"    可用列：{list(COLUMNS)}（外加可选的 column，用于覆盖巨潮板块）"
        )

    targets: list[Target] = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        ticker = (row.get("ticker") or "").strip()
        if not ticker:
            raise TargetError(f"{target_path} 第 {row_number} 行缺少 ticker")
        if ticker in seen:
            raise TargetError(f"{target_path} 第 {row_number} 行的 ticker={ticker} 重复")
        seen.add(ticker)
        targets.append(
            Target(
                ticker=ticker,
                name=(row.get("name") or "").strip(),
                market=(row.get("market") or "CN").strip() or "CN",
                org_id=(row.get("org_id") or "").strip(),
                years=parse_years(row.get("years") or ""),
                annual_report_url=(row.get("annual_report_url") or "").strip(),
                column=(row.get("column") or "").strip(),
            )
        )
    return targets


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        # 与 pipeline.config.read_csv 同一约定：以 # 开头的行是注释
        lines = [line for line in handle if not line.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    if reader.fieldnames is None:
        raise TargetError(f"{path} 缺少表头")
    return [row for row in reader]


EXAMPLE = """ticker,name,market,org_id,years,annual_report_url
# 代码用交易所的六位数字；years 可以写多份年报，分号隔开
# org_id 可留空；填上能少一次模糊匹配（巨潮机构号，形如 gssh0600519）
# annual_report_url 填了就跳过检索，直接用这个直链
600519,贵州茅台,CN,,2023;2024,
000858,五粮液,CN,,2023;2024,
300750,宁德时代,CN,,2024,
"""


def write_example(path: str | Path | None = None, *, overwrite: bool = False) -> Path:
    """写一份清单模板。默认不覆盖已存在的文件 —— 覆盖会抹掉人填的定标公司。"""
    target = Path(path) if path is not None else PATHS.targets_example
    if target.exists() and not overwrite:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EXAMPLE, encoding="utf-8")
    return target
