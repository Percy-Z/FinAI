"""路径、环境变量与运行期配置。

设计约定：
    1. 所有**产出**都落在 `data/` 下（PDF、抽取结果、行情表），
       这些文件要么体积大、要么是终端数据，一律不进 git。
    2. 配置里**只放路径和开关**，不放业务逻辑 —— 想改数据源就改环境变量，
       不必改代码。

数据源开关（`FINAI_DATA_SOURCE`）
    流水线的 `[opening_balance]` 与 `peers.csv` 原先读手填文件。抽取层就绪之后，
    同一个位置可以改读抽取结果：

        config     读 data/assumptions.toml 的 [opening_balance] 与 data/peers.csv（默认）
        extracted  读 data/extracted/pipeline_input.json（由 `finai ingest build` 生成）

    默认必须是 config：仓库里那批单元测试断言的是手填数据的具体数值，
    如果默认行为随「本地有没有抽取结果」而变，测试就会时绿时红。
    所以切换是显式的（环境变量 `FINAI_DATA_SOURCE` 或 `finai run --source extracted`）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 数据源取值。除此之外的值一律按 config 处理并给出提示，
# 不做「猜一个最接近的」这种事 —— 数据源搞错会静默算错数。
SOURCE_CONFIG = "config"
SOURCE_EXTRACTED = "extracted"
DATA_SOURCE_ENV = "FINAI_DATA_SOURCE"


def _load_dotenv(path: Path) -> None:
    """极简 .env 读取：不覆盖已存在的环境变量。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else default


@dataclass(frozen=True)
class Paths:
    root: Path = ROOT
    config_dir: Path = ROOT / "config"
    data: Path = _env_path("FINAI_DATA_DIR", ROOT / "data")
    raw_pdf: Path = _env_path("FINAI_RAW_PDF_DIR", ROOT / "data" / "raw_pdf")
    extracted: Path = _env_path("FINAI_EXTRACTED_DIR", ROOT / "data" / "extracted")
    cache: Path = ROOT / "data" / "cache"
    market_csv: Path = _env_path("FINAI_MARKET_CSV", ROOT / "data" / "market.csv")
    peers_csv: Path = ROOT / "data" / "peers.csv"
    # 抽取结果生成的 peers 表。**刻意不直接写 data/peers.csv**：
    # 那份文件是仓库里被 git 跟踪的手填数据，覆盖它会制造一个没人打算提交的改动，
    # 而且一旦覆盖，`FINAI_DATA_SOURCE=config` 就不再有「回退到手填」这条路。
    peers_extracted: Path = ROOT / "data" / "extracted" / "peers.csv"
    targets_csv: Path = _env_path("FINAI_TARGETS_CSV", ROOT / "config" / "targets.csv")
    targets_example: Path = ROOT / "config" / "targets.example.csv"
    manifest_json: Path = ROOT / "data" / "manifest.json"
    # 抽取结果 → 流水线输入的汇总文件。pipeline.config 认这一个路径，
    # 而不是去 data/extracted/ 里逐个读文件 —— 一个入口，出错时好排查。
    pipeline_input: Path = ROOT / "data" / "extracted" / "pipeline_input.json"

    def ensure(self) -> "Paths":
        for directory in (self.data, self.raw_pdf, self.extracted, self.cache):
            directory.mkdir(parents=True, exist_ok=True)
        return self


PATHS = Paths()


def data_source(explicit: str | None = None) -> str:
    """决定这次跑用哪一路数据源，返回 `config` 或 `extracted`。

    优先级：显式传参 > 环境变量 > 默认 config。
    想用抽取结果就 `FINAI_DATA_SOURCE=extracted`（或在 `finai run --source extracted`）。
    """
    raw = (explicit if explicit is not None else os.environ.get(DATA_SOURCE_ENV, "")).strip()
    if not raw:
        return SOURCE_CONFIG
    if raw == SOURCE_EXTRACTED:
        return SOURCE_EXTRACTED
    return SOURCE_CONFIG


def has_pipeline_input(path: Path | None = None) -> bool:
    """抽取结果是流水线输入时，这个文件必须在。

    单独抽成一个函数，是为了让调用方能在「用户以为在用抽取结果、其实文件不在」
    的时候立刻报错，而不是悄悄退回手填数据继续算。
    """
    return (path or PATHS.pipeline_input).is_file()


def firecrawl_api_key() -> str | None:
    key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    return key or None


def http_timeout() -> float:
    return float(os.environ.get("FINAI_HTTP_TIMEOUT", "30"))


def http_retries() -> int:
    return int(os.environ.get("FINAI_HTTP_RETRIES", "3"))
