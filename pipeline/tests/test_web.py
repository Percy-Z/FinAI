"""Web 工作台的测试。

这一层只有两件事值得看住，别的一改就坏的经验都来自它们：

    1. 复现凭证必须真的打得开
       竞赛要求「数据来源可核验、执行过程可追溯、运行结果可复现」。
       界面上那排按钮就是给评审点开核对用的 —— 按钮在、文件不在，
       比没有按钮更糟。所以这里对每一个凭证都发一次真实请求。

    2. 静态文件服务必须挡住目录穿越
       /runs/、/logs/、/data/ 三个目录是直接读磁盘的。少一次校验，
       整个仓库（包括 apikey.txt）就跟着 URL 出去了。

不依赖网络、不依赖大模型：起一个真的 HTTP 服务在本机回环上打请求。
"""

from __future__ import annotations

import base64
import json
import re
import sys
import threading
import unittest
import urllib.error
import urllib.request
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline import assumptions_gen as gen  # noqa: E402
from pipeline import web  # noqa: E402
from pipeline import config  # noqa: E402


class WebTestBase(unittest.TestCase):
    """在回环上起一次服务，供本文件的用例共用。"""

    server: web.ThreadingHTTPServer
    base: str
    thread: threading.Thread

    @classmethod
    def setUpClass(cls) -> None:
        # 端口 0 = 让系统随便挑一个空闲端口，避免和开发中的工作台撞车
        cls.server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def get(self, path: str) -> tuple[int, str, bytes]:
        """返回 (状态码, Content-Type, 正文)。4xx 不抛异常，当普通结果看。"""
        try:
            with urllib.request.urlopen(self.base + path, timeout=15) as response:
                return response.status, response.headers.get("Content-Type", ""), response.read()
        except urllib.error.HTTPError as err:
            try:
                return err.code, err.headers.get("Content-Type", ""), err.read()
            finally:
                err.close()  # 不关会让解释器在退出时报 ResourceWarning


    def post(self, path: str, data: bytes = b"") -> tuple[int, str, bytes]:
        """发一个 POST。取数页是只读的，这条用于验证「写入口不存在」。"""
        request = urllib.request.Request(self.base + path, data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, response.headers.get("Content-Type", ""), response.read()
        except urllib.error.HTTPError as err:
            try:
                return err.code, err.headers.get("Content-Type", ""), err.read()
            finally:
                err.close()


class TestReportImport(WebTestBase):
    """工作台吃「本地年报 PDF」那条路：上传 → 入库 → 写出清单。

    这一条把「年报从哪来」与「怎么读年报」分开：PDF 由使用者提供（可能来自队友、
    交易所，或者现场发的样例），读取仍走取数层。入库之后 plan 照旧自己跑完成。
    """

    def _upload(self, tmp: str, reports: list[dict]) -> tuple[int, dict]:
        body = json.dumps(
            {"company": "600519", "reports": reports}, ensure_ascii=False
        ).encode("utf-8")
        with mock.patch.object(web, "COMPANIES_ROOT", Path(tmp) / "companies"):
            code, _, raw = self.post("/api/reports", body)
        return code, json.loads(raw.decode("utf-8"))

    def test_上传的年报落进公司目录并写出清单(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blob = base64.b64encode(b"%PDF-1.4" + b"0" * 2048).decode()
            code, payload = self._upload(
                tmp,
                [
                    {
                        "code": "600519",
                        "year": 2026,
                        "name": "贵州茅台",
                        "role": "target",
                        "title": "2025年年度报告",
                        "data": blob,
                    },
                    {"code": "000858", "year": 2026, "name": "五粮液"},
                ],
            )
            self.assertEqual(code, 200, payload)
            self.assertTrue(payload["ok"], payload)
            base = Path(tmp) / "companies" / "600519"
            self.assertTrue((base / "raw_pdf" / "600519_2026.pdf").is_file())
            self.assertTrue((base / "company.json").is_file())
            self.assertEqual(payload["target"]["code"], "600519")
            # 没有 PDF 的可比公司只提示，不拦 —— 它会在取数时联网下载
            self.assertTrue(any("联网下载" in note for note in payload["warnings"]))

    def test_不是PDF的上传被拦下(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blob = base64.b64encode(b"PK\x03\x04" + b"0" * 64).decode()
            code, payload = self._upload(
                tmp,
                [{"code": "600519", "year": 2026, "role": "target", "data": blob}],
            )
            self.assertEqual(code, 409)
            self.assertFalse(payload["ok"])
            self.assertIn("PDF", payload["error"])

    def test_缺年份的上传被拦下(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blob = base64.b64encode(b"%PDF-1.4" + b"0" * 64).decode()
            code, payload = self._upload(
                tmp, [{"code": "600519", "role": "target", "data": blob}]
            )
            self.assertEqual(code, 409)
            self.assertIn("披露年份", payload["error"])


class TestInputsFollowDataset(WebTestBase):
    """界面上那三个「输入文件」要跟着**实际生效的输入路径**走。

    取数层的路径开关能把输入指到别的数据集（例如 data/real-000661/），但界面原先
    写死读 data/ 下那三个演示文件 —— 于是「新建估值」和「复现凭证」看到的输入跟这次
    运行实际用的对不上（2026-10-14 用户指出：工作台里显示的还是示例公司）。
    """

    def test_输入文件按实际生效的路径发(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "assumptions.toml"
            real.write_text('[company]\nname = "长春高新"\n', encoding="utf-8")
            with mock.patch.object(config, "DEFAULT_CONFIG_PATH", real):
                code, kind, body = self.get("/data/assumptions.toml")
            self.assertEqual(code, 200)
            self.assertIn("text/plain", kind)   # .toml 当文本发，浏览器能内嵌显示
            self.assertIn("长春高新", body.decode("utf-8"))

    def test_复现凭证里的输入指向同一份文件(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "market.csv"
            real.write_text("code,name,role\n", encoding="utf-8")
            with mock.patch.object(config, "DEFAULT_MARKET_PATH", real):
                _, _, raw = self.get("/api/status")
                item = next(
                    entry
                    for entry in json.loads(raw)["evidence"]
                    if entry["label"] == "输入 · 行情快照"
                )
                code, _, body = self.get(item["url"])
            # 地址保持稳定（复现凭证的按钮不能因为换了数据集就变），
            # 但提示里要写明到底是哪一份文件
            self.assertEqual(item["url"], "/data/market.csv")
            self.assertIn(Path(tmp).name, item["hint"])
            self.assertTrue(item["exists"])
            self.assertEqual(code, 200)
            # 比内容而不是比换行：Windows 上写文件会把 \n 转成 \r\n
            self.assertIn("code,name,role", body.decode("utf-8"))


class TestCompanyWorkbench(WebTestBase):
    """工作台的「输入公司名 → 自动取数」这条路上，界面依赖的几个接口。

    这些用例**不碰**仓库里的 data/companies/：把 web.COMPANIES_ROOT 指到临时目录，
    在假公司目录上验读与写。真跑取数要联网、要几分钟，那是 pipeline/tests/test_job.py
    的事（那里用假行情 + 预置产物）。
    """

    def _fixture(self, tmp: str) -> Path:
        base = Path(tmp) / "companies" / "000661"
        (base / "raw_pdf").mkdir(parents=True)
        (base / "extracted").mkdir()
        (base / "raw_pdf" / "000661_2026.pdf").write_bytes(b"%PDF-1.4" + b"0" * 2048)
        (base / "extracted" / "000661_2026.json").write_text(
            json.dumps(
                {
                    "code": "000661",
                    "name": "长春高新",
                    "year": 2026,
                    "period": "2026A",
                    "accounts": {"cash": {"value_yuan": 1.0, "page": 81, "raw_text": "货币资金 1"}},
                    "missing_accounts": [],
                    "notes": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (base / "company.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "target": {"code": "000661", "name": "长春高新", "years": [2026, 2025]},
                    "peers": [{"code": "300009", "name": "安科生物", "years": [2026]}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return base

    def test_列出公司目录(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(tmp)
            with mock.patch.object(web, "COMPANIES_ROOT", Path(tmp) / "companies"):
                code, _, raw = self.get("/api/companies")
            payload = json.loads(raw)
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        item = payload["companies"][0]
        self.assertEqual(item["code"], "000661")
        self.assertEqual(item["name"], "长春高新")
        self.assertEqual(item["peers"], ["300009"])
        self.assertEqual(item["pdfs"], 1)
        self.assertEqual(item["records"], 1)
        self.assertFalse(item["has_draft"])

    def test_取数页读的是选中那家公司(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(tmp)
            with mock.patch.object(web, "COMPANIES_ROOT", Path(tmp) / "companies"):
                code, _, raw = self.get("/api/ingest?company=000661")
            payload = json.loads(raw)
        self.assertEqual(code, 200)
        # 这才是「取数层不可用」那类问题的反面：页面读的是当前公司目录，不是仓库里那套
        self.assertEqual(payload["company"]["code"], "000661")
        self.assertEqual(payload["pdfs"]["count"], 1)
        # 临时目录在仓库外，_rel 会给出绝对路径 —— 比后缀就够了
        self.assertTrue(payload["records"]["dir"].replace("\\", "/").endswith("000661/extracted"))

    def test_没有草稿时确认屏说清下一步(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(tmp)
            with mock.patch.object(web, "COMPANIES_ROOT", Path(tmp) / "companies"):
                _, _, raw = self.get("/api/plan/draft?company=000661")
            payload = json.loads(raw)
        self.assertFalse(payload["ok"])
        self.assertIn("先跑一次取数", payload["error"])

    def test_草稿按状态分组给出(self) -> None:
        draft = gen.Draft(
            company={"name": "长春高新", "code": "000661", "source": "cninfo"},
            fields={
                "drivers.revenue_base": gen.Field(
                    "drivers.revenue_base", 1000.0, gen.DERIVED, "年报抽取"
                ),
                "drivers.tax_rate": gen.Field(
                    "drivers.tax_rate", 0.25, gen.DEFAULT, "法定税率"
                ),
                "drivers.dividend_payout_ratio": gen.Field(
                    "drivers.dividend_payout_ratio", None, gen.MISSING, "年报里没有"
                ),
            },
            notes=["报表年度 2025 年"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = self._fixture(tmp)
            draft.save(base / gen.DRAFT_NAME)
            with mock.patch.object(web, "COMPANIES_ROOT", Path(tmp) / "companies"):
                _, _, raw = self.get("/api/plan/draft?company=000661")
            payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["groups"][gen.DERIVED]), 1)
        self.assertEqual(len(payload["groups"][gen.DEFAULT]), 1)
        self.assertEqual(payload["missing"], ["drivers.dividend_payout_ratio"])
        self.assertEqual(payload["notes"], ["报表年度 2025 年"])

    def test_改过的值落进草稿(self) -> None:
        draft = gen.Draft(
            company={"name": "长春高新", "code": "000661"},
            fields={
                "drivers.dividend_payout_ratio": gen.Field(
                    "drivers.dividend_payout_ratio", None, gen.MISSING, "年报里没有"
                )
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = self._fixture(tmp)
            draft.save(base / gen.DRAFT_NAME)
            body = json.dumps(
                {"company": "000661", "fields": {"drivers.dividend_payout_ratio": "0.415"}}
            ).encode()
            with mock.patch.object(web, "COMPANIES_ROOT", Path(tmp) / "companies"):
                _, _, raw = self.post("/api/plan/draft", body)
            payload = json.loads(raw)
            saved = json.loads((base / gen.DRAFT_NAME).read_text(encoding="utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["missing"], [])
        # 字符串 "0.415" 要变成数字 —— 否则配置层读出来是字符串，一路错到估值
        self.assertEqual(saved["fields"]["drivers.dividend_payout_ratio"]["value"], 0.415)
        self.assertEqual(saved["fields"]["drivers.dividend_payout_ratio"]["status"], "human")

    def test_整数类字段不会被存成小数(self) -> None:
        """确认屏只给字符串；`drivers.years` 这类必须收成整数。

        存成 5.0 的话，算法层的数据类拿它乘列表会抛
        「can't multiply sequence by non-int of type 'float'」（2026-10-15 现场）。"""
        draft = gen.Draft(
            company={"name": "长春高新", "code": "000661"},
            fields={"drivers.years": gen.Field("drivers.years", 5, gen.DERIVED, "规则默认")},
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = self._fixture(tmp)
            draft.save(base / gen.DRAFT_NAME)
            body = json.dumps(
                {"company": "000661", "fields": {"drivers.years": "6"}}
            ).encode()
            with mock.patch.object(web, "COMPANIES_ROOT", Path(tmp) / "companies"):
                _, _, raw = self.post("/api/plan/draft", body)
            saved = json.loads((base / gen.DRAFT_NAME).read_text(encoding="utf-8"))
        self.assertTrue(json.loads(raw)["ok"])
        self.assertEqual(saved["fields"]["drivers.years"]["value"], 6)
        self.assertIsInstance(saved["fields"]["drivers.years"]["value"], int)

    def test_人写的依据也一起存进草稿(self) -> None:
        """人填的值必须带着「凭什么」—— 竞赛要的就是这套假设依据台账。

        以前确认屏只回传值，出处被统一写成「在确认屏上改的」，评审按这一栏核不了。
        """
        draft = gen.Draft(
            company={"name": "Apple", "code": "AAPL"},
            fields={
                "wacc.beta": gen.Field("wacc.beta", None, gen.MISSING, "这次没算出 beta")
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = self._fixture(tmp)
            draft.save(base / gen.DRAFT_NAME)
            body = json.dumps(
                {
                    "company": "000661",
                    "fields": {"wacc.beta": "1.19"},
                    "sources": {"wacc.beta": "Wind：5 年月度 beta（对比标普500）= 1.19"},
                }
            ).encode()
            with mock.patch.object(web, "COMPANIES_ROOT", Path(tmp) / "companies"):
                _, _, raw = self.post("/api/plan/draft", body)
            payload = json.loads(raw)
            saved = json.loads((base / gen.DRAFT_NAME).read_text(encoding="utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(saved["fields"]["wacc.beta"]["value"], 1.19)
        self.assertIn("对比标普500", saved["fields"]["wacc.beta"]["source"])

    def test_清空一个字段会被标成没填(self) -> None:
        draft = gen.Draft(
            company={"name": "长春高新", "code": "000661"},
            fields={"drivers.tax_rate": gen.Field("drivers.tax_rate", 0.25, gen.DEFAULT, "法定税率")},
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = self._fixture(tmp)
            draft.save(base / gen.DRAFT_NAME)
            body = json.dumps({"company": "000661", "fields": {"drivers.tax_rate": ""}}).encode()
            with mock.patch.object(web, "COMPANIES_ROOT", Path(tmp) / "companies"):
                _, _, raw = self.post("/api/plan/draft", body)
            payload = json.loads(raw)
        self.assertEqual(payload["missing"], ["drivers.tax_rate"])

    def test_公司代码不能越界(self) -> None:
        """目录名就是代码，必须挡住 ../ 这类输入 —— 这是能写文件的接口。"""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(web, "COMPANIES_ROOT", Path(tmp) / "companies"):
                body = json.dumps({"target_code": "../evil"}).encode()
                code, _, raw = self.post("/api/plan", body)
            payload = json.loads(raw)
        self.assertEqual(code, 409)
        self.assertFalse(payload["ok"])
        self.assertFalse((Path(tmp) / "evil").exists())

    def test_没有草稿时不许起估值子进程(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(tmp)
            with mock.patch.object(web, "COMPANIES_ROOT", Path(tmp) / "companies"):
                code, _, raw = self.post(
                    "/api/ingest/run", json.dumps({"company": "000661"}).encode()
                )
            payload = json.loads(raw)
        self.assertEqual(code, 409)
        self.assertIn("假设草稿", payload["error"])

    def test_名称检索失败也告诉人原因(self) -> None:
        """检索要联网。断网时不能让界面卡住或报一个看不懂的错。"""
        # 要 patch **resolve 模块里那个名字**：它在导入时就把 post_form 绑进了自己的
        # 命名空间，改 http.post_form 影响不到它（有网的环境下这条会假通过）。
        from finai_web import http as web_http
        from finai_web import resolve as resolve_mod

        with mock.patch.object(
            resolve_mod, "post_form", side_effect=web_http.HttpError("连接超时")
        ):
            code, _, raw = self.post("/api/resolve", json.dumps({"keyword": "长春高新"}).encode())
        payload = json.loads(raw)
        self.assertEqual(code, 200)
        self.assertFalse(payload["ok"])
        self.assertIn("长春高新", payload["error"])


class TestIngest(WebTestBase):
    """取数页：界面要能说清「这些数是从哪来的」。

    这一页必须是**只读**的 —— 它把现状摊开、告诉你下一步敲哪条命令，下载与抽取
    留在命令行（要联网、要几分钟，现场演示里让评委干等两分钟的按钮是风险）。
    所以除了结构完整，这里专门验一条「没有写入口」。
    """

    def ingest(self) -> dict[str, Any]:
        code, _, body = self.get("/api/ingest")
        self.assertEqual(code, 200, body[:200])
        return json.loads(body)

    def test_接口结构完整(self) -> None:
        payload = self.ingest()
        self.assertTrue(payload["available"], payload.get("error", ""))
        for key in (
            "source", "targets", "pdfs", "downloads", "records",
            "pipeline_input", "market",
        ):
            self.assertIn(key, payload, f"/api/ingest 少了 {key}")
        for key in ("path", "exists", "template", "count", "items", "error"):
            self.assertIn(key, payload["targets"], f"targets 少了 {key}")
        self.assertIn("active", payload["source"])

    def test_取数是只读的没有写入口(self) -> None:
        """POST 到取数接口必须 404 —— 界面上不该有能改数据的入口。"""
        code, _, _ = self.post("/api/ingest")
        self.assertEqual(code, 404)

    def test_出处条目带页码与原文(self) -> None:
        """评审问「这个数哪来的」，答案要具体到第几页、原文是什么。"""
        records = self.ingest()["records"]["items"]
        if not records:
            self.skipTest("本机还没有抽取记录，跳过出处检查")
        for record in records:
            for key in ("code", "year", "file", "method", "account_count", "accounts"):
                self.assertIn(key, record, f"抽取记录少了 {key}")
            for account in record["accounts"]:
                for key in ("field", "label", "value", "page", "raw"):
                    self.assertIn(key, account, f"科目出处少了 {key}")

    def test_流水线输入带出处映射(self) -> None:
        info = self.ingest()["pipeline_input"]
        if not info["exists"]:
            self.skipTest("本机还没有生成流水线输入")
        self.assertTrue(info["companies"], "有流水线输入却没有出处映射")
        for company in info["companies"]:
            self.assertIn("code", company)
            # 目标公司有基期资产负债表（opening），可比公司只有利润类指标（peer）
            self.assertTrue(
                company["opening"] or company["peer"],
                f"{company['code']} 没有任何科目出处",
            )

    def test_行情表校验结论进了接口(self) -> None:
        market = self.ingest()["market"]
        for key in ("path", "exists", "ok", "rows", "errors", "warnings"):
            self.assertIn(key, market, f"行情表结论少了 {key}")

    def test_数据源进了状态接口(self) -> None:
        """总览与运行页要显示「这次用的是手填数据还是年报抽取结果」。"""
        payload = json.loads(self.get("/api/status")[2])
        self.assertIn("data_source", payload)

    def test_界面里有取数这一页(self) -> None:
        _, _, pages = self.get("/web/js/pages2.js")
        # 用 assertTrue 而不是 assertIn：失败时不要把整个 JS 文件打进报告
        self.assertTrue(
            "id: 'ingest'" in pages.decode("utf-8"),
            "前端页面注册表里没有取数页（id: 'ingest'）",
        )


class TestPage(WebTestBase):
    """页面本身：入口要在，脚本别写错到浏览器跑不起来。"""

    def test_首页能打开且脚本都挂得上(self) -> None:
        """首页的契约：壳页打得开，它引用的每个脚本都取得到、不是空的。

        这条以前断言的是 HTML 里写死的 evidence-card / quick-report —— 那时首页
        由服务端渲染。现在首页换成了壳页（web/index.html），内容改由前端脚本渲染，
        那几个标记不再出现在 HTML 里，但**这条测试真正要拦的东西没变**：
        入口打得开、脚本路径没写错。脚本 src 写错一个字，浏览器打开就是一片白，
        而服务端的这条断言是唯一能提前发现它的地方。

        顺带保住原来那条断言的意图：「复现凭证」这一页仍然要在页面注册表里 ——
        它是竞赛「过程可追溯、结果可复现」在界面上的入口。
        """
        status, content_type, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        text = body.decode("utf-8")

        # 壳页的三个挂载点：应用根节点、路由渲染区、证据抽屉
        for marker in ('id="app"', 'id="view"', 'id="drawer"'):
            self.assertIn(marker, text, f"首页缺少挂载点 {marker}")

        # 首页引用的每个脚本都要真的取得到，而且有内容
        sources = re.findall(r'<script[^>]+src="([^"]+)"', text)
        self.assertTrue(sources, "首页一个脚本都没引用，壳页多半被改坏了")
        for src in sources:
            code, kind, payload = self.get(src)
            self.assertEqual(code, 200, f"首页引用的脚本取不到：{src}")
            self.assertIn("javascript", kind)
            self.assertTrue(payload.strip(), f"{src} 是空文件")

        # 复现凭证页仍在页面注册表里
        _, _, pages = self.get("/web/js/pages2.js")
        self.assertIn("'evidence'", pages.decode("utf-8"))

        # 根地址也要能进
        self.assertEqual(self.get("/index.html")[0], 200)

    def test_未知路径返回404(self) -> None:
        self.assertEqual(self.get("/nope")[0], 404)


class TestEvidence(WebTestBase):
    """复现凭证：界面上的按钮指向的文件必须真的存在、真的打得开。"""

    def status(self) -> dict[str, Any]:
        code, _, body = self.get("/api/status")
        self.assertEqual(code, 200)
        return json.loads(body)

    def test_状态接口结构完整(self) -> None:
        payload = self.status()
        for key in ("run_id", "running", "steps", "artifacts", "evidence", "metrics"):
            self.assertIn(key, payload, f"/api/status 少了 {key}")

    def test_凭证字段完整(self) -> None:
        evidence = self.status()["evidence"]
        if not evidence:
            self.skipTest("本机还没有跑过流水线，没有凭证可查")
        for item in evidence:
            for key in ("label", "hint", "url", "exists", "bytes"):
                self.assertIn(key, item, f"凭证 {item.get('label')} 少了 {key}")
            # 只开放这三个目录，出现在别的路径下就是配错了
            self.assertTrue(
                item["url"].startswith(("/runs/", "/logs/", "/data/")),
                f"凭证 URL 越界：{item['url']}",
            )

    def test_每个已存在的凭证都能打开(self) -> None:
        """这条是这次改动的核心 —— 按钮点下去不能是 404。"""
        evidence = self.status()["evidence"]
        if not evidence:
            self.skipTest("本机还没有跑过流水线，没有凭证可查")
        checked = 0
        for item in evidence:
            if not item["exists"]:
                continue
            code, content_type, body = self.get(item["url"])
            self.assertEqual(code, 200, f"{item['label']} 打不开：{item['url']}")
            self.assertEqual(len(body), item["bytes"], f"{item['label']} 长度对不上")
            self.assertNotIn("application/octet-stream", content_type, f"{item['label']} 会变成下载")
            checked += 1
        self.assertGreater(checked, 0, "一个凭证都没能检查到")

    def test_报告与中间层文件都在清单里(self) -> None:
        """评审要的两类入口：成品报告 + 能证明可复现的中间层记录。"""
        evidence = self.status()["evidence"]
        if not evidence:
            self.skipTest("本机还没有跑过流水线，没有凭证可查")
        urls = " ".join(item["url"] for item in evidence)
        self.assertIn("19_report.pdf", urls)      # 报告
        self.assertIn("trace.jsonl", urls)        # 执行过程可追溯
        self.assertIn("narrative.json", urls)     # 大模型原文可查
        self.assertIn("10_run_manifest.json", urls)   # 结果哈希可核验
        self.assertIn("assumptions.toml", urls)   # 数据来源


class TestStaticFiles(WebTestBase):
    """三个静态目录的安全边界。"""

    def test_输入文件可以读取(self) -> None:
        for name, expect_type in (
            ("assumptions.toml", "text/plain"),
            ("market.csv", "text/csv"),
        ):
            path = REPO_ROOT / "data" / name
            if not path.exists():
                continue
            code, content_type, body = self.get(f"/data/{name}")
            self.assertEqual(code, 200, f"/data/{name} 读不到")
            self.assertIn(expect_type, content_type)
            self.assertGreater(len(body), 0)

    def test_目录穿越被挡住(self) -> None:
        for attack in (
            "/runs/../../etc/passwd",
            "/logs/../../etc/passwd",
            "/data/../../etc/passwd",
            "/runs/../../apikey.txt",
            "/logs/%2e%2e%2f%2e%2e%2fetc/passwd",
            "/data/..%2f..%2fapikey.txt",
        ):
            code, _, _ = self.get(attack)
            self.assertIn(code, (403, 404), f"{attack} 没被挡住，返回 {code}")

    def test_目录本身不能当文件列出(self) -> None:
        # 目录不读成文件；给个不存在或越界的都行，只要不是 200 带内容
        code, _, _ = self.get("/data/")
        self.assertNotEqual(code, 200)


if __name__ == "__main__":
    unittest.main()
