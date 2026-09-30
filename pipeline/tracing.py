"""日志层：记录一次运行做了什么，以及产物是怎么产生的。

两个文件，职责不同：
    logs/<run_id>/trace.jsonl        逐步追加的执行轨迹，一行一条记录
    logs/<run_id>/run_manifest.json  这次运行的总清单

为什么轨迹用 JSONL（一行一个 JSON）而不是一个大 JSON 数组：
    因为它是「追加写」的 —— 每完成一步就往文件末尾加一行，不需要把整个
    文件读出来改完再写回去。程序中途崩溃时，已经写下的记录还在。


设计原则：日志层绝不能成为失败点。
    取不到 git 信息就记 unknown，算不出哈希就不算 —— 但流水线该跑还得跑。
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_ROOT = REPO_ROOT / "logs"


def now_iso() -> str:
    """当前时刻，带时区。日志里写本地时间方便人读，带时区方便换算。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: str | Path) -> str:
    """算一个文件的 sha256。

    分块读取而不是一次读进内存：年报或有几 MB，将来数据更大也不怕。
    """
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1 << 20)  # 每次 1 MB
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _run_git(args: list[str]) -> str | None:
    """跑一条 git 命令。任何失败都返回 None，不抛异常。"""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip()


def git_info() -> dict[str, Any]:
    """当前代码版本。

    dirty 表示工作区有未提交的改动。这一项很重要：如果 dirty 为真，
    说明「这份产物对应的代码」并不等于任何一个提交，严格讲不可复现。
    """
    commit = _run_git(["rev-parse", "HEAD"])
    status = _run_git(["status", "--porcelain"])
    return {
        "commit": commit or "unknown",
        "commit_short": (commit or "unknown")[:7],
        "branch": _run_git(["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown",
        "dirty": bool(status),
    }


class RunTrace:
    """一次运行的轨迹记录器。

    用法：
        trace = RunTrace(run_id)
        trace.record(step="load_config", tool="pipeline.config.load_case",
                     inputs=[CONFIG_PATH], duration_s=0.01)
        trace.finish(outputs=[target])
    """

    def __init__(self, run_id: str, log_root: str | Path = LOG_ROOT) -> None:
        self.run_id = run_id
        self.dir = Path(log_root) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.trace_path = self.dir / "trace.jsonl"
        self.manifest_path = self.dir / "run_manifest.json"

        self.started_at = now_iso()
        self.git = git_info()
        self._seq = 0
        self._steps: list[dict[str, Any]] = []
        self._inputs: dict[str, str] = {}
        self._outputs: dict[str, str] = {}

    @property
    def input_hashes(self) -> dict[str, str]:
        """这次运行读过的输入文件及其哈希。

        供上层导出到「产物清单」——这样产物目录里的清单是自解释的，
        打开 runs/<编号>/ 就能看到当时读的是哪份配置、哈希是多少。
        """
        return dict(self._inputs)

    @property
    def step_summary(self) -> list[dict[str, Any]]:
        """各步骤的摘要（序号、名字、状态、耗时），供上层导出。"""
        return [
            {
                "seq": item["seq"],
                "step": item["step"],
                "status": item["status"],
                "duration_s": item["duration_s"],
            }
            for item in self._steps
        ]

    def _hash_all(self, paths: Iterable[str | Path]) -> dict[str, str]:
        """把一批路径算成 {路径: sha256}，同时登记到输入/输出总表里。"""
        result: dict[str, str] = {}
        for path in paths:
            text = str(path)
            try:
                result[text] = sha256_file(path)
            except OSError as err:
                # 文件不存在也算一条记录，但标出来，不让日志层把流程搞崩
                result[text] = f"unavailable: {err.strerror}"
        return result

    def record(
        self,
        step: str,
        *,
        tool: str = "",
        status: str = "ok",
        duration_s: float | None = None,
        inputs: Iterable[str | Path] = (),
        outputs: Iterable[str | Path] = (),
        model: str | None = None,
        prompt_version: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """记录一步。inputs 和 outputs 传文件路径，哈希由这里算。"""
        input_hashes = self._hash_all(inputs)
        output_hashes = self._hash_all(outputs)
        self._inputs.update(input_hashes)
        self._outputs.update(output_hashes)

        self._seq += 1
        entry: dict[str, Any] = {
            "run_id": self.run_id,
            "seq": self._seq,
            "timestamp": now_iso(),
            "step": step,
            "tool": tool,
            "status": status,
            "duration_s": round(duration_s, 6) if duration_s is not None else None,
            "inputs": input_hashes,
            "outputs": output_hashes,
            "model": model,
            "prompt_version": prompt_version,
        }
        if extra:
            entry["extra"] = extra

        # 追加写一行。每行是一个独立完整的 JSON，所以可以 tail / grep。
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

        self._steps.append(entry)
        return entry

    def finish(self, *, status: str = "ok", outputs: Iterable[str | Path] = ()) -> Path:
        """写出 run_manifest.json，收尾。

        注意这里会算 trace.jsonl 自己的哈希 —— 此时轨迹已经写完了，
        所以清单能证明「这份轨迹从写完之后就没被改过」。
        """
        self._outputs.update(self._hash_all(outputs))

        try:
            trace_hash: str | None = sha256_file(self.trace_path)
        except OSError:
            trace_hash = None

        manifest = {
            "run_id": self.run_id,
            "status": status,
            "started_at": self.started_at,
            "finished_at": now_iso(),
            "code": self.git,
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
            },
            "steps": [
                {
                    "seq": item["seq"],
                    "step": item["step"],
                    "status": item["status"],
                    "duration_s": item["duration_s"],
                }
                for item in self._steps
            ],
            "inputs": self._inputs,
            "outputs": self._outputs,
            "trace_file": str(self.trace_path),
            "trace_sha256": trace_hash,
            # 给 AI 层预留：将来记录用了哪个模型、哪个版本的提示词
            "model_versions": {},
            "prompt_versions": {},
        }
        text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        self.manifest_path.write_text(text, encoding="utf-8")
        return self.manifest_path
