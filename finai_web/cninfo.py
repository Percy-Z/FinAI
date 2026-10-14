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

import re
from datetime import datetime
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
    year: int | None = None,
    org_id: str = "",
    page_size: int = 30,
    transport: Transport | None = None,
) -> list[Announcement]:
    """查一家公司的「年度报告」类公告（含摘要、更正等）。

    `year` 是**披露年份**（2025 年年报在 2026 年披露，就传 2026），传了就给
    seDate 加上这一年的区间。传 None 表示不限年份 —— 此时接口按披露时间倒序
    返回，最新一份年报就在最前面，用来回答「这家最近一年出了哪份年报」
    （2026-10-14 实测：长春高新 total=55，第一条就是 2025 年年度报告）。
    """
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
        "seDate": f"{year}-01-01~{year}-12-31" if year else "",
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
            f"巨潮检索失败（{code} {year or '全部年份'}）：{err}\n"
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
        if not looks_like_annual_report(item):
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
        if not looks_like_annual_report(item):
            continue
        score = (4 if str(year) in item.title else 0) + (3 if "年度报告" in item.title else 0)
        scored.append((score, item))
    scored.sort(key=lambda pair: -pair[0])
    return [item for _, item in scored]


def looks_like_annual_report(item: Announcement) -> bool:
    """标题像不像「年报正文」。

    抽成一个函数是因为它有**两个**调用方：按年份打分挑（`pick_annual_report`）
    与按披露时间取最新（`pick_latest`）。两处各写一遍过滤条件，迟早会有一处
    忘了排掉「摘要」—— 摘要里没有完整三表，抽出来是半份数据。
    """
    title = item.title
    if any(word in title for word in EXCLUDE_TITLE_WORDS):
        return False
    return "年度报告" in title or "年报" in title


# 巨潮的附件路径带披露日期：finalpage/2026-04-22/1225135424.PDF
_DATE_IN_PATH = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def disclosure_year(item: Announcement) -> int | None:
    """这份公告的**披露年份**（不是报表所属年度）。

    为什么必须是披露年份：定标清单里的 `years` 一直就是披露年份的口径
    （2025 年年报在 2026 年披露，写 2026），检索接口的 seDate 也按披露日期过滤。
    两种格式都认：附件路径里的日期优先，退到 announcementTime（毫秒时间戳）。
    两个都认不出来时返回 None —— 宁可不填，也不猜一个年份出来。
    """
    match = _DATE_IN_PATH.search(str(item.adjunct or ""))
    if match:
        return int(match.group(1))
    raw = str(item.date or "").strip()
    if raw.isdigit() and len(raw) >= 10:
        try:
            return datetime.fromtimestamp(int(raw[:13]) / 1000).year
        except (OverflowError, OSError, ValueError):
            return None
    match = re.search(r"(19|20)\d{2}", raw)
    return int(match.group(0)) if match else None


def _date_key(item: Announcement) -> tuple[int, int]:
    """排序用的披露时间。时间戳拿不到就退回附件路径里的日期。"""
    raw = str(item.date or "").strip()
    if raw.isdigit() and len(raw) >= 10:
        return int(raw[:13]), 0
    match = _DATE_IN_PATH.search(str(item.adjunct or ""))
    if match:
        try:
            return int(datetime.strptime(match.group(0), "%Y-%m-%d").timestamp() * 1000), 0
        except ValueError:
            return 0, 0
    return 0, 0


def pick_latest(announcements: Sequence[Announcement]) -> Announcement | None:
    """不限年份检索时，挑出最近披露的那份年报正文。

    与 `pick_annual_report` 的分工：那个是「给定年份，挑最像这一年的正文」，
    靠标题打分；这个是「不知道年份，先问接口有哪些，再按披露时间取最新」。
    有了它，定标清单里的 `years` 就不用人工填了。
    """
    items = [
        item for item in announcements if item.adjunct and looks_like_annual_report(item)
    ]
    if not items:
        return None
    items.sort(
        key=lambda item: (_date_key(item), item.adjunct.upper().endswith(".PDF")),
        reverse=True,
    )
    return items[0]


def latest_annual_reports(
    *,
    code: str,
    column: str,
    org_id: str = "",
    page_size: int = 10,
    transport: Transport | None = None,
) -> list[Announcement]:
    """不限年份检索，按披露时间倒序返回年报类公告（最新的在最前）。"""
    found = query(
        code=code,
        column=column,
        year=None,
        org_id=org_id,
        page_size=page_size,
        transport=transport,
    )
    items = [item for item in found if looks_like_annual_report(item)]
    items.sort(key=lambda item: _date_key(item), reverse=True)
    return items
