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

import json
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .console import safe_console

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = REPO_ROOT / "runs"
LOGS_ROOT = REPO_ROOT / "logs"
# 前端静态资源目录。存在 web/index.html 时，首页改用新的工作台界面；
# 不存在时仍然回落到下面那份内置的 _PAGE，保证旧路径随时可用。
WEB_ROOT = REPO_ROOT / "web"
DEFAULT_PORT = 8770


class RunState:
    """当前运行的状态。单用户本地工具，一个模块级实例就够了。

    界面轮询的是这里的字段，不是流水线内部状态 —— 这样界面与流水线解耦，
    流水线不需要知道有没有人在看。
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.run_id: str | None = None
        self.started_at: float = 0.0
        self.finished_at: float = 0.0
        self.error: str = ""
        self.last_run: str | None = None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            steps = _read_trace(self.run_id) if self.run_id else []
            elapsed = (
                (self.finished_at or time.time()) - self.started_at
                if self.started_at
                else 0.0
            )
            return {
                "running": self.running,
                "run_id": self.run_id,
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
    data_dir = REPO_ROOT / "data"

    def item(label: str, hint: str, path: Path, url: str) -> dict[str, Any]:
        return {
            "label": label,
            "hint": hint,
            "url": url,
            "exists": path.exists(),
            "bytes": path.stat().st_size if path.exists() else 0,
        }

    return [
        item("打开报告 PDF", "十二页 A4，LaTeX 排版",
             run_dir / "19_report.pdf", f"/runs/{run_id}/19_report.pdf"),
        item("打开报告网页", "图内嵌的单文件 HTML",
             run_dir / "18_report.html", f"/runs/{run_id}/18_report.html"),
        item("运行清单", "全部产物的 sha256、输入哈希、代码版本",
             run_dir / "10_run_manifest.json", f"/runs/{run_id}/10_run_manifest.json"),
        item("执行轨迹", "每一步的工具调用、耗时、输入输出哈希",
             log_dir / "trace.jsonl", f"/logs/{run_id}/trace.jsonl"),
        item("大模型原始问答", "提示词与响应的完整原文",
             log_dir / "narrative.json", f"/logs/{run_id}/narrative.json"),
        item("输入 · 经营假设", "含假设依据台账，manifest 中有其 sha256",
             data_dir / "assumptions.toml", "/data/assumptions.toml"),
        item("输入 · 行情快照", "估值基准日的股价与负债表数据",
             data_dir / "market.csv", "/data/market.csv"),
        item("输入 · 可比公司", "可比公司最近一个完整年度的财务数据",
             data_dir / "peers.csv", "/data/peers.csv"),
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


def _ingest_payload() -> dict[str, Any]:
    """取数层现状：定标清单、年报 PDF、抽取记录、数值出处、行情表校验。

    这一页是**只读**的：它把现状摊开，并说清下一步该敲哪条命令。下载与抽取要联网、
    要几分钟，留在命令行里 —— 现场演示时一个点下去就等两分钟的按钮是风险，不是功能。

    读的全是本地文件（都在 data/、config/ 下），不发任何网络请求。
    取数层用延迟导入：pipeline 不该在导入期依赖 finai_web，缺了也得能打开界面。
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
        from finai_web import market as market_mod
        from finai_web import targets as targets_mod
        from finai_web.config import DATA_SOURCE_ENV, PATHS, data_source
        from finai_web.downloader import load_manifest
    except ImportError as err:  # pragma: no cover - 取数层已随仓库提供
        payload["error"] = f"取数层不可用：{err}"
        return payload

    payload["available"] = True
    payload["source"]["env_var"] = DATA_SOURCE_ENV
    payload["source"]["active"] = data_source()

    # ── 定标公司清单 ────────────────────────────────────────────────
    targets: list[Any] = []
    target_error = ""
    try:
        targets = targets_mod.read_targets(required=False)
    except Exception as err:  # noqa: BLE001 - 清单写错了也要能把界面打开
        target_error = str(err)
    payload["targets"] = {
        "path": _rel(PATHS.targets_csv),
        "exists": PATHS.targets_csv.is_file(),
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
    if PATHS.raw_pdf.is_dir():
        for path in sorted(PATHS.raw_pdf.glob("*.pdf")):
            code, year = _split_pdf_name(path.stem)
            pdf_items.append(
                {"file": path.name, "code": code, "year": year, "bytes": path.stat().st_size}
            )
    payload["pdfs"] = {
        "dir": _rel(PATHS.raw_pdf),
        "count": len(pdf_items),
        "items": pdf_items,
    }

    # ── 下载清单：每份 PDF 的来源 URL 与 sha256 ─────────────────────
    manifest: dict[str, Any] = {}
    try:
        manifest = load_manifest()
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
        "path": _rel(PATHS.manifest_json),
        "count": len(downloads),
        "updated_at": str(manifest.get("updated_at") or "") if isinstance(manifest, dict) else "",
        "items": downloads,
    }

    # ── 抽取记录：每个科目的值、页码与原文 ──────────────────────────
    records: list[dict[str, Any]] = []
    if PATHS.extracted.is_dir():
        for path in sorted(PATHS.extracted.glob("*.json")):
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
        "dir": _rel(PATHS.extracted),
        "count": len(records),
        "items": records,
    }

    # ── 流水线输入：每个科目是「读到的」还是「倒挤的」 ──────────────
    input_path = PATHS.pipeline_input
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
    market_path = PATHS.market_csv
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
            # 只读：取数页只看现状，下载与抽取留在命令行（见 _ingest_payload）
            self._json(_ingest_payload())
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
        types = {
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
        self._send(200, candidate.read_bytes(), types.get(suffix, "application/octet-stream"))


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
