"""Firecrawl 客户端：公告页抓取、PDF 解析、结构化抽取。

用在哪两处
    1. 兜底找 PDF —— 巨潮检索不到（接口改版、公司停牌、境外披露）时，
       用 Firecrawl 搜一次公告页，从页面里把 PDF 链接捞出来。
    2. 兜底抽取 —— PDF 是扫描件（没有文字层），或者本地没有 PDF 解析库时，
       让 Firecrawl 解析 PDF 并按 schema 返回 JSON。

为什么不是「全都走 Firecrawl」
    本地能抽就本地抽：不花钱、不依赖网络、结果可重复（同样的 PDF 永远同样的输出）。
    Firecrawl 是**兜底**，每次用都会在记录里写明 `method=firecrawl-*`，
    报告里也就能区分「这个数是从 PDF 文本里读的」还是「模型读出来的」。

密钥
    `FIRECRAWL_API_KEY`（可写在仓库根目录的 .env，见 .env.example）。
    没有密钥时所有接口抛 FirecrawlUnavailable —— 报错要能立刻告诉人「你缺的是哪一步」，
    而不是抛一个 401 让人去猜。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Mapping, Sequence

from .config import firecrawl_api_key
from .http import HttpError, post_json

DEFAULT_BASE = "https://api.firecrawl.dev/v2"
FALLBACK_BASE = "https://api.firecrawl.dev/v1"

Transport = Callable[..., Any]


class FirecrawlError(RuntimeError):
    """调用失败。"""


class FirecrawlUnavailable(FirecrawlError):
    """没配密钥/没有网络 —— 属于「环境问题」，调用方可以选择降级而不是失败。"""


class Firecrawl:
    """最小可用客户端。只实现本项目真正用到的三个动作。"""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base: str = DEFAULT_BASE,
        transport: Transport | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.api_key = (api_key if api_key is not None else firecrawl_api_key()) or ""
        self.base = base.rstrip("/")
        self._transport = transport or post_json
        self.timeout = timeout

    # -- 基础设施 ---------------------------------------------------------

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _require_key(self) -> None:
        if not self.api_key:
            raise FirecrawlUnavailable(
                "没有配置 FIRECRAWL_API_KEY。\n"
                "    两种处理：① 在仓库根目录的 .env 里填 FIRECRAWL_API_KEY=... ；\n"
                "             ② 不用 Firecrawl 兜底，只跑本地路径"
                "（本地路径需要 pip install pdfplumber）。"
            )

    def _call(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self._require_key()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base}{path}"
        try:
            response = self._transport(url, dict(payload), headers=headers, expected=(200, 201))
        except HttpError as err:
            if err.status in (401, 403):
                raise FirecrawlError(
                    f"Firecrawl 拒绝了这次请求（HTTP {err.status}）：密钥无效或额度用尽。"
                ) from err
            if err.status in (400, 404) and self.base.endswith("/v2"):
                # 老账号可能还只有 v1 端点。降级重试一次，并记下来。
                return self._call_v1(path, payload)
            raise FirecrawlError(f"Firecrawl 调用失败：{err}") from err

        if not isinstance(response, Mapping):
            raise FirecrawlError(f"Firecrawl 返回的不是 JSON 对象：{str(response)[:120]}")
        if response.get("success") is False:
            raise FirecrawlError(
                f"Firecrawl 返回失败：{response.get('error') or response}"
            )
        return response

    def _call_v1(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """v2 端点不可用时的老端点。形状差异只在 formats 上，处理掉即可。"""
        legacy = dict(payload)
        formats = legacy.get("formats")
        if isinstance(formats, Sequence) and not isinstance(formats, str):
            simple: list[str] = []
            json_format: Mapping[str, Any] | None = None
            for item in formats:
                if isinstance(item, Mapping):
                    json_format = item
                    simple.append("json")
                else:
                    simple.append(str(item))
            legacy["formats"] = simple
            if json_format is not None:
                legacy["jsonOptions"] = {
                    "schema": json_format.get("schema"),
                    "prompt": json_format.get("prompt"),
                }
        url = f"{FALLBACK_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self._transport(url, legacy, headers=headers, expected=(200, 201))
        except HttpError as err:
            raise FirecrawlError(
                f"Firecrawl v1 兜底也失败了：{err}\n"
                "    可能是版本不匹配，请核对 https://docs.firecrawl.dev 当前的接口形状。"
            ) from err
        return response  # type: ignore[return-value]

    # -- 三个动作 ---------------------------------------------------------

    def scrape(
        self,
        url: str,
        *,
        formats: Sequence[Any] | None = None,
        parsers: Sequence[str] | None = None,
        only_main_content: bool = True,
        wait_for: int | None = None,
        max_age: int | None = None,
        retries: int = 2,
    ) -> dict[str, Any]:
        """抓一个 URL，返回 {"markdown", "json", "metadata", "links"}。"""
        body: dict[str, Any] = {
            "url": url,
            "formats": list(formats or ["markdown"]),
            "onlyMainContent": only_main_content,
        }
        if parsers:
            body["parsers"] = list(parsers)
        if wait_for is not None:
            body["waitFor"] = wait_for
        if max_age is not None:
            body["maxAge"] = max_age

        last: Exception | None = None
        for attempt in range(1, max(retries, 1) + 1):
            try:
                response = self._call("/scrape", body)
                return _unwrap(response)
            except FirecrawlError as err:
                last = err
                if attempt < retries:
                    time.sleep(min(2.0 ** attempt, 8.0))
        raise last if last else FirecrawlError("Firecrawl scrape 失败")

    def extract(
        self,
        url: str,
        schema: Mapping[str, Any],
        *,
        prompt: str = "抽取报表科目：科目名、数值、页码、原文片段",
        pdf: bool = True,
    ) -> dict[str, Any]:
        """按 schema 抽结构化 JSON。PDF 直链要把 parsers 打开，否则拿到的是乱码。"""
        formats: list[Any] = [
            {"type": "json", "schema": dict(schema), "prompt": prompt},
            "markdown",
        ]
        data = self.scrape(url, formats=formats, parsers=["pdf"] if pdf else None)
        payload = data.get("json")
        if not isinstance(payload, Mapping):
            raise FirecrawlError(
                f"Firecrawl 没有返回结构化结果（url={url}）。"
                f"返回字段：{sorted(data)} —— 可能是 PDF 解析失败，改用本地 pdfplumber 试试。"
            )
        return dict(payload)

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """网页搜索，用来在没有直链时找公告页。"""
        response = self._call("/search", {"query": query, "limit": limit})
        data = response.get("data")
        if isinstance(data, Mapping):
            results = data.get("web") or data.get("results") or []
        elif isinstance(data, Sequence):
            results = list(data)
        else:
            results = []
        return [dict(item) for item in results if isinstance(item, Mapping)]

    def find_pdf_link(self, page_url: str) -> str | None:
        """从公告页里找 PDF 直链。找不到返回 None，让调用方走别的路。"""
        data = self.scrape(page_url, formats=["links", "markdown"])
        for link in data.get("links") or []:
            text = str(link)
            if text.lower().endswith(".pdf") and "static.cninfo.com.cn" in text:
                return text
        for link in data.get("links") or []:
            text = str(link)
            if text.lower().endswith(".pdf"):
                return text
        return None


def _unwrap(response: Mapping[str, Any]) -> dict[str, Any]:
    """把 {"success":true,"data":{...}} 拆平；已经是平的就原样返回。"""
    data = response.get("data")
    if not isinstance(data, Mapping):
        return dict(response)
    result: dict[str, Any] = dict(data)
    for key in ("json", "extract", "markdown"):
        if key in data and data[key] is not None:
            result[key] = data[key]
    return result
