"""按名称检索、清单模型、取价三块的测试。

全部不联网：网络调用走假 transport（与 cninfo/downloader 同一套约定）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from finai_web import cninfo, company, quotes, resolve
from finai_web import targets as targets_mod
from finai_web.http import HttpError

# 巨潮按名称检索的真实返回（2026-10-14 实测，摘录）
CANNED_SEARCH = [
    {
        "code": "000661",
        "pinyin": "ccgx",
        "sjstsBond": "false",
        "category": "A股",
        "type": "shj",
        "delisted": "false",
        "orgId": "gssz0000661",
        "zwjc": "长春高新",
    },
    {
        "code": "600148",
        "pinyin": "ccyd",
        "category": "A股",
        "type": "shj",
        "delisted": "true",
        "orgId": "gssh0600148",
        "zwjc": "长春一东",
    },
    {
        "code": "00700",
        "pinyin": "txkg",
        "category": "港股",
        "type": "hke",
        "delisted": "false",
        "orgId": "gshk0000700",
        "zwjc": "腾讯控股",
    },
]


def fake_search(payload, *, error: str = ""):
    def transport(url, form=None, **kwargs):
        if error:
            raise HttpError(error)
        return payload

    return transport


class ResolveTest(unittest.TestCase):
    """公司名 → 代码 + 机构号。这一步以前是人在浏览器里手工查的。"""

    def test_检索结果变成候选并判出板块(self) -> None:
        found = resolve.search("长春", transport=fake_search(CANNED_SEARCH))
        self.assertEqual([item.code for item in found], ["000661", "600148", "00700"])
        # 板块按代码前缀判，不看接口的 type（A 股的 type 一律是 shj，区分不出沪深）
        self.assertEqual(found[0].cninfo_column, "szse")
        self.assertEqual(found[0].market, "CN")
        self.assertEqual(found[0].org_id, "gssz0000661")
        self.assertEqual(found[0].search_key(), "000661,gssz0000661")
        self.assertEqual(found[2].market, "HK")
        self.assertEqual(found[2].cninfo_column, "hke")

    def test_退市的被剔掉(self) -> None:
        found = resolve.active(resolve.search("长春", transport=fake_search(CANNED_SEARCH)))
        self.assertNotIn("600148", [item.code for item in found])

    def test_重复代码只留一条(self) -> None:
        doubled = [CANNED_SEARCH[0], dict(CANNED_SEARCH[0])]
        found = resolve.search("长春高新", transport=fake_search(doubled))
        self.assertEqual(len(found), 1)

    def test_查不到时给出能照做的提示(self) -> None:
        result = resolve.resolve("金赛药业", transport=fake_search([]))
        self.assertFalse(result.ok)
        self.assertIn("没有上市", result.hint)
        self.assertIn("金赛药业", result.hint)

    def test_空关键词不联网(self) -> None:
        def boom(*args, **kwargs):
            raise AssertionError("空关键词不该发起请求")

        self.assertEqual(resolve.search("   ", transport=boom), [])

    def test_网络失败要带下一步(self) -> None:
        with self.assertRaises(resolve.ResolveError) as caught:
            resolve.search("长春高新", transport=fake_search(None, error="连接超时"))
        self.assertIn("连接超时", str(caught.exception))
        self.assertIn("六位代码", str(caught.exception))

    def test_返回的不是列表要报错(self) -> None:
        with self.assertRaises(resolve.ResolveError):
            resolve.search("长春高新", transport=fake_search({"code": "x"}))

    def test_板块前缀映射(self) -> None:
        cases = {
            "600519": "sse",
            "688278": "sse",
            "000661": "szse",
            "300009": "szse",
            "830799": "bj",
            "920001": "bj",
        }
        for code, expected in cases.items():
            self.assertEqual(resolve.cninfo_column(code), expected, code)
        self.assertEqual(resolve.cninfo_column("00700", "港股"), "hke")


class CompanyProfileTest(unittest.TestCase):
    """company.json 与它生成的 targets.csv。"""

    def _profile(self) -> company.CompanyProfile:
        target = company.CompanyRef.from_candidate(
            resolve.Candidate(code="000661", name="长春高新", org_id="gssz0000661"),
            years=company.default_years(2026, 2),
        )
        peers = [
            company.CompanyRef.from_candidate(
                resolve.Candidate(code="300009", name="安科生物", org_id="9900008310"),
                years=company.default_years(2026, 1),
            )
        ]
        return company.CompanyProfile(
            target=target, peers=peers, as_of="2026-09-30", resolved_from="长春高新"
        )

    def test_默认下最近两份年报(self) -> None:
        # 目标公司要两份：当年那份给资产负债表，上一份给利润表（归一要用）
        self.assertEqual(company.default_years(2026, 2), (2026, 2025))
        self.assertEqual(company.default_years(2026, 1), (2026,))
        self.assertEqual(self._profile().target.years, (2026, 2025))
        self.assertEqual(self._profile().peers[0].years, (2026,))

    def test_读写往返(self) -> None:
        profile = self._profile()
        with tempfile.TemporaryDirectory() as tmp:
            path = profile.save(Path(tmp) / "company.json")
            back = company.CompanyProfile.load(path)
        self.assertEqual(back.to_dict(), profile.to_dict())
        self.assertEqual(back.peer_codes, ["300009"])

    def test_生成的清单能被取数层直接读(self) -> None:
        """这条是集成检查：生成物必须与手填清单**同构**，否则取数那一侧要改代码。"""
        profile = self._profile()
        with tempfile.TemporaryDirectory() as tmp:
            path = profile.write_targets_csv(Path(tmp) / "targets.csv")
            items = targets_mod.read_targets(path)
        self.assertEqual([item.ticker for item in items], ["000661", "300009"])
        self.assertEqual(items[0].name, "长春高新")
        self.assertEqual(items[0].org_id, "gssz0000661")
        self.assertEqual(items[0].years_desc, (2026, 2025))
        self.assertEqual(items[0].cninfo_column, "szse")

    def test_没有年份要报错(self) -> None:
        target = company.CompanyRef(code="000661", name="长春高新")
        with self.assertRaises(company.CompanyError) as caught:
            company.CompanyProfile(target=target)
        self.assertIn("years", str(caught.exception))

    def test_可比公司不能是目标公司自己(self) -> None:
        target = company.CompanyRef(code="000661", years=(2026,))
        with self.assertRaises(company.CompanyError):
            company.CompanyProfile(target=target, peers=[target])

    def test_坏JSON要报错而不是抛原始异常(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "company.json"
            path.write_text("{ 这不是 JSON", encoding="utf-8")
            with self.assertRaises(company.CompanyError):
                company.CompanyProfile.load(path)


# 腾讯与东财的真实返回（2026-10-14 实测，长春高新 2026-09-30 收盘 71.83）
TENCENT_CANNED = {
    "code": 0,
    "msg": "",
    "data": {
        "sz000661": {
            "qfqday": [
                ["2026-09-29", "70.200", "70.550", "72.110", "70.080", "36016.000"],
                ["2026-09-30", "70.560", "71.830", "72.560", "70.210", "51879.000"],
            ]
        }
    },
}
EASTMONEY_CANNED = {
    "rc": 0,
    "data": {
        "code": "000661",
        "market": 0,
        "name": "长春高新",
        "klines": [
            "2026-09-29,70.20,70.55,72.11,70.08,36016,255430403.00",
            "2026-09-30,70.56,71.83,72.56,70.21,51879,372782467.99",
        ],
    },
}


def fake_quote_transport(tencent=TENCENT_CANNED, eastmoney=EASTMONEY_CANNED):
    """按 URL 分派到两个源的假实现。"""

    def transport(url, **kwargs):
        if "ifzq.gtimg.cn" in url:
            return tencent
        if "eastmoney.com" in url:
            return eastmoney
        raise AssertionError(f"没预料到的 URL：{url}")

    return transport


class QuotesTest(unittest.TestCase):
    """取价：两个源交叉核对，对不上就停下。"""

    def test_证券标识映射(self) -> None:
        self.assertEqual(quotes.tencent_symbol("600519"), "sh600519")
        self.assertEqual(quotes.tencent_symbol("000661"), "sz000661")
        self.assertEqual(quotes.tencent_symbol("830799"), "bj830799")
        self.assertEqual(quotes.tencent_symbol("700", "HK"), "hk00700")
        self.assertEqual(quotes.eastmoney_secid("600519"), "1.600519")
        self.assertEqual(quotes.eastmoney_secid("000661"), "0.000661")
        self.assertEqual(quotes.eastmoney_secid("700", "HK"), "116.00700")

    def test_解析腾讯的两种键名(self) -> None:
        rows = quotes.parse_tencent(TENCENT_CANNED, "sz000661")
        self.assertEqual(rows[-1], (date(2026, 9, 30), 71.83))
        # 港股：键名是 day、每行末尾多一个对象 —— 不能假定列数
        hk = {"data": {"hk00700": {"day": [["2026-09-30", "435.000", "438.400", "439.800", "433.400", "14277944.000", {"cqr": "2026-09-30"}]]}}}
        self.assertEqual(quotes.parse_tencent(hk, "hk00700")[-1][1], 438.4)

    def test_标的缺失要说清是哪一步(self) -> None:
        with self.assertRaises(quotes.QuoteError) as caught:
            quotes.parse_tencent({"data": {}}, "sz000661")
        self.assertIn("sz000661", str(caught.exception))

    def test_解析东财(self) -> None:
        rows = quotes.parse_eastmoney(EASTMONEY_CANNED)
        self.assertEqual(rows[-1], (date(2026, 9, 30), 71.83))

    def test_东财限流时给的是可读的错误(self) -> None:
        with self.assertRaises(quotes.QuoteError) as caught:
            quotes.parse_eastmoney({"rc": 0, "data": None})
        self.assertIn("限流", str(caught.exception))

    def test_基准日落在非交易日时取前一个交易日(self) -> None:
        rows = [(date(2026, 9, 29), 70.55), (date(2026, 9, 30), 71.83)]
        # 2026-10-03 是周六：应取 09-30，并把真实交易日写出来
        day, price = quotes.pick_on_or_before(rows, date(2026, 10, 3))
        self.assertEqual(day, date(2026, 9, 30))
        self.assertEqual(price, 71.83)

    def test_区间里没有数据要报错(self) -> None:
        with self.assertRaises(quotes.QuoteError):
            quotes.pick_on_or_before([(date(2026, 9, 30), 71.83)], date(2026, 9, 1))

    def test_两个源一致时给出结果(self) -> None:
        quote = quotes.latest_close(
            "000661", as_of="2026-09-30", transport=fake_quote_transport()
        )
        self.assertEqual(quote.price, 71.83)
        self.assertEqual(quote.as_of, "2026-09-30")
        self.assertEqual(quote.agreement, 0.0)

    def test_两个源对不上就停下(self) -> None:
        skewed = json.loads(json.dumps(EASTMONEY_CANNED))
        skewed["data"]["klines"][-1] = "2026-09-30,70.56,69.00,72.56,70.21,51879,1"
        with self.assertRaises(quotes.QuoteError) as caught:
            quotes.latest_close(
                "000661", as_of="2026-09-30", transport=fake_quote_transport(eastmoney=skewed)
            )
        message = str(caught.exception)
        self.assertIn("71.83", message)
        self.assertIn("69.00", message)

    def test_两个源的交易日不同也要停下(self) -> None:
        shifted = json.loads(json.dumps(EASTMONEY_CANNED))
        shifted["data"]["klines"] = shifted["data"]["klines"][:1]
        with self.assertRaises(quotes.QuoteError) as caught:
            quotes.latest_close(
                "000661", as_of="2026-09-30", transport=fake_quote_transport(eastmoney=shifted)
            )
        self.assertIn("交易日", str(caught.exception))

    def test_备源不可用时降级成单源并明确标出(self) -> None:
        """两个源**都有但打架**要停下；备源**根本取不到**（限流、断网）则降级。

        区分这两件事是有原因的：2026-10-14 实测东财对连续请求会直接断连接，
        如果那一侧一挂整条自动取数就失败，「输入公司名出报告」这件事就没法用了。
        但降级必须留痕 —— 报告与行情表里要写清「这个价没交叉核对」。
        """

        def transport(url, **kwargs):
            if "ifzq.gtimg.cn" in url:
                return TENCENT_CANNED
            raise HttpError("Remote end closed connection without response")

        quote = quotes.latest_close("000661", as_of="2026-09-30", transport=transport)
        self.assertEqual(quote.price, 71.83)
        self.assertTrue(quote.single_source)
        self.assertEqual(quote.cross_price, 0.0)
        self.assertIn("备源不可用", quote.cross_source)
        self.assertTrue(quote.to_dict()["single_source"])


class LatestReportTest(unittest.TestCase):
    """不限年份检索：用来自动确定「最近一份年报是哪年」。"""

    Canned = {
        "announcements": [
            {
                "secCode": "000661",
                "secName": "长春高新",
                "announcementTitle": "2025年年度报告",
                "announcementTime": 1776787200000,
                "adjunctUrl": "finalpage/2026-04-22/1225135424.PDF",
                "announcementId": "1225135424",
            },
            {
                "secCode": "000661",
                "secName": "长春高新",
                "announcementTitle": "2025年年度报告摘要",
                "announcementTime": 1776787200000,
                "adjunctUrl": "finalpage/2026-04-22/1225135423.PDF",
                "announcementId": "1225135423",
            },
            {
                "secCode": "000661",
                "secName": "长春高新",
                "announcementTitle": "2024年年度报告",
                "announcementTime": 1745164800000,
                "adjunctUrl": "finalpage/2025-04-21/1223156299.PDF",
                "announcementId": "1223156299",
            },
        ]
    }

    def test_挑出最近那份正文并给出披露年份(self) -> None:
        found = cninfo.query(
            code="000661", column="szse", year=None, transport=lambda *a, **k: self.Canned
        )
        picked = cninfo.pick_latest(found)
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.title, "2025年年度报告")  # 摘要被排掉
        self.assertEqual(cninfo.disclosure_year(picked), 2026)

    def test_披露年份拿不到时返回None不猜(self) -> None:
        item = cninfo.Announcement(code="000661", name="", title="", date="", adjunct="")
        self.assertIsNone(cninfo.disclosure_year(item))

    def test_没有正文时返回None(self) -> None:
        self.assertIsNone(cninfo.pick_latest([]))


if __name__ == "__main__":
    unittest.main()
