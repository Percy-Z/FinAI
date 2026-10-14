"""一家公司的数据集清单：`company.json` 与它生成的取数清单 `targets.csv`。

为什么要有这一层
    取数链路的输入一直是 `targets.csv`（人填的 CSV），它回答得了「下哪些公司的哪几年
    年报」，但装不下「这家公司是怎么被找出来的」（用户输入的名字、检索时间、候选里
    为什么选了它）。工作台里输入公司名自动取数之后，这些溯源信息必须留着 ——
    评审问「你怎么知道是这家」时要答得上。

    所以分两个文件，职责不同：
        company.json   人/程序选定的结果 + 溯源（谁、什么时候、从哪个关键词选的）
        targets.csv    **生成物**，只给取数层读（列与 targets.py 的 COLUMNS 一致）

    一份数据一个目录（`data/companies/<代码>/`），两者都放在里面。CSV 是生成物，
    手改它会在下次生成时被覆盖 —— 文件头里写明了这一点。
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .resolve import Candidate
from .targets import COLUMNS, Target

VERSION = 1
FILENAME = "company.json"
TARGETS_NAME = "targets.csv"


class CompanyError(ValueError):
    """清单文件本身有问题（缺目标公司、年份没写、JSON 坏了）。"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def default_years(latest_disclosure_year: int, count: int = 2) -> tuple[int, ...]:
    """默认要下哪几年（都是**披露年份**）。

    目标公司默认两份：最近那份给基期资产负债表，上一份给利润表与费用明细 ——
    「盈利能力归一到上一个正常年份」这个口径要靠上一年的数才做得出来
    （长春高新的毛利率 85.72%、费用率 57.38% 就是从 2024 年反推的）。
    可比公司一份就够：只用到利润类指标。
    """
    if count < 1:
        raise CompanyError("年报份数至少是 1")
    latest = int(latest_disclosure_year)
    return tuple(latest - offset for offset in range(count))


@dataclass(frozen=True)
class CompanyRef:
    """清单里的一家公司。字段与 targets.csv 的列一一对应，外加一个 column 覆盖项。"""

    code: str
    name: str = ""
    org_id: str = ""
    market: str = "CN"
    column: str = ""
    years: tuple[int, ...] = ()
    annual_report_url: str = ""

    @classmethod
    def from_candidate(
        cls, candidate: Candidate, *, years: Sequence[int] = ()
    ) -> "CompanyRef":
        """按名称检索到的候选直接变成清单项。板块按代码前缀推（不用接口的 type）。"""
        return cls(
            code=candidate.code,
            name=candidate.name,
            org_id=candidate.org_id,
            market=candidate.market,
            years=tuple(int(year) for year in years),
        )

    @property
    def cninfo_column(self) -> str:
        """巨潮公告接口的板块。`column` 手填时优先，否则按代码前缀推。"""
        if self.column:
            return self.column
        return Target(ticker=self.code, market=self.market).cninfo_column

    def to_target(self) -> Target:
        return Target(
            ticker=self.code,
            name=self.name,
            market=self.market,
            org_id=self.org_id,
            years=self.years,
            annual_report_url=self.annual_report_url,
            column=self.column,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "org_id": self.org_id,
            "market": self.market,
            "column": self.column,
            "years": list(self.years),
            "annual_report_url": self.annual_report_url,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CompanyRef":
        if not isinstance(raw, Mapping):
            raise CompanyError(f"清单里的一项不是对象：{raw!r}")
        code = str(raw.get("code") or raw.get("ticker") or "").strip()
        if not code:
            raise CompanyError(f"清单里有公司没写 code：{raw!r}")
        years: list[int] = []
        for value in raw.get("years") or ():
            try:
                years.append(int(value))
            except (TypeError, ValueError):
                raise CompanyError(f"{code} 的 years 里有不是年份的内容：{value!r}") from None
        return cls(
            code=code,
            name=str(raw.get("name") or "").strip(),
            org_id=str(raw.get("org_id") or "").strip(),
            market=str(raw.get("market") or "CN").strip() or "CN",
            column=str(raw.get("column") or "").strip(),
            years=tuple(years),
            annual_report_url=str(raw.get("annual_report_url") or "").strip(),
        )


@dataclass
class CompanyProfile:
    """一家公司的完整清单：被估值的那家 + 可比公司 → 见 `README.md` 的目录约定。"""

    target: CompanyRef
    peers: list[CompanyRef] = field(default_factory=list)
    as_of: str = ""
    resolved_from: str = ""
    resolved_at: str = ""
    version: int = VERSION
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.target.code:
            raise CompanyError("清单里没有目标公司")
        if not self.target.years:
            raise CompanyError(
                f"目标公司 {self.target.code} 没写 years —— 不知道要下哪几年年报"
            )
        if any(peer.code == self.target.code for peer in self.peers):
            raise CompanyError("可比公司里出现了目标公司自己")

    @property
    def all_refs(self) -> list[CompanyRef]:
        return [self.target, *self.peers]

    @property
    def peer_codes(self) -> list[str]:
        return [peer.code for peer in self.peers]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "resolved_from": self.resolved_from,
            "resolved_at": self.resolved_at,
            "as_of": self.as_of,
            "target": self.target.to_dict(),
            "peers": [peer.to_dict() for peer in self.peers],
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CompanyProfile":
        if not isinstance(raw, Mapping):
            raise CompanyError(f"清单不是对象：{raw!r}")
        target_raw = raw.get("target")
        if not isinstance(target_raw, Mapping):
            raise CompanyError("清单里没有 target 小节")
        peers_raw = raw.get("peers") or []
        if not isinstance(peers_raw, Sequence) or isinstance(peers_raw, (str, bytes)):
            raise CompanyError("清单的 peers 应该是一个数组")
        return cls(
            target=CompanyRef.from_dict(target_raw),
            peers=[CompanyRef.from_dict(item) for item in peers_raw],
            as_of=str(raw.get("as_of") or "").strip(),
            resolved_from=str(raw.get("resolved_from") or "").strip(),
            resolved_at=str(raw.get("resolved_at") or "").strip(),
            version=int(raw.get("version") or VERSION),
            notes=[str(item) for item in (raw.get("notes") or [])],
        )

    # ── 落盘 ────────────────────────────────────────────────────────

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return target

    @classmethod
    def load(cls, path: str | Path) -> "CompanyProfile":
        source = Path(path)
        if not source.is_file():
            raise CompanyError(f"清单不存在：{source}")
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            raise CompanyError(f"{source} 不是合法 JSON：{err}") from err
        return cls.from_dict(raw)

    def write_targets_csv(self, path: str | Path) -> Path:
        """生成取数层要读的 targets.csv。

        列与 `targets.py` 的 COLUMNS 逐字一致 —— 那边读这份 CSV 用的是同一套解析，
        所以生成出来的东西不需要任何特殊处理。CSV 里 `years` 用分号分隔，
        与手填文件的写法一致。
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        buffer = io.StringIO()
        buffer.write(
            "# 本文件由 company.json 生成，不要手改 —— 改 company.json 或在工作台里重选。\n"
            "# 生成它的命令：python -m finai_web company --sync <公司目录>\n"
            "# years 是**披露年份**（2025 年年报在 2026 年披露，写 2026）。\n"
        )
        writer = csv.DictWriter(buffer, fieldnames=list(COLUMNS), lineterminator="\n")
        writer.writeheader()
        for ref in self.all_refs:
            row = ref.to_target().to_dict()
            writer.writerow({column: row.get(column, "") for column in COLUMNS})
        target.write_text(buffer.getvalue(), encoding="utf-8")
        return target
