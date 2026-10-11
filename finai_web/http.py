"""最小 HTTP 客户端：标准库实现，带重试与浏览器 UA。

在 Windows 上优先用 Python 自带证书；若目标站点证书链异常，
回退到 `ssl` 默认上下文（不静默关闭校验）。
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Mapping

from .config import http_retries, http_timeout

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 finai-web/0.1"
)


class HttpError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


def _context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    form: Mapping[str, Any] | None = None,
    json_body: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
    retries: int | None = None,
    expected: Iterable[int] = (200,),
) -> bytes:
    """发一个请求并返回响应体；失败按次数重试。"""
    if form is not None:
        data = urllib.parse.urlencode(form, doseq=True).encode("utf-8")
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")

    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if json_body is not None:
        hdrs["Content-Type"] = "application/json"
    elif form is not None:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    if headers:
        hdrs.update({k: v for k, v in headers.items() if v is not None})

    attempts = retries if retries is not None else http_retries()
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
        try:
            with urllib.request.urlopen(
                req, timeout=timeout or http_timeout(), context=_context()
            ) as resp:
                body = resp.read()
                if resp.status not in set(expected):
                    raise HttpError(
                        f"HTTP {resp.status} for {url}", status=resp.status, url=url
                    )
                return body
        except urllib.error.HTTPError as exc:  # 4xx/5xx
            last_error = HttpError(
                f"HTTP {exc.code} for {url}", status=exc.code, url=url
            )
            if exc.code in (400, 401, 403, 404, 422):
                break  # 重试没有意义
        except Exception as exc:  # noqa: BLE001 - 网络/证书/超时
            last_error = exc
        if attempt < attempts:
            time.sleep(min(2.0 ** (attempt - 1), 8.0))
    raise HttpError(f"request failed: {url} ({last_error})", url=url) from last_error


def get_bytes(url: str, **kwargs: Any) -> bytes:
    return request(url, **kwargs)


def get_text(url: str, *, encoding: str = "utf-8", **kwargs: Any) -> str:
    return request(url, **kwargs).decode(encoding, errors="replace")


def get_json(url: str, **kwargs: Any) -> Any:
    return json.loads(request(url, **kwargs).decode("utf-8", errors="replace"))


def post_form(url: str, form: Mapping[str, Any], **kwargs: Any) -> Any:
    body = request(url, method="POST", form=form, **kwargs)
    text = body.decode("utf-8", errors="replace").strip()
    return json.loads(text) if text.startswith(("{", "[")) else text


def post_json(url: str, payload: Any, **kwargs: Any) -> Any:
    body = request(url, method="POST", json_body=payload, **kwargs)
    return json.loads(body.decode("utf-8", errors="replace"))


def download(url: str, dest_path, *, chunk: int = 1 << 16, **kwargs: Any):
    """流式下载到文件，返回 (路径, 字节数)。"""
    from pathlib import Path

    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    hdrs.update(kwargs.pop("headers", {}) or {})
    req = urllib.request.Request(url, headers=hdrs)
    total = 0
    with urllib.request.urlopen(
        req, timeout=kwargs.pop("timeout", None) or http_timeout(), context=_context()
    ) as resp, dest.open("wb") as fh:
        while True:
            block = resp.read(chunk)
            if not block:
                break
            fh.write(block)
            total += len(block)
    return dest, total
