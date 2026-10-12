"""巨潮资讯网（cninfo）年报检索：公司 → 年报公告 → PDF 直链。

为什么是巨潮
    沪深北交易所的法定信息披露平台，年报原文免费、可直链、不需要登录，
    而且有稳定的查询接口（`/new/hisAnnouncement/query`）。
    上交所/深交所官网也能查，但页面结构改版频繁，抓取维护成本更高。

接口形状（2026-10 实测）
    POST https://www.cninfo.com.cn/new/hisAnnouncement/query
        column=szse|sse|bj|hke    板块
        category=category_ndbg_szsh   「年度报告」这一类
        stock=<code>,<orgId>        指定公司（orgId 可省）
        seDate=2024-01-01~2024-12-31
    → JSON {"announcements": [{...}], "totalAnnouncement": n}
    每条公告的 `adjunctUrl` 拼到 https://static.cninfo.com.cn/ 就是 PDF 直链。

网络调用全部经过 `transport` 参数，默认走 finai_web.http；测试里换成假实现，
这样「解析逻辑」不需要联网就能验。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .http import HttpError, post_form

BASE = "https://www.cninfo.com.cn"
STATIC_BASE = "https://static.cninfo.com.cn"
QUERY_URL = f"{BASE}/new/hisAnnouncement/query"
CATEGORY_ANNUAL_REPORT = "category_ndbg_szsh"

Transport = Callable[..., Any]

# 标题里出现这些词的不是正文年报，检索时要排掉。
# 「摘要」排掉是因为摘要里没有完整三表；「英文」排掉是因为科目名对不上中文表；
# 「更正 / 取消 / 补充」排掉是因为同一份年报可能有两版，容易抽到半份。
EXCLUDE_TITLE_WORDS = (
    "摘要",
    "英文",
    "English",
    "更正",
    "取消",
    "补充",
    "问询",
    "提示性",
    "预约",
    "标题为",
)


class CninfoError(RuntimeError):
    """检索失败。消息要能直接讲给使用者听。"""


@dataclass(frozen=True)
class Announcement:
    code: str
    name: str
    title: str
    date: str
    adjunct: str
    announcement_id: str = ""
    org_id: str = ""

    @property
    def pdf_url(self) -> str:
        return pdf_url(self.adjunct)

    @property
    def detail_url(self) -> str:
        if not self.announcement_id:
            return ""
        return (
            f"{BASE}/new/disclosure/detail?stockCode={self.code}"
            f"&announcementId={self.announcement_id}&orgId={self.org_id}"
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "name": self.name,
            "title": self.title,
            "date": self.date,
            "pdf_url": self.pdf_url,
            "detail_url": self.detail_url,
        }


def pdf_url(adjunct: str) -> str:
    """`finalpage/2025-04-01/xxx.PDF` → 巨潮静态站直链。"""
    return f"{STATIC_BASE}/" + str(adjunct or "").lstrip("/")


def _clean_title(raw: str) -> str:
    """标题里常带 <em> 高亮标签，去掉后再判断。"""
    return (
        str(raw or "")
        .replace("<em>", "")
        .replace("</em>", "")
        .strip()
    )


def query(
    *,
    code: str,
    column: str,
    year: int,
    org_id: str = "",
    page_size: int = 30,
    transport: Transport | None = None,
) -> list[Announcement]:
    """查一家公司某一年的全部「年度报告」类公告（含摘要、更正等）。"""
    sender = transport or post_form
    stock = f"{code},{org_id}" if org_id else code
    form: dict[str, Any] = {
        "pageNum": 1,
        "pageSize": page_size,
        "column": column,
        "tabName": "fulltext",
        "plate": "",
        "stock": stock,
        "searchkey": "",
        "secid": "",
        "category": CATEGORY_ANNUAL_REPORT,
        "trade": "",
        "seDate": f"{year}-01-01~{year}-12-31",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    headers = {
        # 巨潮对没有 Referer/UA 的请求会返回空结果，这两行是必需的
        "Referer": f"{BASE}/new/commonUrl?url=disclosure/list/notice",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    try:
        payload = sender(QUERY_URL, form, headers=headers, expected=(200,))
    except HttpError as err:
        raise CninfoError(
            f"巨潮检索失败（{code} {year}）：{err}\n"
            "    常见原因：网络不可达 / 被限流。稍后重试，或改用 --url 直接给 PDF 直链。"
        ) from err

    if not isinstance(payload, Mapping):
        raise CninfoError(f"巨潮返回的不是 JSON：{str(payload)[:120]}")

    items = payload.get("announcements") or []
    results: list[Announcement] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        results.append(
            Announcement(
                code=str(item.get("secCode") or code),
                name=str(item.get("secName") or ""),
                title=_clean_title(item.get("announcementTitle") or ""),
                date=str(item.get("announcementTime") or ""),
                adjunct=str(item.get("adjunctUrl") or ""),
                announcement_id=str(item.get("announcementId") or ""),
                org_id=str(item.get("orgId") or org_id),
            )
        )
    return results


def pick_annual_report(
    announcements: Sequence[Announcement], year: int
) -> Announcement | None:
    """从检索结果里挑出「那一年」的年报正文。

    评分而不是过滤：巨潮的标题写法各年不一（「2024 年年度报告」/「2024年度报告」/
    「2024 年年度报告（修订版）」）。硬匹配字符串会在某一年突然抽不到，
    评分则总能挑出最像的那一份，并且把「次优候选」留给调用方写进日志。
    """
    best: tuple[int, Announcement] | None = None
    for item in announcements:
        title = item.title
        if not item.adjunct:
            continue
        if any(word in title for word in EXCLUDE_TITLE_WORDS):
            continue
        if "年度报告" not in title and "年报" not in title:
            continue
        score = 0
        if f"{year}" in title:
            score += 4
        else:
            # 年份对不上：可能是上一年度的报告在这一年披露，降权但仍然保留
            score -= 2
        if "年度报告" in title:
            score += 3
        if title.endswith("年度报告"):
            score += 2
        if "修订" in title or "更新" in title:
            score -= 1
        if item.adjunct.upper().endswith(".PDF"):
            score += 1
        if best is None or score > best[0]:
            best = (score, item)
    return best[1] if best else None


def candidates(announcements: Sequence[Announcement], year: int) -> list[Announcement]:
    """按「像正文的程度」排序的全部候选，供日志与人工核对。"""
    scored: list[tuple[int, Announcement]] = []
    for item in announcements:
        if any(word in item.title for word in EXCLUDE_TITLE_WORDS):
            continue
        if "年度报告" not in item.title and "年报" not in item.title:
            continue
        score = (4 if str(year) in item.title else 0) + (3 if "年度报告" in item.title else 0)
        scored.append((score, item))
    scored.sort(key=lambda pair: -pair[0])
    return [item for _, item in scored]
