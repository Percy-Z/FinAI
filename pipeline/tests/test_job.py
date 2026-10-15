"""任务编排的离线端到端测试：plan 的六步能不能走通，产物对不对。

不联网的做法：预置年报 PDF（大于 1KB 的占位文件）与抽取记录 —— 取数层命中
这两样就直接跳过下载与抽取；再把行情接口换成假实现。这样测的是**编排**本身：
顺序、环境变量、产物落盘、失败时停在哪一步。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline import assumptions_gen as gen  # noqa: E402
from pipeline import job  # noqa: E402
from finai_web import company as company_mod  # noqa: E402


def account(value: float) -> dict:
    return {
        "value_yuan": value,
        "label": "科目",
        "page": 88,
        "raw_text": f"科目 {value:,.2f}",
        "unit": "元",
        "scale": 1.0,
        "confidence": 0.95,
        "note": "",
    }


def full_record(code: str, year: int, *, revenue: float, scale: float = 1.0) -> dict:
    """一份「什么科目都有」的抽取记录，省得测试被缺科目拦住。"""
    accounts = {
        "revenue": account(revenue),
        "operating_cost": account(100.0 * scale),
        "net_profit_attributable": account(80.0 * scale),
        "net_profit": account(90.0 * scale),
        "total_profit": account(100.0 * scale),
        "income_tax": account(10.0 * scale),
        "interest_expense": account(10.0 * scale),
        "finance_expense": account(10.0 * scale),
        "depreciation_fixed_assets": account(20.0 * scale),
        "amortization_intangible": account(5.0 * scale),
        "amortization_lt_prepaid": account(5.0 * scale),
        "operating_cash_flow": account(150.0 * scale),
        "cash": account(55.0),
        "accounts_receivable": account(60.0),
        "inventory": account(40.0),
        "fixed_assets": account(200.0),
        "total_assets": account(385.0),
        "accounts_payable": account(40.0),
        "short_term_debt": account(100.0),
        "long_term_debt": account(0.0),
        "total_liabilities": account(165.0),
        "equity_attributable": account(220.0),
        "minority_equity": account(0.0),
        "total_equity": account(220.0),
        "share_capital": account(100.0),
    }
    return {
        "code": code,
        "name": {"000661": "长春高新", "300009": "安科生物"}.get(code, code),
        "year": year,
        "period": f"{year}A",
        "pdf": f"{code}_{year}.pdf",
        "sha256": "0" * 64,
        "pages": 200,
        "has_text_layer": True,
        "backend": "pdfplumber",
        "method": "cninfo",
        "source_url": "https://static.cninfo.com.cn/x.PDF",
        "extracted_at": "2026-10-14T22:00:00+08:00",
        "accounts": accounts,
        "notes": [],
        # 基期 11 个科目（配平：资产 385 = 负债 165 + 权益 220）。
        # 抽取记录把它们放在 opening_balance.values 里，adapter 从这里读。
        "opening_balance": {
            "values": {
                "cash": 55.0,
                "accounts_receivable": 60.0,
                "inventory": 40.0,
                "fixed_assets": 200.0,
                "other_non_current_assets": 30.0,
                "accounts_payable": 40.0,
                "interest_bearing_debt": 100.0,
                "other_non_current_liabilities": 25.0,
                "share_capital": 100.0,
                "retained_earnings": 120.0,
                "minority_equity": 0.0,
            },
            "provenance": {"cash": "read", "accounts_receivable": "read"},
            "notes": [],
        },
        # 可比指标（5 个）与基期同一个形状：{values, provenance, notes}
        "peer": {
            "values": {
                "revenue": revenue,
                "net_profit_attributable": 80.0 * scale,
                "book_equity_attributable": 220.0,
                "ebit": 110.0 * scale,
                "ebitda": 140.0 * scale,
            },
            "provenance": {"revenue": "read"},
            "notes": [],
        },
        "missing_accounts": [],
        "cross_check": {},
    }


def monthly_series(months: int, *, beta: float, seed: int = 7) -> list[list[str]]:
    """造一段月线：股票收益 = beta × 指数收益，这样回归出来的 beta 是已知的。"""
    out_stock: list[list[str]] = []
    out_index: list[list[str]] = []
    price_stock = 100.0
    price_index = 1000.0
    year, month = 2021, 1
    for index in range(months):
        shock = ((index * seed) % 11 - 5) / 100.0        # 指数月收益，-5% ~ +5%
        stock_return = beta * shock
        price_index *= 1.0 + shock
        price_stock *= 1.0 + stock_return
        stamp = f"{year}-{month:02d}-28"
        out_index.append([stamp, "0", f"{price_index:.4f}", "0", "0", "0"])
        out_stock.append([stamp, "0", f"{price_stock:.4f}", "0", "0", "0"])
        month += 1
        if month > 12:
            month = 1
            year += 1
    return out_stock, out_index


STOCK_MONTHS, INDEX_MONTHS = monthly_series(42, beta=1.5)


def fake_transport(url: str, **kwargs):
    """按 URL 分派：日线（腾讯 + 东财）与月线（腾讯）。"""
    day_close = {"sz000661": "71.830", "sz300009": "8.610"}
    if "ifzq.gtimg.cn" in url:
        symbol = url.split("param=")[1].split(",")[0]
        period = url.split(",")[1]
        if period == "month":
            if symbol == "sh000300":
                return {"data": {"sh000300": {"qfqmonth": INDEX_MONTHS}}}
            return {"data": {symbol: {"qfqmonth": STOCK_MONTHS}}}
        close = day_close.get(symbol)
        if close is None:
            raise AssertionError(f"假行情里没有 {symbol}")
        price = float(close)
        rows = [
            ["2026-09-29", f"{price:.3f}", f"{price:.3f}", f"{price:.3f}", f"{price:.3f}", "36016.000"],
            ["2026-09-30", f"{price:.3f}", f"{price:.3f}", f"{price:.3f}", f"{price:.3f}", "51879.000"],
        ]
        return {"data": {symbol: {"qfqday": rows}}}
    if "eastmoney.com" in url:
        secid = url.split("secid=")[1].split("&")[0]
        price = {"0.000661": "71.83", "0.300009": "8.61"}.get(secid, "10.00")
        return {
            "data": {
                "code": secid.split(".")[1],
                "klines": [
                    f"2026-09-29,{price},{price},{price},{price},36016,1.0",
                    f"2026-09-30,{price},{price},{price},{price},51879,1.0",
                ],
            }
        }
    raise AssertionError(f"没预料到的 URL：{url}")


def make_company_dir(tmp: Path, *, with_peer: bool = True) -> Path:
    base = tmp / "companies" / "000661"
    (base / "raw_pdf").mkdir(parents=True)
    (base / "extracted").mkdir()
    # 占位 PDF：>1024 字节，取数层会当成「本地已有」直接跳过下载
    for name in ("000661_2026.pdf", "000661_2025.pdf"):
        (base / "raw_pdf" / name).write_bytes(b"%PDF-1.4" + b"0" * 2048)
    records = [
        full_record("000661", 2026, revenue=1000.0, scale=1.0),
        full_record("000661", 2025, revenue=900.0, scale=0.95),
    ]
    for item in records:
        (base / "extracted" / f"000661_{item['year']}.json").write_text(
            json.dumps(item, ensure_ascii=False), encoding="utf-8"
        )
    peers = [{"code": "300009", "name": "安科生物", "org_id": "9900008310",
              "market": "CN", "column": "", "years": [2026], "annual_report_url": ""}]
    if with_peer:
        (base / "raw_pdf" / "300009_2026.pdf").write_bytes(b"%PDF-1.4" + b"0" * 2048)
        peer = full_record("300009", 2026, revenue=500.0, scale=0.5)
        (base / "extracted" / "300009_2026.json").write_text(
            json.dumps(peer, ensure_ascii=False), encoding="utf-8"
        )
    (base / "company.json").write_text(
        json.dumps(
            {
                "version": 1,
                "target": {"code": "000661", "name": "长春高新", "org_id": "gssz0000661",
                           "market": "CN", "years": [2026, 2025]},
                "peers": peers if with_peer else [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (base / "manifest.json").write_text(
        json.dumps(
            {
                # 与 downloader.save_manifest 写出来的形状一致：entries 是「代码_年份 → 记录」
                "entries": {
                    "000661_2026": {"code": "000661", "year": 2026, "title": "2025年年度报告", "ok": True},
                    "000661_2025": {"code": "000661", "year": 2025, "title": "2024年年度报告", "ok": True},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return base


class PlanTest(unittest.TestCase):
    def setUp(self) -> None:
        """job.plan/run 会往 os.environ 里写公司目录与数据源开关（配置层的路径是
        导入时读环境变量的）。整类都隔离一次环境，免得漏到后面的用例里 ——
        泄漏的症状是别的测试报「数据源设成了 extracted，但抽取结果不存在」。"""
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_六步走完并落下该有的产物(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = make_company_dir(Path(tmp))
            # plan 会把公司目录写进 os.environ（配置层的路径是导入时读环境变量的），
            # 所以用完必须还原 —— 否则后面的用例会看到上一个临时目录。
            with mock.patch.dict(os.environ):
                summary = job.plan(base, job_id="test-plan-1", transport=fake_transport)

            self.assertTrue((base / "market.csv").is_file())
            self.assertTrue((base / "targets.csv").is_file())
            self.assertTrue((base / "extracted" / "pipeline_input.json").is_file())
            self.assertTrue((base / gen.DRAFT_NAME).is_file())

            market = (base / "market.csv").read_text(encoding="utf-8")
            prices = {row["code"]: row["price"] for row in _rows(market)}
            self.assertEqual(prices["000661"], "71.83")
            self.assertEqual(prices["300009"], "8.61")
            shares = {row["code"]: row["diluted_shares"] for row in _rows(market)}
            self.assertEqual(shares["000661"], "100")  # 股本取自年报抽取，不是研报
            draft = gen.draft_for(base)
            self.assertAlmostEqual(draft.value("drivers.revenue_base"), 1000.0)
            self.assertAlmostEqual(draft.value("wacc.beta"), 1.5, places=4)  # 造出来的 beta
            self.assertIn("as_of", summary)
            self.assertEqual(summary["as_of"], "2026-09-30")

    def test_没有可比公司就停下并说清原因(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = make_company_dir(Path(tmp), with_peer=False)
            with mock.patch.dict(os.environ):
                with self.assertRaises(job.JobError) as caught:
                    job.plan(base, job_id="test-plan-2", transport=fake_transport)
        self.assertIn("可比公司", str(caught.exception))

    def test_轨迹里能看到取数六步(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = make_company_dir(Path(tmp))
            with mock.patch.dict(os.environ):
                job.plan(base, job_id="test-plan-3", transport=fake_transport)
            trace = (REPO_ROOT / "logs" / "test-plan-3" / "trace.jsonl").read_text(
                encoding="utf-8"
            )
        # 界面按 trace 的 step 渲染进度，所以这几步的名字就是界面上的阶段名
        for step in (
            "ingest_resolve",
            "ingest_fetch",
            "ingest_extract",
            "ingest_build",
            "ingest_market",
            "ingest_assumptions",
        ):
            self.assertIn(f'"{step}"', trace)


class ImportReportsTest(unittest.TestCase):
    """年报入库：使用者把年报 PDF 给工作台，程序负责读 —— 这条路不联网。

    为什么值得单测：竞赛现场拿到的可能是**离线**的年报文件（队友给的、
    交易所要验证码才给的、官方样例）。入库之后走的必须是同一条链路，
    否则「本地年报」与「联网下载」会变成两套会各自漂移的代码。
    """

    def _pdf(self, folder: Path, name: str, filler: bytes = b"0") -> Path:
        # 只要求前几个字节是 %PDF：入库这一步不解析内容，解析是抽取那一步的事
        path = folder / name
        path.write_bytes(b"%PDF-1.4" + filler * 2048)
        return path

    def test_入库按代码与年份落文件名并写出清单(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "companies" / "600519"
            downloads = Path(tmp) / "downloads"
            downloads.mkdir()
            target = self._pdf(downloads, "贵州茅台2025年年度报告.pdf", b"A")
            peer = self._pdf(downloads, "000858_2025.pdf", b"B")

            summary = job.import_reports(
                base,
                [
                    {
                        "path": str(target),
                        "code": "600519",
                        "year": 2026,
                        "name": "贵州茅台",
                        "role": "target",
                        "title": "2025年年度报告",
                    },
                    {"path": str(peer), "code": "000858", "year": 2026, "name": "五粮液"},
                ],
            )

            # 文件名一定要落成 <代码>_<年份>.pdf —— 抽取那一步靠它认年份
            self.assertTrue((base / "raw_pdf" / "600519_2026.pdf").is_file())
            self.assertTrue((base / "raw_pdf" / "000858_2026.pdf").is_file())
            self.assertEqual(summary["target"]["code"], "600519")
            self.assertEqual([item["code"] for item in summary["peers"]], ["000858"])

            # 清单是 plan 的唯一入口：它必须能被读回来
            profile = company_mod.CompanyProfile.load(base / "company.json")
            self.assertEqual(profile.target.code, "600519")
            self.assertEqual(profile.target.years, (2026,))
            self.assertEqual(profile.peer_codes, ["000858"])
            self.assertTrue((base / "targets.csv").is_file())

            # 出处：PDF 的 sha256 与公告标题都进下载清单（报告里要说得出哪来的）
            manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
            entry = manifest["entries"]["600519_2026"]
            self.assertEqual(entry["method"], "imported")
            self.assertEqual(entry["title"], "2025年年度报告")
            self.assertEqual(len(entry["sha256"]), 64)

    def test_没有给PDF的那家只提示联网下载(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "companies" / "600519"
            downloads = Path(tmp) / "downloads"
            downloads.mkdir()
            target = self._pdf(downloads, "600519_2025.pdf")
            summary = job.import_reports(
                base,
                [
                    {"path": str(target), "code": "600519", "year": 2026, "role": "target"},
                    {"code": "000858", "year": 2026, "name": "五粮液"},
                ],
            )
            self.assertEqual([item["code"] for item in summary["peers"]], ["000858"])
            self.assertTrue(any("联网下载" in note for note in summary["warnings"]))
            self.assertFalse((base / "raw_pdf" / "000858_2026.pdf").exists())

    def test_不是PDF就停下并说清(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "companies" / "600519"
            fake = Path(tmp) / "年报.docx"
            fake.write_bytes(b"PK\x03\x04" + b"0" * 2048)  # 看起来就是个 docx
            with self.assertRaises(job.JobError) as caught:
                job.import_reports(
                    base,
                    [{"path": str(fake), "code": "600519", "year": 2026, "role": "target"}],
                )
            self.assertIn("不是 PDF", str(caught.exception))

    def test_缺年份与缺代码都要报错(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "companies" / "600519"
            pdf = self._pdf(Path(tmp), "600519_2025.pdf")
            with self.assertRaises(job.JobError) as caught:
                job.import_reports(base, [{"path": str(pdf), "code": "600519"}])
            self.assertIn("披露年份", str(caught.exception))
            with self.assertRaises(job.JobError) as caught:
                job.import_reports(base, [{"path": str(pdf), "code": ""}])
            self.assertIn("公司代码", str(caught.exception))

    def test_手填行情时不再联网取价(self) -> None:
        """价格是唯一必须来自市场的数（股数、负债、现金都在年报里）。
        断网时由人填，但 market.csv 里必须写明「人工填写、没有交叉核对」。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = make_company_dir(Path(tmp))
            with mock.patch.dict(os.environ):
                summary = job.plan(
                    base,
                    job_id="test-plan-manual-prices",
                    transport=fake_transport,
                    manual_prices={
                        "000661": {"price": 71.83, "as_of": "2026-09-30"},
                        "300009": {"price": 8.61, "as_of": "2026-09-30"},
                    },
                )
            market = (base / "market.csv").read_text(encoding="utf-8")
            self.assertIn("000661,长春高新,target,71.83", market)
            self.assertIn("人工填写", market)
            self.assertTrue(summary["prices_manual"])
            self.assertEqual(summary["as_of"], "2026-09-30")

    def test_手填行情缺一家就停下(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = make_company_dir(Path(tmp))
            with mock.patch.dict(os.environ):
                with self.assertRaises(job.JobError) as caught:
                    job.plan(
                        base,
                        job_id="test-plan-manual-missing",
                        transport=fake_transport,
                        manual_prices={
                            "000661": {"price": 71.83, "as_of": "2026-09-30"}
                        },
                    )
            self.assertIn("300009", str(caught.exception))

    def test_手填行情的基准日必须一致(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = make_company_dir(Path(tmp))
            with mock.patch.dict(os.environ):
                with self.assertRaises(job.JobError) as caught:
                    job.plan(
                        base,
                        job_id="test-plan-manual-days",
                        transport=fake_transport,
                        manual_prices={
                            "000661": {"price": 71.83, "as_of": "2026-09-30"},
                            "300009": {"price": 8.61, "as_of": "2026-09-29"},
                        },
                    )
            self.assertIn("不一致", str(caught.exception))


    def test_手填股数补上抽不到的股本(self) -> None:
        """股数是每股价值的分母。有些年报就是抽不到这一行 —— 腾讯的股本在报表上
        用破折号占位（真数在附注）、苹果 10-K 把面值与股数拆成两行、非股份公司
        压根没有「股本」。没有这一格，这些公司会在估值那一步报
        「diluted_shares 必须为正数」，而人查得到的数字却没处填。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = make_company_dir(Path(tmp))
            with mock.patch.dict(os.environ):
                summary = job.plan(
                    base,
                    job_id="test-plan-manual-shares",
                    transport=fake_transport,
                    manual_prices={
                        "000661": {"price": 71.83, "as_of": "2026-09-30"},
                        "300009": {"price": 8.61, "as_of": "2026-09-30"},
                    },
                    manual_shares={"000661": 500, "300009": "800"},
                )
            market = (base / "market.csv").read_text(encoding="utf-8")
            self.assertIn("000661,长春高新,target,71.83,500,", market)
            self.assertIn("300009,安科生物,peer,8.61,800,", market)
            self.assertEqual(summary["shares_manual"], {"000661": 500.0, "300009": 800.0})

    def test_手填股数必须为正数(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = make_company_dir(Path(tmp))
            with mock.patch.dict(os.environ):
                with self.assertRaises(job.JobError) as caught:
                    job.plan(
                        base,
                        job_id="test-plan-bad-shares",
                        transport=fake_transport,
                        manual_prices={
                            "000661": {"price": 71.83, "as_of": "2026-09-30"},
                            "300009": {"price": 8.61, "as_of": "2026-09-30"},
                        },
                        manual_shares={"000661": 0},
                    )
            self.assertIn("必须为正数", str(caught.exception))


class RunTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_没跑过取数就明确报错(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "empty"
            base.mkdir()
            with self.assertRaises(job.JobError) as caught:
                job.run(base)
        self.assertIn("假设草稿", str(caught.exception))

    def test_草稿没填完就不许跑(self) -> None:
        """抽不到的字段（分红比例）没填之前不能跑 —— 与其让配置层抛一句
        「字段对不上」，不如在点按钮的时候就告诉人「这几项还没填」。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = make_company_dir(Path(tmp))
            with mock.patch.dict(os.environ):
                job.plan(base, job_id="test-plan-4", transport=fake_transport)
                with self.assertRaises(gen.DraftError) as caught:
                    job.run(base)
        self.assertIn("dividend_payout_ratio", str(caught.exception))

    def test_环境变量指向公司目录(self) -> None:
        env = job.company_env(Path("data/companies/000661"))
        self.assertEqual(env["FINAI_DATA_SOURCE"], "extracted")
        self.assertTrue(env["FINAI_ASSUMPTIONS"].endswith("assumptions.toml"))
        self.assertTrue(env["FINAI_COMPANY_DIR"].endswith("companies\\000661"))


def _rows(csv_text: str) -> list[dict[str, str]]:
    lines = [line for line in csv_text.splitlines() if line and not line.startswith("#")]
    header = lines[0].split(",")
    return [dict(zip(header, line.split(","))) for line in lines[1:]]


if __name__ == "__main__":
    unittest.main()
