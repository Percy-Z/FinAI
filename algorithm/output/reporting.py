"""输出层：把算法结果落成文件。

设计约定
    每次运行产出一个带时间戳的目录 outputs/<run_id>/，不覆盖上一次。
    两次运行结果不一样时，能立刻判断是输入变了、代码变了还是假设变了。

    同时写一份 run_manifest.json，记录每个输出文件的 sha256、字节数，
    以及调用方传入的输入说明。评委要复现时，这份清单就是凭证。

产出的文件
    编号是固定的，方便在目录里按顺序读，也方便报告里直接引用某个文件：

    01_projections.json       三表联动预测
    02_validation_report.json 勾稽校验报告（可能含多年）
    03_valuation.json         DCF 估值结果
    04_tornado.csv            单因素敏感性排序
    05_sensitivity.csv        双因素敏感性网格
    06_reverse_dcf.json       反向求解出的市场隐含假设
    07_comps.csv              每家公司一行的倍数表
    08_comps_implied.csv      每种倍数的隐含每股价值
    09_football_field.json    估值区间的数据
    10_run_manifest.json      本次运行的清单与哈希
    charts/11 到 16 是图表，交给 charts 子包渲染

模块本身不依赖 matplotlib：图表由 charts 子包处理，缺库时只是不生成图。
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from algorithm.finai.comps import (
    CompsResult,
    FootballField,
    write_comps_csv,
    write_implied_csv,
)
from algorithm.finai.forecast import ThreeStatementForecast
from algorithm.finai.reverse_dcf import ReverseResult
from algorithm.finai.sensitivity import (
    GridResult,
    TornadoRow,
    write_grid_csv,
    write_tornado_csv,
)
from algorithm.finai.types import DCFResult
from algorithm.finai.validate import ValidationReport

from . import charts

# 产物目录是 algorithm/outputs/，与本包名 output 只差一个 s：output 是代码，outputs 是产物
DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parents[1] / "outputs"

# 数据文件的编号与文件名。编号固定不随运行变化，报告里可以按编号引用。
DATA_FILES: dict[str, tuple[str, str]] = {
    "projections": ("01", "projections.json"),
    "validation_report": ("02", "validation_report.json"),
    "valuation": ("03", "valuation.json"),
    "tornado": ("04", "tornado.csv"),
    "sensitivity": ("05", "sensitivity.csv"),
    "reverse_dcf": ("06", "reverse_dcf.json"),
    "comps": ("07", "comps.csv"),
    "comps_implied": ("08", "comps_implied.csv"),
    "football_field": ("09", "football_field.json"),
    "run_manifest": ("10", "run_manifest.json"),
}


def data_filename(name: str) -> str:
    """返回带编号的数据文件名，例如 valuation -> 03_valuation.json。"""
    if name not in DATA_FILES:
        raise KeyError(f"未知的数据文件 {name!r}，可选：{sorted(DATA_FILES)}")
    number, base = DATA_FILES[name]
    return f"{number}_{base}"


@dataclass
class RunOutputs:
    """一次运行的产出。哪些字段是 None，对应文件就不会生成。"""

    dcf: DCFResult | None = None
    football_field: FootballField | None = None
    comps: CompsResult | None = None
    tornado: Sequence[TornadoRow] = field(default_factory=tuple)
    grid: GridResult | None = None
    reverse: Sequence[ReverseResult] = field(default_factory=tuple)
    validation: Sequence[ValidationReport] = field(default_factory=tuple)
    forecast: ThreeStatementForecast | None = None


@dataclass
class RunArtifacts:
    """一次运行写出的文件。"""

    run_id: str
    directory: Path
    files: dict[str, Path] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)

    def path_of(self, name: str) -> Path:
        if name not in self.files:
            raise KeyError(f"本次运行没有产出 {name}")
        return self.files[name]


def make_run_id(when: datetime | None = None, prefix: str = "") -> str:
    """用时间戳生成运行编号，精确到秒。"""
    moment = when or datetime.now()
    stamp = moment.strftime("%Y%m%dT%H%M%S")
    return f"{prefix}{stamp}" if prefix else stamp


def write_json(path: str | Path, payload: Any) -> Path:
    """写 JSON，中文不转义，缩进两格。

    写之前会把 NaN 和无穷大换成 null：标准 JSON 不允许这两个值，
    直接写出去会让 JavaScript 的 JSON.parse 之类的解析器直接报错。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(_json_safe(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target


def _json_safe(value: Any) -> Any:
    """递归把非有限浮点数换成 None，其余原样返回。"""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def validate_run_id(run_id: str) -> str:
    """运行编号只能是一段普通名字，不能带路径分隔符。

    否则传 "../x" 就会把文件写到 outputs 之外。
    """
    if not run_id:
        raise ValueError("run_id 不能为空")
    if run_id in (".", "..") or "/" in run_id or "\\" in run_id:
        raise ValueError(f"run_id 不能包含路径分隔符，收到 {run_id!r}")
    return run_id


def sha256_of(path: str | Path) -> str:
    """算文件的 sha256，用于运行清单。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_directory(root: Path, run_id: str, overwrite: bool) -> Path:
    validate_run_id(run_id)
    directory = root / run_id
    if directory.exists() and any(directory.iterdir()) and not overwrite:
        raise FileExistsError(
            f"{directory} 已经存在且有内容。每次运行应该用新的 run_id，"
            "确实要覆盖就传 overwrite=True"
        )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def export_run(
    outputs: RunOutputs,
    root: str | Path | None = None,
    run_id: str | None = None,
    meta: dict[str, Any] | None = None,
    overwrite: bool = False,
    render_charts: bool = True,
) -> RunArtifacts:
    """把一次运行的全部结果写进 outputs/<run_id>/。"""
    root_path = Path(root) if root is not None else DEFAULT_OUTPUT_ROOT
    identifier = validate_run_id(run_id) if run_id else make_run_id()
    directory = _prepare_directory(root_path, identifier, overwrite)

    artifacts = RunArtifacts(run_id=identifier, directory=directory)
    warnings: list[str] = []

    def record(name: str, path: Path) -> None:
        artifacts.files[name] = path

    def target(name: str) -> Path:
        return directory / data_filename(name)

    if outputs.dcf is not None:
        record("valuation", write_json(target("valuation"), outputs.dcf.to_dict()))

    if outputs.football_field is not None:
        record(
            "football_field",
            write_json(target("football_field"), outputs.football_field.to_dict()),
        )

    if outputs.comps is not None:
        record("comps", write_comps_csv(target("comps"), outputs.comps))
        record(
            "comps_implied",
            write_implied_csv(target("comps_implied"), outputs.comps),
        )

    if outputs.tornado:
        record("tornado", write_tornado_csv(target("tornado"), outputs.tornado))

    if outputs.grid is not None:
        record("sensitivity", write_grid_csv(target("sensitivity"), outputs.grid))

    if outputs.reverse:
        record(
            "reverse_dcf",
            write_json(
                target("reverse_dcf"),
                {"results": [item.to_dict() for item in outputs.reverse]},
            ),
        )

    if outputs.validation:
        reports = [report.to_dict() for report in outputs.validation]
        record(
            "validation_report",
            write_json(
                target("validation_report"),
                {"count": len(reports), "reports": reports},
            ),
        )

    if outputs.forecast is not None:
        record(
            "projections",
            write_json(
                target("projections"),
                {
                    "source": outputs.forecast.source,
                    "years": outputs.forecast.to_records(),
                    "worst_balance_gap": outputs.forecast.worst_balance_gap,
                },
            ),
        )

    if render_charts:
        if charts.HAS_MATPLOTLIB:
            for chart_name, chart_path in charts.render_all(
                outputs, directory / "charts"
            ).items():
                # 加前缀，避免和同名的数据文件（例如 football_field.json）撞键
                record(f"chart_{chart_name}", chart_path)
        else:
            warnings.append("没有安装 matplotlib，跳过全部图表的渲染")

    manifest = _build_manifest(
        run_id=identifier,
        directory=directory,
        files=artifacts.files,
        meta=meta or {},
        warnings=warnings,
    )
    manifest_path = write_json(target("run_manifest"), manifest)
    artifacts.files["run_manifest"] = manifest_path
    artifacts.manifest = manifest
    return artifacts


def _build_manifest(
    run_id: str,
    directory: Path,
    files: dict[str, Path],
    meta: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    """生成运行清单。

    清单本身不列在里面：它是在写完所有产物之后才生成的，没法包含自己的哈希。
    所以 outputs 列出的永远是「除了 run_manifest.json 以外的全部产物」。
    """
    entries = []
    for name, path in sorted(files.items()):
        entries.append(
            {
                "name": name,
                # 用相对路径，图表在 charts/ 子目录里也能被定位
                "file": str(path.relative_to(directory)),
                "bytes": path.stat().st_size,
                "sha256": sha256_of(path),
            }
        )
    return {
        "run_id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "inputs": meta,
        "outputs": entries,
        "warnings": warnings,
    }


def summarize(artifacts: RunArtifacts) -> str:
    """把本次运行写出的文件列成一段文本，终端里打印用。"""
    lines = [f"运行编号 {artifacts.run_id}", f"输出目录 {artifacts.directory}"]
    for name, path in sorted(artifacts.files.items()):
        size = path.stat().st_size
        lines.append(f"  {name:<20} {path.name:<24} {size:>8,} 字节")
    warnings = artifacts.manifest.get("warnings", [])
    for warning in warnings:
        lines.append(f"  提示：{warning}")
    return "\n".join(lines)
