"""Web 界面：一个能在浏览器里跑估值流水线的工作台。

为什么做它
    通知说「可运行的系统原型（Web 界面或命令行均可）」，命令行已经满足要求。
    但命令行有三个实际问题：演示时不好看、评审现场投影不体面、
    而且**运行过程是黑盒** —— 两分钟里屏幕上只有滚动的日志。

    所以这个界面解决的是这三件事，不是「加一个花哨外壳」：
        1. 一键运行：点一下按钮，不用记命令
        2. 过程可见：九个阶段逐个亮起来，评审能看见系统在工作
        3. 结果可点：跑完直接看指标、图、产物清单，点开就是完整报告

技术选择：**标准库 http.server，不加任何依赖**
    用 FastAPI 之类能写得更舒服，但那会破坏本项目「核心零依赖、
    评委能在干净机器上复现」这条原则。这个界面只是个本地工具，
    不需要并发、不需要认证、不需要数据库 —— http.server 够用。

安全边界（本地工具也要守）
    只绑 127.0.0.1，不对外网开放。
    静态文件服务做路径校验，只能读 runs/ 目录下的文件，防目录穿越。
    没有鉴权，因为没有必要：它只能被本机访问，且唯一能触发的动作是跑流水线。

启动：
    python3 -m pipeline.web          # 默认 http://127.0.0.1:8770
    finai web --port 9000
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlparse

from .console import safe_console

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = REPO_ROOT / "runs"
LOGS_ROOT = REPO_ROOT / "logs"
# 前端静态资源目录。存在 web/index.html 时，首页改用新的工作台界面；
# 不存在时仍然回落到下面那份内置的 _PAGE，保证旧路径随时可用。
WEB_ROOT = REPO_ROOT / "web"
DEFAULT_PORT = 8770
# 每家公司一个目录（见 finai_web/config.py 的 FINAI_COMPANY_DIR）：
# 工作台里选中的「当前公司」就是这里的一个子目录。
COMPANIES_ROOT = REPO_ROOT / "data" / "companies"


class RunState:
    """当前运行的状态。单用户本地工具，一个模块级实例就够了。

    界面轮询的是这里的字段，不是流水线内部状态 —— 这样界面与流水线解耦，
    流水线不需要知道有没有人在看。
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.run_id: str | None = None
        # 当前跑的是「取数」还是「估值」：两者的进度都来自 trace.jsonl，
        # 但取数写在 logs/<任务号>/ 下、估值写在 logs/<运行编号>/ 下。
        self.phase = ""
        self.job_id: str | None = None
        self.job_returncode: int | None = None
        self.job_log = ""
        self.started_at: float = 0.0
        self.finished_at: float = 0.0
        self.error: str = ""
        self.last_run: str | None = None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            # 取数阶段看任务号，估值阶段看运行编号 —— 界面不必关心这个区别
            active = self.job_id if self.phase == "plan" else self.run_id
            steps = _read_trace(active) if active else []
            elapsed = (
                (self.finished_at or time.time()) - self.started_at
                if self.started_at
                else 0.0
            )
            return {
                "running": self.running,
                "run_id": self.run_id,
                "phase": self.phase,
                "job_id": self.job_id,
                "job_returncode": self.job_returncode,
                "job_log": self.job_log,
                "elapsed_s": round(elapsed, 1),
                "error": self.error,
                "last_run": self.last_run,
                "steps": steps,
                "artifacts": _artifacts(self.run_id) if self.run_id else [],
                "evidence": _evidence(self.run_id) if self.run_id else [],
                "metrics": _metrics(self.run_id) if self.run_id else {},
                # 这次用的是手填数据还是年报抽取结果：总览与取数页都要说清，
                # 否则「数字哪来的」这件事只能靠人去翻 runs/ 下的清单。
                "data_source": _data_source(self.run_id),
            }


def _read_trace(run_id: str | None) -> list[dict[str, Any]]:
    """读执行轨迹，给界面显示「现在跑到哪一步了」。

    直接读 trace.jsonl —— 它是流水线本来就写的日志，界面只是换了个方式呈现，
    不额外要求流水线暴露进度回调。
    """
    return [
        {
            "seq": entry.get("seq"),
            "step": entry.get("step"),
            "status": entry.get("status"),
            "duration_s": entry.get("duration_s"),
            "model": entry.get("model"),
        }
        for entry in _trace_entries(run_id)
    ]


def _trace_entries(run_id: str | None) -> list[dict[str, Any]]:
    """把 trace.jsonl 原样读成字典列表（读不到就是空表）。

    界面要看两个东西：跑到哪一步了（_read_trace），以及这次数据从哪来
    （_data_source）。两者读同一个文件，所以解析只写一份，各自挑自己要的字段。
    """
    if not run_id:
        return []
    path = LOGS_ROOT / run_id / "trace.jsonl"
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _data_source(run_id: str | None) -> dict[str, Any]:
    """这次运行读的是手填文件，还是年报抽取结果。

    记在轨迹里（load_config 那一步的 extra），不另存一份状态 —— 报告清单与界面
    读的是同一处记录，两边的说法不会打架。

    为什么值得单列：报告里所有数字的来源就取决于这一个开关。评审问「这个数哪来的」，
    界面必须先能自己说出「这次用的是年报抽取结果」还是「手填的过渡数据」。
    """
    for entry in _trace_entries(run_id):
        if entry.get("step") != "load_config":
            continue
        extra = entry.get("extra") or {}
        switch = str(extra.get("data_source") or "")
        if not switch:
            continue
        return {
            "switch": switch,
            "opening_balance": str(extra.get("opening_balance_from") or ""),
            "peers": str(extra.get("peers_from") or ""),
        }
    return {}


def _artifacts(run_id: str | None) -> list[dict[str, Any]]:
    """列出产物文件，供界面显示与点击下载。"""
    if not run_id:
        return []
    directory = RUNS_ROOT / run_id
    if not directory.exists():
        return []
    items: list[dict[str, Any]] = []
    manifest_path = directory / "10_run_manifest.json"
    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
    warnings = manifest.get("warnings") or []

    def add(path: Path, name: str, kind: str) -> None:
        # 去重：数据文件的通配 [0-9][0-9]_* 会连报告（17/18/19）一起匹配进来，
        # 后面报告那一轮又会加一次，清单里就出现两份。
        if any(item["file"] == path.name for item in items):
            return
        items.append(
            {
                "name": name,
                "file": path.name,
                "kind": kind,
                "bytes": path.stat().st_size,
                "url": f"/runs/{run_id}/{path.relative_to(directory)}",
            }
        )

    for path in sorted(directory.glob("[0-9][0-9]_*")):
        # 报告由下面那一轮加，这里跳过 —— 否则它们会被标成「数据」。
        # 注意不能用 "_report." 这种模糊匹配：02_validation_report.json
        # 名字里也含它，会被误排除。必须按报告自身的编号前缀匹配。
        is_report = re.match(r"1[789]_report\.", path.name) is not None
        if path.is_file() and not is_report:
            add(path, path.stem, "数据")
    for path in sorted((directory / "charts").glob("*.png")):
        add(path, path.stem, "图表")
    # 报告三种格式。编号固定是 17/18/19，但用通配更耐改（比如以后加 20 号）
    for suffix, kind in (("md", "报告"), ("html", "报告"), ("pdf", "报告")):
        for candidate in sorted(directory.glob(f"1?_report.{suffix}")):
            add(candidate, candidate.stem, kind)
    return items + [{"warnings": warnings}] if warnings else items


def _metrics(run_id: str | None) -> dict[str, Any]:
    """从产物里挑出界面上那几个关键数字。"""
    if not run_id:
        return {}
    valuation = RUNS_ROOT / run_id / "03_valuation.json"
    field = RUNS_ROOT / run_id / "09_football_field.json"
    result: dict[str, Any] = {}
    if valuation.exists():
        try:
            payload = json.loads(valuation.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        for key, label, kind in (
            ("per_share", "每股价值（DCF）", "money"),
            ("price", "当前股价", "money"),
            ("implied_return", "隐含回报", "percent"),
            ("enterprise_value", "企业价值", "money"),
            ("terminal_share_of_ev", "终值占企业价值", "percent"),
            ("implied_exit_multiple", "隐含退出倍数", "multiple"),
        ):
            if payload.get(key) is not None:
                result[label] = {"value": payload[key], "kind": kind}
    if field.exists():
        try:
            payload = json.loads(field.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        overall = payload.get("overall")
        if overall:
            result["估值区间"] = {
                "value": f"{overall[0]:.2f} ~ {overall[1]:.2f}",
                "kind": "text",
            }
    return result


def _evidence(run_id: str | None) -> list[dict[str, Any]]:
    """复现凭证：能证明「这个结果可以重跑」的原始记录。

    这一组按钮是给评审用的。竞赛要求「数据来源可核验、执行过程可追溯、
    运行结果可复现」—— 这三句话对应下面三类文件，点开就是原始内容，
    不是我们二次加工过的展示页：

        数据来源  → data/ 下的三个输入文件（manifest 里有它们的 sha256）
        执行过程  → logs/<运行编号>/trace.jsonl（每一步的工具调用与耗时）
        结果复现  → 10_run_manifest.json（全部产物的 sha256 + 代码版本）
        模型可查  → logs/<运行编号>/narrative.json（提示词与响应原文）
    """
    if not run_id:
        return []
    run_dir = RUNS_ROOT / run_id
    log_dir = LOGS_ROOT / run_id
    inputs = _input_paths()

    def item(label: str, hint: str, path: Path, url: str) -> dict[str, Any]:
        return {
            "label": label,
            "hint": hint,
            "url": url,
            "exists": path.exists(),
            "bytes": path.stat().st_size if path.exists() else 0,
        }

    return [
        item("打开报告 PDF", "A4 报告书，LaTeX 排版（没装 LaTeX 时为 reportlab 版）",
             run_dir / "19_report.pdf", f"/runs/{run_id}/19_report.pdf"),
        item("打开报告网页", "图内嵌的单文件 HTML",
             run_dir / "18_report.html", f"/runs/{run_id}/18_report.html"),
        item("运行清单", "全部产物的 sha256、输入哈希、代码版本",
             run_dir / "10_run_manifest.json", f"/runs/{run_id}/10_run_manifest.json"),
        item("执行轨迹", "每一步的工具调用、耗时、输入输出哈希",
             log_dir / "trace.jsonl", f"/logs/{run_id}/trace.jsonl"),
        item("大模型原始问答", "提示词与响应的完整原文",
             log_dir / "narrative.json", f"/logs/{run_id}/narrative.json"),
        # 三个输入文件按「实际生效的路径」列，并在提示里写明它到底是哪一份 ——
        # 数据集可以指到别的目录（例如 data/real-000661/），不写清楚会让人以为
        # 看的是仓库里那份演示数据。
        item("输入 · 经营假设", f"含假设依据台账（{_rel(inputs['assumptions.toml'])}）",
             inputs["assumptions.toml"], "/data/assumptions.toml"),
        item("输入 · 行情快照", f"估值基准日的股价与负债表数据（{_rel(inputs['market.csv'])}）",
             inputs["market.csv"], "/data/market.csv"),
        item("输入 · 可比公司", f"可比公司最近一个完整年度的财务数据（{_rel(inputs['peers.csv'])}）",
             inputs["peers.csv"], "/data/peers.csv"),
    ]


def _recent_runs(limit: int = 8) -> list[dict[str, Any]]:
    if not RUNS_ROOT.exists():
        return []
    runs = sorted((p for p in RUNS_ROOT.iterdir() if p.is_dir()), key=lambda p: p.name)
    return [
        {
            "run_id": path.name,
            "has_report": (path / "19_report.pdf").exists()
            or (path / "18_report.html").exists(),
            "has_charts": len(list((path / "charts").glob("*.png"))),
        }
        for path in reversed(runs[-limit:])
    ]


def _environment() -> dict[str, Any]:
    """界面顶部显示的「环境准备好没有」。缺什么直接说，别等跑一半才报错。"""
    import importlib.util
    import os
    import platform
    import shutil
    import sys

    report_params: dict[str, Any] = {}
    try:
        from .config import load_report_params

        request = load_report_params()
        key, source = request.resolve_api_key()
        report_params = {
            "model": request.model,
            "has_key": bool(key),
            "key_source": source,
        }
    except Exception as err:  # noqa: BLE001 - 配置读不出来也要能打开界面
        report_params = {"model": "—", "has_key": False, "key_source": f"配置读取失败：{err}"}

    return {
        "matplotlib": importlib.util.find_spec("matplotlib") is not None,
        "reportlab": importlib.util.find_spec("reportlab") is not None,
        "pdfplumber": importlib.util.find_spec("pdfplumber") is not None,
        "latex": bool(shutil.which("xelatex") or shutil.which("lualatex")),
        "api_key": report_params.get("has_key", False),
        "model": report_params.get("model", "—"),
        "key_source": report_params.get("key_source", ""),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "env_latex": os.environ.get("SSL_CERT_FILE", ""),
    }


def _rel(path: Path) -> str:
    """路径显示成仓库内相对路径：界面上短，也不把本机目录结构摊出去。"""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


# 静态文件的 Content-Type。放在模块层，是为了让「按实际生效路径发输入文件」
# 那条路（见 _input_paths / do_GET 的 /data/ 分支）用同一张表。
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".webp": "image/webp",
    ".woff2": "font/woff2",
    ".jsonl": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".tex": "text/plain; charset=utf-8",
    ".toml": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}

# 界面上三个「输入文件」对外用固定名字（/data/<名字>），由服务器解析成**这次实际
# 生效**的路径。取数层的路径开关（FINAI_ASSUMPTIONS / FINAI_MARKET_CSV /
# FINAI_PEERS_CSV）能把输入指到别的数据集（例如 data/real-000661/），但界面原先
# 写死读 data/ 下那三个演示文件 —— 于是「新建估值」和「复现凭证」看到的输入跟这次
# 运行实际用的对不上（2026-10-14 用户指出：工作台里显示的还是示例公司）。
INPUT_NAMES = ("assumptions.toml", "market.csv", "peers.csv")


def _input_paths() -> dict[str, Path]:
    """这次实际生效的三个输入文件的路径。

    每次都现取（而不是在模块导入时固化）：测试要用 mock 换一套输入，真实使用里
    这几个路径也来自环境变量。
    """
    from . import config

    return {
        "assumptions.toml": Path(config.DEFAULT_CONFIG_PATH),
        "market.csv": Path(config.DEFAULT_MARKET_PATH),
        "peers.csv": Path(config.DEFAULT_PEER_PATH),
    }


def _short(text: str, limit: int = 140) -> str:
    """把原文压成一行并截断。出处要看得见，但不能让一页 JSON 撑到几百 KB。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _split_pdf_name(stem: str) -> tuple[str, int | None]:
    """`600519_2024` → ('600519', 2024)。命名不符合约定时只回文件名。"""
    head, _, tail = stem.rpartition("_")
    if head and tail.isdigit():
        return head, int(tail)
    return stem, None


def _provenance_rows(value: Any) -> list[dict[str, str]]:
    """出处字典 → 列表：界面按顺序渲染，不用管 dict 的键序。"""
    if not isinstance(value, dict):
        return []
    return [{"field": str(key), "provenance": str(item)} for key, item in value.items()]


def _peer_values(peers: Any, code: str) -> dict[str, float]:
    """取这家可比公司最终进流水线的数值（与出处表并排显示）。"""
    if not isinstance(peers, dict):
        return {}
    entry = peers.get(code)
    if not isinstance(entry, dict):
        return {}
    return {
        str(key): float(value)
        for key, value in entry.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _ingest_payload(paths: Any = None) -> dict[str, Any]:
    """取数层现状：定标清单、年报 PDF、抽取记录、数值出处、行情表校验。

    这一页是**只读**的：它把现状摊开，并说清下一步该敲哪条命令。下载与抽取要联网、
    要几分钟，留在命令行里 —— 现场演示时一个点下去就等两分钟的按钮是风险，不是功能。

    读的全是本地文件（都在 data/、config/ 下），不发任何网络请求。
    取数层用延迟导入：pipeline 不该在导入期依赖 finai_web，缺了也得能打开界面。

    `paths` 指定读哪一套文件（`finai_web.config.Paths`）。工作台可以按「当前选中的
    公司」传 `Paths.for_company(...)` 进来 —— 模块级的 `PATHS` 是进程启动那一刻定死的，
    它不会跟着界面上的选择变。不传就沿用模块级 `PATHS`（= 老行为）。
    """
    payload: dict[str, Any] = {
        "available": False,
        "error": "",
        "source": {
            "env_var": "FINAI_DATA_SOURCE",
            "active": "config",
            "last_run": _data_source(STATE.run_id or STATE.last_run),
        },
        "targets": {},
        "pdfs": {},
        "downloads": {},
        "records": {},
        "pipeline_input": {},
        "market": {},
    }
    try:
        # 取数层是仓库里的兄弟包（finai_web/），不是从 pip 装进来的。
        # 工作台可能从别的目录启动 —— 那时 sys.path 上只有「已安装的包」，而
        # 可编辑安装如果是在 finai_web 加进 pyproject 之前做的，映射里就没有它，
        # 取数页会显示成「取数层不可用」（2026-10-14 用户实测踩到）。
        # 把仓库根加进 sys.path 兜住这种情况：界面要么能用，要么给出照着就能做的
        # 下一步，不能只甩一句 No module named。
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from finai_web import market as market_mod
        from finai_web import targets as targets_mod
        from finai_web.config import DATA_SOURCE_ENV, PATHS, data_source
        from finai_web.downloader import load_manifest
    except ImportError as err:  # pragma: no cover - 取数层已随仓库提供
        payload["error"] = (
            f"取数层不可用：{err}。两种修法："
            "① 从仓库根目录启动工作台（先 cd 到 FinAI，再 python -m pipeline.web）；"
            "② 重装一次可编辑包（python -m pip install -e .）—— "
            "旧的可编辑安装是在 finai_web 加进 pyproject 之前做的，映射里没有它。"
        )
        return payload

    payload["available"] = True
    payload["source"]["env_var"] = DATA_SOURCE_ENV
    payload["source"]["active"] = data_source()
    paths = paths or PATHS

    # ── 定标公司清单 ────────────────────────────────────────────────
    targets: list[Any] = []
    target_error = ""
    try:
        targets = targets_mod.read_targets(paths.targets_csv, required=False)
    except Exception as err:  # noqa: BLE001 - 清单写错了也要能把界面打开
        target_error = str(err)
    payload["targets"] = {
        "path": _rel(paths.targets_csv),
        "exists": paths.targets_csv.is_file(),
        "template": _rel(PATHS.targets_example),
        "template_exists": PATHS.targets_example.is_file(),
        "count": len(targets),
        "items": [
            {
                "ticker": target.ticker,
                "name": target.name,
                "market": target.market,
                "years": list(target.years),
                "has_url": bool(target.annual_report_url),
            }
            for target in targets
        ],
        "error": target_error,
    }

    # ── 下好的年报 PDF ──────────────────────────────────────────────
    pdf_items: list[dict[str, Any]] = []
    if paths.raw_pdf.is_dir():
        for path in sorted(paths.raw_pdf.glob("*.pdf")):
            code, year = _split_pdf_name(path.stem)
            pdf_items.append(
                {"file": path.name, "code": code, "year": year, "bytes": path.stat().st_size}
            )
    payload["pdfs"] = {
        "dir": _rel(paths.raw_pdf),
        "count": len(pdf_items),
        "items": pdf_items,
    }

    # ── 下载清单：每份 PDF 的来源 URL 与 sha256 ─────────────────────
    manifest: dict[str, Any] = {}
    try:
        manifest = load_manifest(paths.manifest_json)
    except Exception:  # noqa: BLE001 - 清单损坏不该让整页打不开
        manifest = {}
    raw_entries = manifest.get("entries") if isinstance(manifest, dict) else None
    if isinstance(raw_entries, dict):
        raw_entries = list(raw_entries.values())
    downloads: list[dict[str, Any]] = []
    for entry in raw_entries or []:
        if not isinstance(entry, dict):
            continue
        downloads.append(
            {
                "code": str(entry.get("code") or ""),
                "year": entry.get("year"),
                "url": str(entry.get("url") or ""),
                "sha256": str(entry.get("sha256") or ""),
                "bytes": int(entry.get("bytes") or 0),
                "fetched_at": str(entry.get("fetched_at") or ""),
                "ok": bool(entry.get("ok", True)),
                "note": str(entry.get("note") or ""),
            }
        )
    payload["downloads"] = {
        "path": _rel(paths.manifest_json),
        "count": len(downloads),
        "updated_at": str(manifest.get("updated_at") or "") if isinstance(manifest, dict) else "",
        "items": downloads,
    }

    # ── 抽取记录：每个科目的值、页码与原文 ──────────────────────────
    records: list[dict[str, Any]] = []
    if paths.extracted.is_dir():
        for path in sorted(paths.extracted.glob("*.json")):
            # 汇总文件由下一段单独读，避免同一份数据出现两次、说法还可能不一致
            if path.name == "pipeline_input.json":
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            accounts = record.get("accounts") or {}
            account_items: list[dict[str, Any]] = []
            if isinstance(accounts, dict):
                for field, raw in accounts.items():
                    if not isinstance(raw, dict):
                        continue
                    account_items.append(
                        {
                            "field": str(field),
                            "label": str(raw.get("label") or ""),
                            "value": raw.get("value_yuan"),
                            "unit": str(raw.get("unit") or ""),
                            "page": raw.get("page"),
                            "raw": _short(str(raw.get("raw_text") or "")),
                            "note": str(raw.get("note") or ""),
                        }
                    )
            records.append(
                {
                    "file": path.name,
                    "code": str(record.get("code") or ""),
                    "name": str(record.get("name") or ""),
                    "year": record.get("year"),
                    "period": str(record.get("period") or ""),
                    "method": str(record.get("method") or ""),
                    "backend": str(record.get("backend") or ""),
                    "pdf": str(record.get("pdf") or ""),
                    "source_url": str(record.get("source_url") or ""),
                    "sha256": str(record.get("sha256") or ""),
                    "pages": record.get("pages"),
                    "has_text_layer": record.get("has_text_layer"),
                    "extracted_at": str(record.get("extracted_at") or ""),
                    "account_count": len(account_items),
                    "accounts": account_items,
                    "missing_accounts": [str(x) for x in (record.get("missing_accounts") or [])],
                    "notes": [str(x) for x in (record.get("notes") or [])],
                }
            )
    payload["records"] = {
        "dir": _rel(paths.extracted),
        "count": len(records),
        "items": records,
    }

    # ── 流水线输入：每个科目是「读到的」还是「倒挤的」 ──────────────
    input_path = paths.pipeline_input
    info: dict[str, Any] = {
        "path": _rel(input_path),
        "exists": input_path.is_file(),
        "generated_at": "",
        "target": "",
        "peer_codes": [],
        "companies": [],
        "error": "",
    }
    if input_path.is_file():
        try:
            data = json.loads(input_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            info["error"] = str(err)
            data = {}
        if isinstance(data, dict):
            info["generated_at"] = str(data.get("generated_at") or "")
            actuals = data.get("target_actuals") or {}
            if isinstance(actuals, dict):
                info["target"] = str(actuals.get("code") or "")
            peers = data.get("peers") or {}
            if isinstance(peers, dict):
                info["peer_codes"] = sorted(str(code) for code in peers)
            provenance = data.get("provenance") or {}
            if isinstance(provenance, dict):
                for code, entry in provenance.items():
                    if not isinstance(entry, dict):
                        continue
                    info["companies"].append(
                        {
                            "code": str(code),
                            "year": entry.get("year"),
                            "period": str(entry.get("period") or ""),
                            "method": str(entry.get("method") or ""),
                            "pdf": str(entry.get("pdf") or ""),
                            "source_url": str(entry.get("source_url") or ""),
                            "sha256": str(entry.get("sha256") or ""),
                            "opening": _provenance_rows(entry.get("opening_provenance")),
                            "peer": _provenance_rows(entry.get("peer_provenance")),
                            # 可比公司的指标值：出处表里有些字段在抽取记录里
                            # 没有同名科目（例如 EBIT 是由利润总额加利息费用推出来的），
                            # 界面要有个地方取到最终进流水线的那个数，否则会出现
                            # 「出处写着年报直接读取、取值却是空」的矛盾画面。
                            "peer_values": _peer_values(peers, str(code)),
                            "notes": [str(x) for x in (entry.get("notes") or [])],
                            "skipped": str(entry.get("skipped") or ""),
                        }
                    )
    payload["pipeline_input"] = info

    # ── 行情表：股价与股本要人工从 Wind / Choice 导出，这里只报校验结论 ──
    market_path = paths.market_csv
    market_info: dict[str, Any] = {
        "path": _rel(market_path),
        "exists": market_path.is_file(),
        "ok": False,
        "rows": 0,
        "target": "",
        "peer_count": 0,
        "errors": [],
        "warnings": [],
        "error": "",
    }
    if market_path.is_file():
        try:
            report = market_mod.validate(market_path, targets=targets or None)
            target = getattr(report, "target", None) or {}
            peers = getattr(report, "peers", None) or []
            market_info.update(
                {
                    "ok": bool(report.ok),
                    "rows": len(report.rows),
                    "errors": [str(x) for x in report.errors],
                    "warnings": [str(x) for x in report.warnings],
                    "target": str(target.get("code") or "") if isinstance(target, dict) else "",
                    "peer_count": len(peers),
                }
            )
        except Exception as err:  # noqa: BLE001 - 行情表格式错也要把结论说出来
            market_info["error"] = str(err)
    else:
        market_info["error"] = "行情表不存在：股价与摊薄股数要从 Wind / Choice 导出"
    payload["market"] = market_info

    return payload


# ── 每家公司一个目录：界面上的「当前公司」 ───────────────────────────


def _paths_for(directory: Path) -> Any:
    """按某家公司目录构造一套路径（取数层是兄弟包，延迟导入）。

    不能直接用模块级 `PATHS`：它是工作台进程启动那一刻定死的，不会跟着界面上的选择变。
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from finai_web.config import Paths

    return Paths.for_company(directory)


def _company_target(code: str) -> Path:
    """公司目录路径（可能还不存在 —— 取数时才创建）。目录名就是代码，必须防越界。"""
    text = str(code or "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text or ":" in text:
        raise ValueError(f"不合法的公司代码：{code!r}")
    return COMPANIES_ROOT / text


def _company_dir(code: str) -> Path | None:
    """已存在的公司目录；没有返回 None。"""
    try:
        candidate = _company_target(code)
    except ValueError:
        return None
    return candidate if candidate.is_dir() else None


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _company_info(directory: Path) -> dict[str, Any]:
    """一家公司在界面上要显示的现状。"""
    code = directory.name
    name = ""
    peers: list[str] = []
    years: list[int] = []
    raw = _read_json_file(directory / "company.json")
    if isinstance(raw, dict):
        target = raw.get("target")
        if isinstance(target, dict):
            code = str(target.get("code") or code)
            name = str(target.get("name") or "")
            years = [int(year) for year in (target.get("years") or []) if str(year).isdigit()]
        peers = [
            str(item.get("code"))
            for item in (raw.get("peers") or [])
            if isinstance(item, dict) and item.get("code")
        ]
    missing: list[str] = []
    draft = _read_json_file(directory / "assumptions.draft.json")
    if isinstance(draft, dict):
        fields = draft.get("fields")
        if isinstance(fields, dict):
            missing = [
                str(key)
                for key, item in fields.items()
                if isinstance(item, dict) and item.get("value") is None
            ]
    extracted = directory / "extracted"
    return {
        "code": code,
        "name": name,
        "dir": _rel(directory),
        "peers": peers,
        "years": years,
        "has_profile": (directory / "company.json").is_file(),
        "has_draft": (directory / "assumptions.draft.json").is_file(),
        "has_assumptions": (directory / "assumptions.toml").is_file(),
        "has_market": (directory / "market.csv").is_file(),
        "pdfs": len(list((directory / "raw_pdf").glob("*.pdf")))
        if (directory / "raw_pdf").is_dir()
        else 0,
        "records": len(
            [
                item
                for item in extracted.glob("*.json")
                if item.name not in {"pipeline_input.json", "peers.csv"}
            ]
        )
        if extracted.is_dir()
        else 0,
        "missing": missing,
    }


def _list_companies() -> list[dict[str, Any]]:
    if not COMPANIES_ROOT.is_dir():
        return []
    return [
        _company_info(path)
        for path in sorted(COMPANIES_ROOT.iterdir())
        if path.is_dir() and not path.name.startswith(".")
    ]


MAX_REPORT_BYTES = 200 * 1024 * 1024


def _stage_reports(items: Sequence[Any], *, folder: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """把界面上的「年报入库」条目整理成 job.import_reports 要的形状。

    一条可以是三种来源之一：
        浏览器上传（data 是 base64）→ 落到临时目录，按 `<代码>_<年份>.pdf` 命名
        机器上已有的路径（path）    → 直接用，入库时再复制
        只给了代码与年份（都没给）  → 承认这一家没有 PDF，取数时联网下载

    落盘时一定要用 `<代码>_<年份>.pdf` 这个命名：抽取那一步靠文件名认年份。
    """
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    total = 0
    for index, raw in enumerate(items or ()):
        if not isinstance(raw, Mapping):
            warnings.append(f"第 {index + 1} 条年报不是对象，已跳过")
            continue
        code = str(raw.get("code") or "").strip()
        if not code:
            warnings.append(f"第 {index + 1} 条年报没写公司代码，已跳过")
            continue
        try:
            year = int(raw.get("year") or 0)
        except (TypeError, ValueError):
            warnings.append(f"{code} 的披露年份不是数字，已跳过")
            continue
        entry: dict[str, Any] = {
            "code": code,
            "year": year,
            "name": str(raw.get("name") or "").strip(),
            "role": str(raw.get("role") or "").strip(),
            "title": str(raw.get("title") or "").strip(),
            "path": str(raw.get("path") or "").strip(),
        }
        blob = raw.get("data")
        if isinstance(blob, str) and blob:
            try:
                payload = base64.b64decode(blob, validate=False)
            except (binascii.Error, ValueError):
                warnings.append(f"{code} 的上传内容不是合法的 base64，已跳过")
                continue
            total += len(payload)
            if total > MAX_REPORT_BYTES:
                raise ValueError("一次入库的 PDF 总量超过 200 MB —— 请分批入库")
            if not payload[:8].startswith(b"%PDF"):
                warnings.append(f"{code} 这一份不是 PDF（前几个字节不是 %PDF），已跳过")
                continue
            if year <= 0:
                warnings.append(
                    f"{code} 没写披露年份，已跳过 —— 文件名要落成 <代码>_<年份>.pdf"
                )
                continue
            staged = folder / f"{code}_{year}.pdf"
            staged.write_bytes(payload)
            entry["path"] = str(staged)
        entries.append(entry)
    return entries, warnings


def _coerce(value: Any) -> Any:
    """确认屏传回来的值：空串就是「没填」，数字串变数字，其余原样。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return [_coerce(item) for item in value]
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return text


def _draft_payload(code: str) -> dict[str, Any]:
    """假设草稿 → 确认屏要的形状：按状态分组，每组里带出处。"""
    directory = _company_dir(code)
    if directory is None:
        return {
            "ok": False,
            "error": f"没有这家公司：{code}（data/companies/ 下没有这个目录）",
        }
    from . import assumptions_gen as gen

    draft_path, assumptions = gen.default_paths(directory)
    if not draft_path.is_file():
        return {
            "ok": False,
            "company": _company_info(directory),
            "error": f"还没有假设草稿（{_rel(draft_path)}）—— 先跑一次取数",
        }
    draft = gen.Draft.load(draft_path)
    groups: dict[str, list[dict[str, Any]]] = {}
    for status in (gen.DERIVED, gen.DEFAULT, gen.KEPT, gen.MISSING, "human"):
        groups[status] = [
            {"key": key, "value": item.value, "source": item.source}
            for key, item in draft.fields.items()
            if item.status == status
        ]
    return {
        "ok": True,
        "company": _company_info(directory),
        "company_info": draft.company,
        "normalized": draft.normalized,
        "notes": draft.notes,
        "generated_at": draft.generated_at,
        "missing": gen.missing_fields(draft),
        "groups": groups,
        "draft_path": _rel(draft_path),
        "assumptions_path": _rel(assumptions),
    }


def _tail(path: Path, lines: int = 25) -> str:
    """子进程日志的末尾几行。失败时界面要能说清原因，而不是只报一个退出码。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    rows = [line for line in text.splitlines() if line.strip()]
    return "\n".join(rows[-lines:])


def _initial_state() -> RunState:
    """初始状态指向最近一次运行 —— 打开界面就该看到上一次的结果，
    而不是一片空白等着人点按钮。第一次使用时没有历史运行，保持空。

    放在这里而不是 RunState.__init__ 里：_recent_runs 定义在类之后，
    在类的构造函数里调它会撞上「名称还没定义」。
    """
    state = RunState()
    existing = _recent_runs(limit=1)
    if existing:
        state.run_id = existing[0]["run_id"]
        state.last_run = state.run_id
    return state


STATE = _initial_state()


def _start_run() -> str:
    """在后台线程里跑流水线，立刻返回运行编号。"""
    from . import run as pipeline_run

    run_id = pipeline_run.new_run_id()
    with STATE.lock:
        if STATE.running:
            raise RuntimeError("已经有一次运行在进行中")
        STATE.running = True
        STATE.phase = "run"
        STATE.run_id = run_id
        STATE.started_at = time.time()
        STATE.finished_at = 0.0
        STATE.error = ""

    def worker() -> None:
        try:
            pipeline_run.run_pipeline(run_id)
        except Exception as err:  # noqa: BLE001 - 失败也要让界面看到原因
            with STATE.lock:
                STATE.error = f"{type(err).__name__}: {err}"
        finally:
            with STATE.lock:
                STATE.running = False
                STATE.finished_at = time.time()
                STATE.last_run = run_id

    threading.Thread(target=worker, daemon=True).start()
    return run_id


def _start_company_job(phase: str, code: str, extra: list[str] | None = None) -> str:
    """起一个带公司目录环境变量的子进程：取数（plan）或估值（run）。

    为什么必须是子进程：两个 config.py 的路径都是**导入时**读环境变量的，
    而工作台是长期运行的进程 —— 在同一进程里换公司就得改那批模块级默认值。
    子进程还有一个好处：进度机制一行都不用改，它照旧写 logs/<编号>/trace.jsonl。
    """
    from . import job as job_mod

    directory = _company_target(code)
    job_id = job_mod.new_job_id()
    with STATE.lock:
        if STATE.running:
            raise RuntimeError("已经有一次任务在进行中，等它跑完再点")
        STATE.running = True
        STATE.phase = phase
        STATE.job_id = job_id
        STATE.started_at = time.time()
        STATE.finished_at = 0.0
        STATE.error = ""
        STATE.job_returncode = None
        if phase == "run":
            # 估值的轨迹写在运行编号下，界面照旧按 runs/<编号>/ 找产物
            STATE.run_id = job_id

    env = dict(os.environ)
    env.update(job_mod.company_env(directory))
    log_dir = LOGS_ROOT / job_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "job.log"
    argv = [sys.executable, "-m", "pipeline.job", phase, "--company-dir", str(directory)]
    if phase == "plan":
        argv += ["--job-id", job_id, *(extra or [])]
    else:
        argv += ["--run-id", job_id]

    handle = log_path.open("wb")
    try:
        process = subprocess.Popen(  # noqa: S603 - 命令是代码拼的固定参数，不经 shell
            argv,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    except OSError as err:
        handle.close()
        with STATE.lock:
            STATE.running = False
            STATE.error = f"起子进程失败：{err}"
        raise RuntimeError(f"起子进程失败：{err}") from err

    def watch() -> None:
        returncode = process.wait()
        handle.close()
        with STATE.lock:
            STATE.running = False
            STATE.finished_at = time.time()
            STATE.job_returncode = returncode
            STATE.job_log = _rel(log_path)
            if returncode == 0:
                if phase == "run":
                    STATE.last_run = job_id
            else:
                STATE.error = _tail(log_path) or f"子进程退出码 {returncode}"

    threading.Thread(target=watch, daemon=True).start()
    return job_id


class Handler(BaseHTTPRequestHandler):
    """路由很少，直接写在 do_GET / do_POST 里，不引入框架。"""

    server_version = "FinAI-Web/0.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # 默认会把每个请求打到 stderr，界面自己会刷新，不需要这份噪音
        return

    # ── 工具 ────────────────────────────────────────────────────────

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 本地工具：只允许同源页面嵌入（报告中心的 HTML / PDF 内嵌预览需要它），
        # 别的站点仍然嵌不进来。
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        """读 POST 的 JSON 体。读不出来就当成空字典 —— 参数缺失由各端点自己说清。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _query(self) -> dict[str, str]:
        """查询串 → 单值字典（只取第一个值）。"""
        parsed = parse_qs(urlparse(self.path).query)
        return {key: values[0] for key, values in parsed.items() if values}

    def _html(self, text: str, code: int = 200) -> None:
        self._send(code, text.encode("utf-8"), "text/html; charset=utf-8")

    # ── 路由 ────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            app_page = WEB_ROOT / "index.html"
            if app_page.is_file():
                self._send(
                    200, app_page.read_bytes(), "text/html; charset=utf-8"
                )
            else:
                self._html(_PAGE)
            return
        if path == "/api/status":
            payload = STATE.snapshot()
            payload["environment"] = _environment()
            payload["recent"] = _recent_runs()
            self._json(payload)
            return
        if path == "/api/ingest":
            # 只读：取数页摊开现状；下载与抽取要人点确认后才起子进程（见 /api/plan）
            #
            # ?company=<代码> 决定看哪家公司 —— 传了就读那家公司目录里的文件，
            # 不传还是老行为（仓库 data/ 下那套平铺的演示数据）。
            code = self._query().get("company", "")
            directory = _company_dir(code) if code else None
            if directory is not None:
                payload = _ingest_payload(_paths_for(directory))
                payload["company"] = _company_info(directory)
                self._json(payload)
            else:
                self._json(_ingest_payload())
            return
        if path == "/api/companies":
            self._json(
                {
                    "ok": True,
                    "dir": _rel(COMPANIES_ROOT),
                    "exists": COMPANIES_ROOT.is_dir(),
                    "companies": _list_companies(),
                }
            )
            return
        if path == "/api/plan/draft":
            self._json(_draft_payload(self._query().get("company", "")))
            return
        if path.startswith("/web/"):
            self._serve_under(WEB_ROOT, path, "/web/")
            return
        if path.startswith("/runs/"):
            self._serve_under(RUNS_ROOT, path, "/runs/")
            return
        if path.startswith("/logs/"):
            self._serve_under(LOGS_ROOT, path, "/logs/")
            return
        if path.startswith("/data/"):
            # 三个输入文件按「实际生效的路径」发：数据集可以由 FINAI_ASSUMPTIONS /
            # FINAI_MARKET_CSV / FINAI_PEERS_CSV 指到别的目录，界面照旧用
            # /data/assumptions.toml 这样的固定地址取，两边才对得上。
            name = unquote(path[len("/data/") :])
            mapped = _input_paths().get(name)
            if mapped is not None and mapped.is_file():
                self._send(
                    200,
                    mapped.read_bytes(),
                    _CONTENT_TYPES.get(mapped.suffix.lower(), "text/plain; charset=utf-8"),
                )
                return
            self._serve_under(REPO_ROOT / "data", path, "/data/")
            return
        self._html("<h1>404</h1>", 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/run":
            try:
                run_id = _start_run()
            except RuntimeError as err:
                self._json({"ok": False, "error": str(err)}, 409)
                return
            self._json({"ok": True, "run_id": run_id})
            return
        if path == "/api/resolve":
            # 按名称检索公司（联网，只读）。返回全部候选交给界面点选 ——
            # 「长春」会匹配到三家，替用户挑就是替用户做决定。
            body = self._read_json()
            keyword = str(body.get("keyword") or "")
            try:
                from . import job as job_mod

                resolve = job_mod._ingest()["resolve"]
                self._json({"ok": True, **resolve.resolve(keyword).to_dict()})
            except Exception as err:  # noqa: BLE001 - 检索失败要原样告诉人
                self._json({"ok": False, "keyword": keyword, "error": str(err)}, 200)
            return
        if path == "/api/plan":
            body = self._read_json()
            code = str(body.get("target_code") or body.get("company") or "")
            extra: list[str] = []
            if body.get("keyword"):
                extra += ["--target", str(body["keyword"])]
            if body.get("target_code"):
                extra += ["--target-code", str(body["target_code"])]
            for peer in body.get("peers") or []:
                if str(peer).strip():
                    extra += ["--peer", str(peer).strip()]
            if body.get("as_of"):
                extra += ["--as-of", str(body["as_of"])]
            if body.get("force"):
                extra += ["--force"]
            # 报表不是人民币计价时必须给汇率（不给会在汇总那一步停下并说清原因）
            if body.get("fx") not in (None, ""):
                try:
                    fx = float(body["fx"])
                except (TypeError, ValueError):
                    self._json({"ok": False, "error": "汇率不是数字"}, 409)
                    return
                if fx <= 0:
                    self._json({"ok": False, "error": "汇率必须大于 0"}, 409)
                    return
                extra += ["--fx", str(fx)]
            # 手填行情：给了就不联网取价（断网/现场演示用）
            prices = body.get("prices") or []
            price_day = str(body.get("price_as_of") or "")
            if prices:
                if price_day:
                    extra += ["--price-as-of", price_day]
                for item in prices:
                    raw_code = str((item or {}).get("code") or "").strip()
                    raw_price = (item or {}).get("price")
                    raw_shares = (item or {}).get("shares")
                    # 股数（每股价值的分母）单独传：年报抽不到股本的公司靠它补上，
                    # 而且它不影响「要不要联网取价」这件事。
                    if raw_code and raw_shares not in (None, ""):
                        try:
                            shares_value = float(raw_shares)
                        except (TypeError, ValueError):
                            self._json(
                                {"ok": False, "error": f"{raw_code} 的股数不是数字"}, 409
                            )
                            return
                        if shares_value <= 0:
                            self._json(
                                {"ok": False, "error": f"{raw_code} 的股数必须为正数"}, 409
                            )
                            return
                        extra += ["--shares", f"{raw_code}={shares_value}"]
                    if not raw_code or raw_price in (None, ""):
                        continue
                    try:
                        extra += ["--price", f"{raw_code}={float(raw_price)}"]
                    except (TypeError, ValueError):
                        self._json(
                            {"ok": False, "error": f"{raw_code} 的价格不是数字"}, 409
                        )
                        return
            try:
                job_id = _start_company_job("plan", code, extra)
            except (ValueError, RuntimeError) as err:
                self._json({"ok": False, "error": str(err)}, 409)
                return
            self._json({"ok": True, "job_id": job_id, "phase": "plan"})
            return
        if path == "/api/reports":
            # 年报入库：把使用者手上的 PDF 收进公司目录，并按它们写出清单（不联网）。
            # 入库之后走的还是同一条 plan —— 抽取、取价、假设草稿、估值一模一样。
            body = self._read_json()
            items = body.get("reports") or body.get("files") or []
            code = str(
                body.get("company") or body.get("target_code") or ""
            ).strip()
            staged = tempfile.mkdtemp(prefix="finai-reports-")
            try:
                entries, warnings = _stage_reports(items, folder=Path(staged))
            except ValueError as err:
                self._json({"ok": False, "error": str(err)}, 409)
                return
            if not code:
                target = next(
                    (item for item in entries if item.get("role") == "target"), None
                )
                code = str((target or (entries[0] if entries else {})).get("code") or "")
            try:
                directory = _company_target(code)
            except ValueError as err:
                self._json({"ok": False, "error": str(err)}, 409)
                return
            if not entries:
                self._json(
                    {
                        "ok": False,
                        "error": "没有可入库的年报 —— "
                        + ("；".join(warnings) if warnings else "请选择 PDF 文件"),
                    },
                    409,
                )
                return
            from . import job as job_mod

            try:
                summary = job_mod.import_reports(
                    directory,
                    entries,
                    target_code=code,
                    resolved_from=str(body.get("resolved_from") or "工作台年报入库（不联网检索）"),
                )
            except (job_mod.JobError, ValueError) as err:
                self._json({"ok": False, "error": str(err)}, 409)
                return
            summary["ok"] = True
            summary["warnings"] = warnings + list(summary.get("warnings") or [])
            self._json(summary)
            return
        if path == "/api/plan/draft":
            # 确认屏上改的值直接落到草稿里 —— 草稿是「人确认过的假设」的唯一出处，
            # 估值那一步只读它。
            from . import assumptions_gen as gen

            body = self._read_json()
            directory = _company_dir(str(body.get("company") or ""))
            if directory is None:
                self._json({"ok": False, "error": "没有这家公司"}, 404)
                return
            draft_path, _ = gen.default_paths(directory)
            if not draft_path.is_file():
                self._json({"ok": False, "error": "还没有假设草稿，先跑一次取数"}, 404)
                return
            draft = gen.Draft.load(draft_path)
            changed: list[str] = []
            for key, raw_value in (body.get("fields") or {}).items():
                if key not in draft.fields:
                    continue
                value = _coerce(raw_value)
                if value is None:
                    draft.fields[key] = gen.Field(key, None, gen.MISSING, "清空了，需要填")
                else:
                    draft.set_value(key, value, source="在确认屏上改的")
                changed.append(str(key))
            draft.save(draft_path)
            self._json(
                {"ok": True, "changed": changed, "missing": gen.missing_fields(draft)}
            )
            return
        if path == "/api/ingest/run":
            body = self._read_json()
            code = str(body.get("company") or body.get("target_code") or "")
            directory = _company_dir(code)
            if directory is None or not (directory / "assumptions.draft.json").is_file():
                # 没有草稿就直接拦下：起一个注定失败的子进程只会让人等一分钟再看到错误
                self._json(
                    {"ok": False, "error": "这家公司还没有假设草稿 —— 先在界面上跑一次取数"},
                    409,
                )
                return
            try:
                job_id = _start_company_job("run", code)
            except (ValueError, RuntimeError) as err:
                self._json({"ok": False, "error": str(err)}, 409)
                return
            self._json({"ok": True, "run_id": job_id, "phase": "run"})
            return
        self._json({"ok": False, "error": "未知接口"}, 404)

    def _serve_under(self, base: Path, path: str, prefix: str) -> None:
        """只允许读指定目录下的文件 —— 这里必须防目录穿越。

        runs/ 放产物、logs/ 放执行轨迹与模型原文、data/ 放输入文件。
        三个目录都只读，且都做同一套路径校验。
        """
        relative = unquote(path[len(prefix) :])
        candidate = (base / relative).resolve()
        try:
            candidate.relative_to(base.resolve())
        except ValueError:
            self._json({"error": "越界路径"}, 403)
            return
        if not candidate.is_file():
            self._json({"error": "文件不存在"}, 404)
            return
        suffix = candidate.suffix.lower()
        self._send(
            200,
            candidate.read_bytes(),
            _CONTENT_TYPES.get(suffix, "application/octet-stream"),
        )


_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FinAI 自动化估值工作台</title>
<style>
:root{--navy:#0B1B33;--navy2:#13294b;--gold:#D2A74A;--text:#2b3440;--muted:#6b7280;
      --line:#e3e8ee;--bg:#f4f6f9;--card:#fff;--ok:#1a7f52;--warn:#b45309;--bad:#b3261e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-size:14px;
     font-family:-apple-system,"PingFang SC","Microsoft YaHei",Arial,sans-serif}
header{background:var(--navy);color:#fff;padding:18px 28px;display:flex;
       align-items:center;gap:16px;flex-wrap:wrap}
header h1{font-size:17px;margin:0;font-weight:500;letter-spacing:.5px}
header .sub{color:#9fb0c8;font-size:12.5px}
header .spacer{flex:1}
.env{display:flex;gap:14px;flex-wrap:wrap;font-size:12px}
.env span{display:inline-flex;align-items:center;gap:5px;color:#cbd5e1}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ok)}
.dot.off{background:#8b95a5}
main{max-width:1180px;margin:0 auto;padding:22px 28px 64px}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:18px 20px;margin-bottom:16px}
.row{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:16px;align-items:start}
@media(max-width:900px){.row{grid-template-columns:minmax(0,1fr)}}
h2{font-size:14px;margin:0 0 12px;color:var(--navy);font-weight:500}
button{font:inherit;border:0;border-radius:4px;cursor:pointer}
.primary{background:var(--navy);color:#fff;padding:11px 22px;font-size:14px}
.primary:hover{background:var(--navy2)}
.primary:disabled{background:#aab4c2;cursor:not-allowed}
.ghost{display:inline-block;background:#fff;color:var(--navy);border:1px solid var(--line);
       padding:10px 18px;font-size:14px;text-decoration:none;border-radius:4px}
.ghost:hover{border-color:var(--gold);background:#fffdf7}
.ghost{background:transparent;border:1px solid var(--line);padding:7px 14px;color:var(--text)}
.ghost:hover{border-color:var(--navy);color:var(--navy)}
.status{margin-left:14px;font-size:13px;color:var(--muted)}
.steps{list-style:none;margin:0;padding:0}
.steps li{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px dashed var(--line);font-size:13px}
.steps li:last-child{border-bottom:0}
.steps .name{flex:1;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.steps .ms{color:var(--muted);font-variant-numeric:tabular-nums}
.pill{font-size:11px;padding:2px 8px;border-radius:10px;border:1px solid var(--line);color:var(--muted)}
.pill.done{color:var(--ok);border-color:#bfe3d0;background:#f1faf5}
.pill.now{color:var(--navy);border-color:var(--gold);background:#fdf8ec}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.metric{border:1px solid var(--line);border-radius:5px;padding:12px 14px}
.metric .k{font-size:12px;color:var(--muted)}
.metric .v{font-size:19px;font-variant-numeric:tabular-nums;margin-top:4px;color:var(--navy)}
.figs{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.figs a{display:block;border:1px solid var(--line);border-radius:5px;overflow:hidden;background:#fff}
.figs img{width:100%;display:block}
.figs .cap{font-size:11.5px;color:var(--muted);padding:6px 8px}
.files{width:100%;border-collapse:collapse;font-size:13px}
.files td{padding:6px 8px;border-bottom:1px solid var(--line)}
.files td:last-child{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.files a{color:var(--navy);text-decoration:none}
.files a:hover{text-decoration:underline}
.warn{background:#fff8f0;border:1px solid #f0d9b8;color:var(--warn);
      padding:10px 14px;border-radius:4px;font-size:13px;margin-bottom:10px}
.err{background:#fdf2f2;border:1px solid #f0c4c1;color:var(--bad);
     padding:10px 14px;border-radius:4px;font-size:13px;margin-bottom:10px}
.muted{color:var(--muted);font-size:12.5px}
.runs{list-style:none;margin:0;padding:0;font-size:13px}
.runs li{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px dashed var(--line)}
.runs li:last-child{border-bottom:0}
.btns{display:flex;flex-wrap:wrap;gap:10px}
.btn-ev{display:block;text-align:left;border:1px solid var(--line);background:#fff;
        border-radius:5px;padding:10px 14px;min-width:190px;flex:1 1 190px;
        text-decoration:none;color:var(--text)}
.btn-ev:hover{border-color:var(--gold);background:#fffdf7}
.btn-ev .l{font-weight:500;color:var(--navy);font-size:13.5px}
.btn-ev .h{display:block;color:var(--muted);font-size:11.5px;margin-top:3px}
.btn-ev.gone{opacity:.45;cursor:not-allowed}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#f2f5f8;
     padding:1px 5px;border-radius:3px;font-size:12.5px}
</style>
</head>
<body>
<header>
  <h1>FinAI 自动化估值工作台</h1>
  <span class="sub">选题 4 · 自动化估值建模</span>
  <span class="spacer"></span>
  <div class="env" id="env"></div>
</header>
<main>
  <div class="card">
    <h2>运行</h2>
    <button class="primary" id="run">运行估值流水线</button>
    <a class="ghost" id="quick-report" href="#" target="_blank" rel="noopener"
       style="display:none">打开报告</a>
    <span class="status" id="status">就绪</span>
    <p class="muted" style="margin:10px 0 0">
      流程：读配置 → 三表联动预测 → 勾稽校验 → 资本成本 → 现金流折现 →
      敏感性 → 反向求解 → 相对估值 → 估值区间 → 大模型撰写报告 → 落盘。
      全程约两分钟，其中大部分是大模型在推理与写作。
    </p>
  </div>

  <div id="alerts"></div>

  <div class="row">
    <div>
      <div class="card" id="evidence-card" style="display:none">
        <h2>复现凭证</h2>
        <p class="muted" style="margin:0 0 12px">
          这些是原始记录，不是二次加工的内容。数据来源、执行过程、结果哈希、
          模型原文都能点开核对 —— 评审要复现，靠的就是这一组。
        </p>
        <div class="btns" id="evidence"></div>
      </div>
      <div class="card" id="metrics-card" style="display:none">
        <h2>本次结果</h2>
        <div class="metrics" id="metrics"></div>
      </div>
      <div class="card" id="figs-card" style="display:none">
        <h2>分析图表</h2>
        <div class="figs" id="figs"></div>
      </div>
      <div class="card" id="files-card" style="display:none">
        <h2>产物清单</h2>
        <table class="files"><tbody id="files"></tbody></table>
      </div>
    </div>
    <div>
      <div class="card">
        <h2>执行过程</h2>
        <ul class="steps" id="steps"><li class="muted">还没有运行</li></ul>
      </div>
      <div class="card">
        <h2>历史运行</h2>
        <ul class="runs" id="runs"><li class="muted">—</li></ul>
      </div>
    </div>
  </div>
</main>
<script>
const STEP_NAMES = {
  load_config:"读取配置", forecast:"三表联动预测", validate:"勾稽校验",
  wacc:"资本成本", valuation:"现金流折现", sensitivity:"敏感性分析",
  reverse_dcf:"反向求解", comps:"相对估值", football_field:"估值区间",
  narrative:"大模型撰写报告", write_outputs:"落盘与报告渲染"
};
const ENV_NAMES = {matplotlib:"图表", latex:"LaTeX", reportlab:"PDF后备",
                   pdfplumber:"年报抽取", api_key:"大模型"};
const fmtBytes = n => n > 1048576 ? (n/1048576).toFixed(1)+" MB"
                   : n > 1024 ? (n/1024).toFixed(0)+" KB" : n+" B";
const fmtValue = m => m.kind==="percent" ? (m.value*100).toFixed(2)+"%"
                   : m.kind==="multiple" ? m.value.toFixed(2)+"x"
                   : m.kind==="text" ? m.value : m.value.toLocaleString("zh-CN",{minimumFractionDigits:2,maximumFractionDigits:2});
let offline = false;

async function post(path){
  const r = await fetch(path,{method:"POST"});
  return r.json();
}

document.getElementById("run").onclick = async () => {
  const btn = document.getElementById("run");
  btn.disabled = true; btn.textContent = "正在运行…";
  const res = await post("/api/run");
  if(!res.ok){ alert(res.error); btn.disabled=false; btn.textContent="运行估值流水线"; }
  refresh();
};

async function refresh(){
  let s;
  try {
    s = await (await fetch("/api/status", {cache:"no-store"})).json();
    offline = false;
  } catch(e) {
    // 这个页面必须由本地服务提供。服务停了以后页面还在（浏览器缓存着），
    // 但所有接口都失效 —— 所以要把「怎么办」写在页面上，而不是只说连不上。
    if(!offline){
      offline = true;
      document.getElementById("alerts").innerHTML =
        '<div class="err"><b>本地服务没有响应。</b>' +
        '这个页面必须由本地服务提供，不能离线打开，也不要只保存网页文件。<br>' +
        '服务可能已经停止：重新双击仓库根目录的 <code>启动工作台.command</code>，' +
        '或在终端里跑 <code>make web</code>。<br>' +
        '<b>请保持那个终端窗口开着</b> —— 关掉窗口就等于停止服务。' +
        '页面每 1.2 秒自动重连，服务起来后会自己恢复。</div>';
      document.getElementById("status").textContent = "未连接到本地服务";
    }
    return;
  }

  // 环境状态
  const env = s.environment || {};
  document.getElementById("env").innerHTML = Object.entries(ENV_NAMES).map(([k,label])=>{
    const on = env[k] ? "" : " off";
    return `<span><i class="dot${on}"></i>${label}</span>`;
  }).join("");

  // 状态行
  const status = document.getElementById("status");
  const btn = document.getElementById("run");
  if(s.running){
    status.textContent = `运行中 ${s.run_id} · 已用 ${s.elapsed_s}s`;
    btn.disabled = true; btn.textContent = "正在运行…";
  } else {
    btn.disabled = false; btn.textContent = "运行估值流水线";
    status.textContent = s.run_id ? `最近一次 ${s.run_id} · 用时 ${s.elapsed_s}s` : "就绪";
  }

  // 提示
  const alerts = [];
  if(s.error) alerts.push(`<div class="err">运行失败：${s.error}</div>`);
  if(!env.api_key) alerts.push(`<div class="warn">没有配置大模型 key，报告将不含推理与结论（${env.key_source||"未知来源"}）。设置 <code>DEEPSEEK_API_KEY</code> 或写 <code>apikey.txt</code>。</div>`);
  if(!env.matplotlib) alerts.push(`<div class="warn">没有安装 matplotlib，报告不会包含图表。</div>`);
  (s.artifacts||[]).filter(a=>a.warnings).forEach(a=>a.warnings.forEach(w=>alerts.push(`<div class="warn">${w}</div>`)));
  document.getElementById("alerts").innerHTML = alerts.join("");

  // 步骤
  const steps = s.steps || [];
  const running = s.running;
  const list = steps.map((st,i)=>{
    const last = i === steps.length-1;
    const cls = (last && running) ? "now" : "done";
    const label = (last && running) ? "进行中" : "完成";
    return `<li><span class="pill ${cls}">${label}</span>
      <span class="name">${STEP_NAMES[st.step] || st.step}</span>
      <span class="ms">${st.duration_s!=null?st.duration_s.toFixed(2)+"s":""}</span></li>`;
  }).join("");
  document.getElementById("steps").innerHTML = steps.length ? list : '<li class="muted">还没有运行</li>';

  // 复现凭证
  const ev = s.evidence || [];
  document.getElementById("evidence-card").style.display = ev.length ? "block" : "none";
  document.getElementById("evidence").innerHTML = ev.map(e=>
    e.exists
      ? `<a class="btn-ev" href="${e.url}" target="_blank" rel="noopener">
           <span class="l">${e.label}</span>
           <span class="h">${e.hint}</span></a>`
      : `<span class="btn-ev gone"><span class="l">${e.label}</span>
           <span class="h">文件不存在</span></span>`
  ).join("");

  // 顶部快捷入口：直接开报告
  const quick = (s.artifacts||[]).find(a=>a.kind==="报告" && a.file.endsWith(".pdf"));
  const qbtn = document.getElementById("quick-report");
  if(quick){ qbtn.href = quick.url; qbtn.style.display = "inline-block"; }
  else { qbtn.style.display = "none"; }

  // 指标
  const metrics = s.metrics || {};
  const keys = Object.keys(metrics);
  document.getElementById("metrics-card").style.display = keys.length ? "block" : "none";
  document.getElementById("metrics").innerHTML = keys.map(k=>
    `<div class="metric"><div class="k">${k}</div><div class="v">${fmtValue(metrics[k])}</div></div>`).join("");

  // 图
  const figs = (s.artifacts||[]).filter(a=>a.kind==="图表");
  document.getElementById("figs-card").style.display = figs.length ? "block" : "none";
  document.getElementById("figs").innerHTML = figs.map(f=>
    `<a href="${f.url}" target="_blank"><img src="${f.url}" alt="${f.name}">
     <div class="cap">${f.file}</div></a>`).join("");

  // 产物
  const files = (s.artifacts||[]).filter(a=>a.kind!=="图表");
  document.getElementById("files-card").style.display = files.length ? "block" : "none";
  document.getElementById("files").innerHTML = files.map(f=>
    `<tr><td>${f.kind}</td><td><a href="${f.url}" target="_blank">${f.file}</a></td>
     <td>${fmtBytes(f.bytes)}</td></tr>`).join("");

  // 历史
  document.getElementById("runs").innerHTML = (s.recent||[]).map(r=>
    `<li><span>${r.run_id}</span><span class="muted">${r.has_report?"有报告":"仅数据"} · 图 ${r.has_charts}</span></li>`
  ).join("") || '<li class="muted">—</li>';
}

refresh();
setInterval(refresh, 1200);
</script>
</body>
</html>
"""


def serve(port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    """起服务。只绑 127.0.0.1 —— 这是本地工具，不对外网开放。"""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"FinAI 工作台已启动：{url}")
    print("按 Ctrl-C 停止。")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    safe_console()
    parser = argparse.ArgumentParser(description="FinAI 本地工作台")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)
    serve(port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
