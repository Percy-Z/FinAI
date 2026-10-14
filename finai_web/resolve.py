"""公司名 → 巨潮的证券代码与机构号。

为什么需要这一层
    取数链路的入口是定标清单（`config/targets.csv`），那里面必须写六位代码与
    巨潮机构号（`org_id`）—— 而人手里只有公司名字。以前这一步是人在浏览器里
    手工查出来再填进 CSV 的，整条链路自动化就卡在这里。

接口形状（2026-10-14 实测）
    POST https://www.cninfo.com.cn/new/information/topSearch/query
        keyWord=<中文名、简称或代码>    maxNum=10
    → [{"code":"000661","pinyin":"ccgx","category":"A股","type":"shj",
        "delisted":"false","orgId":"gssz0000661","zwjc":"长春高新"}]

    实测：中文名可以模糊匹配（「长春」返回长春高新、长春一东、长春燃气三家），
    港股也认得（「腾讯控股」→ 00700 / gshk0000700）。

⚠️ 一个容易踩的点：`type` 对 A 股是 `shj`（沪、深、京放在一起），**不能**用它
   区分沪市深市 —— 板块一律按代码前缀判定（与 `targets.py` 的 `cninfo_column`
   同一套规则）。港股用 `type == "hke"` 或 `category == "港股"` 判定。

网络调用全部经过 `transport` 参数（与 `cninfo.py` 同一约定），测试里换成假实现，
所以「解析与板块判定」不需要联网就能验。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .http import HttpError, post_form

SEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"

Transport = Callable[..., Any]


class ResolveError(RuntimeError):
    """按名称检索失败。消息要能直接讲给使用者听。"""


def cninfo_column(code: str, category: str = "", type_code: str = "") -> str:
    """巨潮公告接口要的 `column` 值。

    为什么不用接口给的 `type`：A 股的 `type` 是 `shj`（沪、深、京一个值），
    拿它去查公告会查不出深市公司。按代码前缀判定才准。

    北交所那一支（4/8 开头与 920 开头）**没有实测过** —— 首次真跑到北交所时
    要核对一次，并在 `finai_web/README.md` 记一笔。
    """
    if "港股" in str(category or "") or str(type_code or "").lower() == "hke":
        return "hke"
    text = str(code or "").strip()
    if text.startswith("92"):  # 北交所新号段，必须排在 9 开头（沪 B 股）之前
        return "bj"
    if text.startswith("6") or text.startswith("9"):  # 600/688 沪市；900 沪 B 股
        return "sse"
    if text.startswith(("0", "2", "3")):  # 000/002/300 深市；200 深 B 股
        return "szse"
    if text.startswith(("4", "8")):  # 43/83/87/88 北交所
        return "bj"
    # 认不出的前缀：按深市试一次。真错了会在下载那一步报「没有检索到年度报告」，
    # 比在这里猜一个更响的错要好 —— 这一步只是给个默认值。
    return "szse"


@dataclass(frozen=True)
class Candidate:
    """一家候选公司。字段名与巨潮接口的返回一一对应，方便对账。"""

    code: str
    name: str
    org_id: str = ""
    category: str = ""
    type_code: str = ""
    pinyin: str = ""
    delisted: bool = False

    @property
    def market(self) -> str:
        """CN / HK —— 与 `targets.py` 的 market 列同一个取值空间。"""
        return "HK" if self.cninfo_column == "hke" else "CN"

    @property
    def cninfo_column(self) -> str:
        return cninfo_column(self.code, self.category, self.type_code)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "org_id": self.org_id,
            "category": self.category,
            "market": self.market,
            "cninfo_column": self.cninfo_column,
            "pinyin": self.pinyin,
        }

    def search_key(self) -> str:
        """巨潮检索要的 `stock` 值。有机构号就带上 —— 不带会多一次模糊匹配，
        而且代码相同、市场不同的情况下可能匹配到别家。"""
        return f"{self.code},{self.org_id}" if self.org_id else self.code


def _is_delisted(value: Any) -> bool:
    return str(value or "").strip().lower() == "true"


def search(
    keyword: str,
    *,
    max_num: int = 10,
    transport: Transport | None = None,
) -> list[Candidate]:
    """按名称（或代码）检索候选公司。查不到返回空列表，不抛异常。

    空列表要由调用方转成一句能照着做的话（见 `hint_for_empty`）——
    界面上「查不到」和「程序坏了」必须看得出区别。
    """
    text = str(keyword or "").strip()
    if not text:
        return []
    sender = transport or post_form
    form = {"keyWord": text, "maxNum": max(1, int(max_num))}
    headers = {
        "Referer": "https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    try:
        payload = sender(SEARCH_URL, form, headers=headers, expected=(200,))
    except HttpError as err:
        raise ResolveError(
            f"按名称检索失败（{text}）：{err}\n"
            "    常见原因：网络不可达 / 被限流。稍后重试，或直接用六位代码检索。"
        ) from err

    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise ResolveError(f"巨潮检索返回的不是列表：{str(payload)[:120]}")

    results: list[Candidate] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("code") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        results.append(
            Candidate(
                code=code,
                name=str(item.get("zwjc") or "").strip(),
                org_id=str(item.get("orgId") or "").strip(),
                category=str(item.get("category") or "").strip(),
                type_code=str(item.get("type") or "").strip(),
                pinyin=str(item.get("pinyin") or "").strip(),
                delisted=_is_delisted(item.get("delisted")),
            )
        )
    return results


def active(candidates: Sequence[Candidate]) -> list[Candidate]:
    """剔掉已退市的。退市公司没有可用的年报，留在候选里只会让人点错。"""
    return [item for item in candidates if not item.delisted]


def hint_for_empty(keyword: str) -> str:
    """查不到公司时给的那句话 —— 要能照着做，不能只说「没有结果」。

    最常见的两种「查不到」：公司没上市（比如长春高新的子公司金赛药业，
    它没有自己的年报），或者输入的是全称而巨潮只认简称。
    """
    text = str(keyword or "").strip()
    return (
        f"巨潮里没有查到「{text}」。两种常见情况：\n"
        "    ① 这家公司没有上市（子公司、未公开募集的公司都没有自己的年报）——"
        "请改用它的上市母公司；\n"
        "    ② 输入的是公司全称，而巨潮只认简称 —— 换成简称或用六位代码再试。"
    )


@dataclass
class Resolution:
    """一次检索的完整结果，供界面显示与日志留痕。"""

    keyword: str
    candidates: list[Candidate] = field(default_factory=list)
    hint: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.candidates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "keyword": self.keyword,
            "ok": self.ok,
            "hint": self.hint,
            "candidates": [item.to_dict() for item in self.candidates],
        }


def resolve(
    keyword: str, *, max_num: int = 10, transport: Transport | None = None
) -> Resolution:
    """检索并把结果包成界面与日志要的形状。"""
    found = active(search(keyword, max_num=max_num, transport=transport))
    return Resolution(
        keyword=str(keyword or "").strip(),
        candidates=found,
        hint="" if found else hint_for_empty(keyword),
    )
