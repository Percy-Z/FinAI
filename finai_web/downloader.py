"""下载编排：定标公司 → 年报 PDF 落盘 → 清单（manifest）。

顺序（每一步都有明确的失败语义，不静默跳过）
    1. 清单里写了 `annual_report_url`  → 直接用（最快，也最可控）
    2. 巨潮检索                        → 拿到公告 → 拼 PDF 直链
    3. Firecrawl 搜公告页              → 从页面里找 PDF 直链（兜底）
    4. 都不行                          → 记一条 note，继续下一家（不中断整批）

为什么失败要「继续下一家」
    定标五家公司，第四家年报缺失不该让前四家白下。整批结束后
    `finai ingest fetch` 会把失败清单打在最后，并给非零退出码 ——
    失败是看得见的，只是不阻塞。

产物
    data/raw_pdf/<代码>_<年份>.pdf      文件名里带年份，同一年不会混两份
    data/manifest.json                  每份 PDF 的来源 URL、sha256、字节数、方式

manifest 的用处：报告里要能回答「这份数是从哪来的」。有了 URL 与哈希，
换一台机器重跑能得到同一份文件（哈希一致），评审现场可复现。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import cninfo
from .config import PATHS
from .extract import pdf_sha256
from .firecrawl import Firecrawl, FirecrawlError, FirecrawlUnavailable
from .http import HttpError, download, get_bytes, post_form
from .targets import Target

PDF_MAGIC = b"%PDF"


class DownloadError(RuntimeError):
    """下载失败，且原因需要讲给使用者听。"""


@dataclass
class FetchOutcome:
    """一次下载的结果。失败也是一条结果 —— 失败必须留痕。"""

    code: str
    year: int
    ok: bool
    path: str = ""
    url: str = ""
    title: str = ""
    method: str = ""
    bytes: int = 0
    sha256: str = ""
    note: str = ""
    fetched_at: str = ""
    candidates: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "year": self.year,
            "ok": self.ok,
            "path": self.path,
            "url": self.url,
            "title": self.title,
            "method": self.method,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "note": self.note,
            "fetched_at": self.fetched_at,
            "candidates": self.candidates,
        }


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def pdf_name(code: str, year: int) -> str:
    return f"{code}_{year}.pdf"


def pdf_path(code: str, year: int, directory: Path | None = None) -> Path:
    return (directory or PATHS.raw_pdf) / pdf_name(code, year)


@dataclass
class Resolved:
    """找到的 PDF 直链 + 它是怎么找到的。"""

    url: str
    title: str
    method: str
    candidates: list[dict[str, str]] = field(default_factory=list)


def resolve_report(
    target: Target,
    year: int,
    *,
    firecrawl: Firecrawl | None = None,
    transport: Any = None,
) -> Resolved:
    """找这一年年报的 PDF 直链。找不到抛 DownloadError。"""
    if target.annual_report_url and len(target.years_desc) <= 1:
        # 只标了一家一年的时候，直链就是这一年；多年份时直链不可复用，
        # 继续走检索（否则会把同一份 PDF 当成两年的年报下载两次）。
        return Resolved(url=target.annual_report_url, title="清单里指定的直链", method="targets.csv")

    announcements: list[cninfo.Announcement] = []
    search_error: Exception | None = None
    try:
        announcements = cninfo.query(
            code=target.ticker,
            column=target.cninfo_column,
            year=year,
            org_id=target.org_id,
            transport=transport or post_form,
        )
    except cninfo.CninfoError as err:
        search_error = err
        if firecrawl is None or not firecrawl.available:
            raise DownloadError(
                f"{target.ticker} {year} 年报检索失败，且没有配置 Firecrawl 兜底：{err}"
            ) from err

    picked = cninfo.pick_annual_report(announcements, year)
    if picked is not None:
        return Resolved(
            url=picked.pdf_url,
            title=picked.title,
            method="cninfo",
            candidates=[item.to_dict() for item in cninfo.candidates(announcements, year)[:5]],
        )

    if firecrawl is not None and firecrawl.available:
        query = f"{target.name or target.ticker} {year} 年年度报告 PDF 巨潮资讯"
        results: list[dict[str, Any]] = []
        try:
            results = firecrawl.search(query, limit=5)
        except FirecrawlUnavailable:
            results = []
        for item in results:
            link = str(item.get("url") or "")
            if not link:
                continue
            if link.lower().endswith(".pdf"):
                return Resolved(url=link, title=str(item.get("title") or ""), method="firecrawl-search")
            pdf_link = None
            try:
                pdf_link = firecrawl.find_pdf_link(link)
            except FirecrawlError:
                pdf_link = None
            if pdf_link:
                return Resolved(
                    url=pdf_link,
                    title=str(item.get("title") or ""),
                    method="firecrawl-page",
                )

    detail = f"检索本身也失败了：{search_error}" if search_error else f"检索到 {len(announcements)} 条公告，都不是年报正文"
    raise DownloadError(
        f"{target.ticker} {year} 没找到年报（板块 {target.cninfo_column or '未指定'}）：{detail}\n"
        "    常见原因：① 该年度的报告在次年披露，years 要写披露年份；"
        "② 板块写错（market/column）；③ 巨潮接口改版。"
        "也可以直接在 config/targets.csv 里填 annual_report_url。"
    )


def fetch_one(
    target: Target,
    year: int,
    *,
    firecrawl: Firecrawl | None = None,
    force: bool = False,
    out_dir: Path | None = None,
    transport: Any = None,
) -> FetchOutcome:
    """下载一家公司一年的年报。已经下过（同名文件 + 合法 PDF）就跳过。"""
    destination = pdf_path(target.ticker, year, out_dir)
    if destination.exists() and not force:
        size = destination.stat().st_size
        if size > 1024:
            return FetchOutcome(
                code=target.ticker,
                year=year,
                ok=True,
                path=str(destination),
                method="cached",
                bytes=size,
                sha256=pdf_sha256(destination),
                note="本地已有同名文件，跳过下载（--force 可强制重下）",
                fetched_at=_now(),
            )

    try:
        resolved = resolve_report(target, year, firecrawl=firecrawl, transport=transport)
    except DownloadError as err:
        return FetchOutcome(code=target.ticker, year=year, ok=False, note=str(err), fetched_at=_now())

    try:
        path, size = download(resolved.url, destination)
    except HttpError as err:
        return FetchOutcome(
            code=target.ticker,
            year=year,
            ok=False,
            url=resolved.url,
            title=resolved.title,
            method=resolved.method,
            note=f"下载失败：{err}",
            fetched_at=_now(),
        )

    head = path.read_bytes()[:8] if path.exists() else b""
    if not head.startswith(PDF_MAGIC):
        # 拿到的多半是 HTML（登录页 / 反爬页）。删掉，免得后面抽取报「不是 PDF」时
        # 没人知道是这个环节出的问题。
        try:
            path.unlink()
        except OSError:
            pass
        return FetchOutcome(
            code=target.ticker,
            year=year,
            ok=False,
            url=resolved.url,
            title=resolved.title,
            method=resolved.method,
            note=(
                "下到的不是 PDF（前几个字节不是 %PDF）—— 多半被重定向到登录/反爬页。"
                "核对该 URL 在浏览器里能否直接打开。"
            ),
            fetched_at=_now(),
        )

    return FetchOutcome(
        code=target.ticker,
        year=year,
        ok=True,
        path=str(path),
        url=resolved.url,
        title=resolved.title,
        method=resolved.method,
        bytes=size,
        sha256=pdf_sha256(path),
        candidates=resolved.candidates,
        fetched_at=_now(),
    )


def fetch_all(
    targets: Sequence[Target],
    *,
    years: Sequence[int] | None = None,
    firecrawl: Firecrawl | None = None,
    force: bool = False,
    out_dir: Path | None = None,
    transport: Any = None,
    progress: Any = None,
) -> list[FetchOutcome]:
    """按「公司 × 年份」全量下载。years 传了就只下这些年份。"""
    outcomes: list[FetchOutcome] = []
    for target in targets:
        wanted = tuple(years) if years else target.years_desc
        if not wanted:
            outcomes.append(
                FetchOutcome(
                    code=target.ticker,
                    year=0,
                    ok=False,
                    note="清单里没写 years，不知道要下哪几年",
                    fetched_at=_now(),
                )
            )
            continue
        for year in wanted:
            outcome = fetch_one(
                target,
                year,
                firecrawl=firecrawl,
                force=force,
                out_dir=out_dir,
                transport=transport,
            )
            outcomes.append(outcome)
            if progress is not None:
                progress(outcome)
    return outcomes


def import_local(
    files: Iterable[str | Path], *, out_dir: Path | None = None, copy: bool = True
) -> list[FetchOutcome]:
    """手工下载的 PDF 入库：认文件名 `<代码>_<年份>.pdf`，登记哈希。

    为什么需要它：有些年报在交易所官网要过验证码才能下，人工下载反而更快。
    人工下载的文件同样要进 manifest —— 不然报告里就说不清它的出处。
    """
    import shutil

    outcomes: list[FetchOutcome] = []
    folder = out_dir or PATHS.raw_pdf
    folder.mkdir(parents=True, exist_ok=True)
    for raw in files:
        source = Path(raw)
        if not source.is_file():
            outcomes.append(
                FetchOutcome(code="", year=0, ok=False, note=f"文件不存在：{source}", fetched_at=_now())
            )
            continue
        parts = source.stem.rsplit("_", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            outcomes.append(
                FetchOutcome(
                    code="",
                    year=0,
                    ok=False,
                    note=f"文件名不符合 <代码>_<年份>.pdf：{source.name}",
                    fetched_at=_now(),
                )
            )
            continue
        code, year = parts[0], int(parts[1])
        destination = pdf_path(code, year, folder)
        if source.resolve() != destination.resolve() and copy:
            shutil.copy2(source, destination)
        elif source.resolve() != destination.resolve():
            destination = source
        if not destination.read_bytes()[:8].startswith(PDF_MAGIC):
            outcomes.append(
                FetchOutcome(
                    code=code,
                    year=year,
                    ok=False,
                    path=str(destination),
                    note="不是 PDF（前几个字节不是 %PDF）",
                    fetched_at=_now(),
                )
            )
            continue
        outcomes.append(
            FetchOutcome(
                code=code,
                year=year,
                ok=True,
                path=str(destination),
                method="imported",
                bytes=destination.stat().st_size,
                sha256=pdf_sha256(destination),
                note="手工入库",
                fetched_at=_now(),
            )
        )
    return outcomes


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    target = path or PATHS.manifest_json
    if not target.is_file():
        return {"version": 1, "updated_at": "", "entries": {}}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"version": 1, "updated_at": "", "entries": {}}
    data.setdefault("entries", {})
    return data


def save_manifest(outcomes: Sequence[FetchOutcome], path: Path | None = None) -> Path:
    """把这一批结果并进 manifest。同一家同一年只留最新一条。"""
    target = path or PATHS.manifest_json
    data = load_manifest(target)
    entries: dict[str, Any] = data["entries"]
    for outcome in outcomes:
        if not outcome.code or not outcome.year:
            continue
        entries[f"{outcome.code}_{outcome.year}"] = outcome.to_dict()
    data["updated_at"] = _now()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def is_pdf_url(url: str) -> bool:
    """只探前几个字节，判断一个链接到底给的是不是 PDF。

    用途：年报直链经常在链接里写着 .PDF，实际返回一个 HTML 跳转页。
    下载前先探一下，能省一次白下载。
    """
    try:
        head = get_bytes(url, headers={"Range": "bytes=0-7"}, expected=(200, 206))
    except HttpError:
        return False
    return bytes(head[:8]).startswith(PDF_MAGIC)
