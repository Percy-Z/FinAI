"""框架层的测试。

和 algorithm/tests/ 的区别：
    算法层的测试验算术（手算数字做断言）
    这里验装配 —— 配置读得对不对、串起来跑不跑得通、日志记没记上、
    出错时有没有拦住并说清楚

运行：
    python3 -m unittest discover -s tests
    或 make test

这个文件里有两条是真正的回归测试，看住两个具体的坑：
    test_fcff_两条路径必须一致      防止映射时漏减折旧率（漏了会高估约三成）
    test_逐年比率无法映射要报错      防止配置里写了逐年数组，跑到估值才炸
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

# 让测试可以从任何目录运行
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from algorithm.finai.dcf import project, value_dcf  # noqa: E402
from algorithm.finai.forecast import build_forecast  # noqa: E402
from algorithm.finai.types import ForecastAssumptions  # noqa: E402
from algorithm.finai.wacc import weights_from_market, wacc_from_market  # noqa: E402
from pipeline import config, run, tracing  # noqa: E402
from pipeline.adapters import (  # noqa: E402
    company_to_market_data,
    drivers_to_assumptions,
    fill_target_from_forecast,
)


# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------


def read_real_config():
    """读仓库里那份真实配置。测的是「配置和算法层能不能对上」。"""
    company, drivers, opening = config.load_case()
    params = config.load_wacc_params()
    terminal = config.load_terminal_params()
    target, peers = config.load_market_snapshot()
    return company, drivers, opening, params, terminal, target, peers


class CsvFixture(unittest.TestCase):
    """给需要临时 CSV 的测试用的小底座。"""

    def write_temp_csv(self, text: str) -> Path:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False, encoding="utf-8"
        )
        with handle:
            handle.write(text)
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return Path(handle.name)

    def market_csv_text(self) -> str:
        return config.DEFAULT_MARKET_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 一、配置层：能正确读出来
# ---------------------------------------------------------------------------


class TestConfigLoads(unittest.TestCase):
    def test_三个小节都读得出来(self):
        company, drivers, opening, params, terminal, target, peers = read_real_config()
        self.assertEqual(company.code, "T")
        self.assertEqual(drivers.years, 5)
        self.assertEqual(opening.total_assets, 385.0)
        self.assertGreater(params.beta, 0)
        self.assertIn(terminal.terminal_method, ("perpetuity", "exit_multiple"))
        self.assertEqual(target.code, "T")
        self.assertEqual(len(peers), 3)

    def test_基期资产负债表必须平(self):
        _, _, opening = read_real_config()[:3]
        self.assertAlmostEqual(
            opening.total_assets,
            opening.total_liabilities + opening.total_equity,
            places=6,
        )

    def test_逐年假设的个数必须等于预测期年数(self):
        drivers = config.load_case()[1]
        self.assertEqual(len(drivers.revenue_growth), drivers.years)
        # 注意：ForecastDrivers 里没有 ebit_margin 这个字段 ——
        # EBIT 利润率是「毛利 - 费用 - 折旧」推出来的，只在 ForecastAssumptions
        # （估值路径）里才作为一个独立假设存在。两个栈的字段并不一样。
        self.assertEqual(len(drivers.gross_margin), drivers.years)
        self.assertEqual(len(drivers.opex_pct_revenue), drivers.years)
        # 这几个「占收入比」虽然不是数组字段，但数据类会把它们广播成年数组
        self.assertEqual(len(drivers.da_pct_revenue), drivers.years)
        self.assertEqual(len(drivers.ar_pct_revenue), drivers.years)

    def test_市场快照里目标公司正好一家(self):
        target, peers = config.load_market_snapshot()
        self.assertEqual(target.code, "T")
        self.assertTrue(all(peer.code != target.code for peer in peers))

    def test_权重能从市场数据推导出来且归一(self):
        """这条测的是「三份配置能不能组合起来算出 WACC」。"""
        drivers = config.load_case()[1]
        params = config.load_wacc_params()
        target, _ = config.load_market_snapshot()

        equity_weight, debt_weight = weights_from_market(
            target.market_cap, target.interest_bearing_debt
        )
        self.assertAlmostEqual(equity_weight + debt_weight, 1.0, places=9)

        wacc = wacc_from_market(
            risk_free=params.risk_free,
            beta=params.beta,
            equity_risk_premium=params.equity_risk_premium,
            cost_of_debt=params.cost_of_debt,
            tax_rate=drivers.tax_rate,
            market_cap=target.market_cap,
            debt_value=target.interest_bearing_debt,
        )
        self.assertTrue(0.0 < wacc < 1.0)


# ---------------------------------------------------------------------------
# 二、配置层：坏数据必须被拦住
# ---------------------------------------------------------------------------


class TestConfigRejectsBadCsv(CsvFixture):
    def test_表头拼错要报错(self):
        broken = self.market_csv_text().replace("diluted_shares", "diluted_share")
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_market_snapshot(self.write_temp_csv(broken))
        self.assertIn("无法识别的列", str(ctx.exception))

    def test_数字列写了非数字要报错(self):
        broken = self.market_csv_text().replace(
            "T,目标公司,target,10.00", "T,目标公司,target,十块"
        )
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_market_snapshot(self.write_temp_csv(broken))
        self.assertIn("不是数字", str(ctx.exception))
        self.assertIn("第 2 行", str(ctx.exception))

    def test_role_写错要报错(self):
        broken = self.market_csv_text().replace("target,10.00", "boss,10.00", 1)
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_market_snapshot(self.write_temp_csv(broken))
        self.assertIn("role", str(ctx.exception))

    def test_没有目标公司要报错(self):
        broken = self.market_csv_text().replace(
            "T,目标公司,target", "T,目标公司,peer", 1
        )
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_market_snapshot(self.write_temp_csv(broken))
        self.assertIn("role=target", str(ctx.exception))

    def test_没有可比公司要报错(self):
        keep = [
            line
            for line in self.market_csv_text().splitlines()
            if line.startswith("#") or "role" in line or ",target," in line
        ]
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_market_snapshot(self.write_temp_csv("\n".join(keep) + "\n"))
        self.assertIn("peer", str(ctx.exception))

    def test_必填列留空要报错(self):
        broken = self.market_csv_text().replace(
            "target,10.00,100.00", "target,,100.00", 1
        )
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_market_snapshot(self.write_temp_csv(broken))
        self.assertIn("缺少必填的 price", str(ctx.exception))

    def test_可选列留空取零不报错(self):
        broken = self.market_csv_text().replace(
            "target,10.00,100.00,100.00,55.00", "target,10.00,100.00,100.00,", 1
        )
        target, _ = config.load_market_snapshot(self.write_temp_csv(broken))
        self.assertEqual(target.cash, 0.0)

    def test_注释行和表头行会被跳过(self):
        rows = config.read_csv(config.DEFAULT_MARKET_PATH)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["code"], "T")


class TestConfigRejectsBadToml(unittest.TestCase):
    def write_temp_toml(self, text: str) -> Path:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".toml", delete=False, encoding="utf-8"
        )
        with handle:
            handle.write(text)
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return Path(handle.name)

    def toml_text(self) -> str:
        return config.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")

    def test_字段名拼错要报错(self):
        broken = self.toml_text().replace("revenue_base ", "revenu_base ")
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_drivers(self.write_temp_toml(broken))
        self.assertIn("字段对不上", str(ctx.exception))

    def test_取值越界要报错(self):
        broken = self.toml_text().replace(
            "tax_rate                = 0.25", "tax_rate                = 0.90"
        )
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_drivers(self.write_temp_toml(broken))
        self.assertIn("取值不合法", str(ctx.exception))

    def test_缺少小节要报错(self):
        broken = self.toml_text().replace("[wacc]", "[wacc_typo]")
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_wacc_params(self.write_temp_toml(broken))
        self.assertIn("[wacc]", str(ctx.exception))

    def test_终值方法写错要报错(self):
        broken = self.toml_text().replace(
            'terminal_method = "perpetuity"', 'terminal_method = "永久增长"'
        )
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_terminal_params(self.write_temp_toml(broken))
        self.assertIn("terminal_method", str(ctx.exception))

    def test_选了退出倍数却不给倍数要报错(self):
        broken = self.toml_text().replace(
            'terminal_method = "perpetuity"', 'terminal_method = "exit_multiple"'
        )
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_terminal_params(self.write_temp_toml(broken))
        self.assertIn("exit_multiple", str(ctx.exception))

    def test_基期资产负债表不平要在配置层拦住(self):
        """数据自相矛盾属于「输入有问题」，不是代码问题。

        这条在配置层拦，报错就能指出是哪个文件；如果放到三表预测里，
        报错会是一段 Python 堆栈，只说差额多少。
        """
        broken = self.toml_text().replace(
            "cash                        = 55.0",
            "cash                        = 100.0",
        )
        path = self.write_temp_toml(broken)
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_case(path)
        message = str(ctx.exception)
        self.assertIn("[opening_balance]", message)
        self.assertIn("不平", message)
        self.assertIn("45.00", message)          # 差额
        self.assertIn("资产总计", message)        # 三个合计数都列出来了
        self.assertIn("负债合计", message)
        self.assertIn("权益合计", message)

        # 单独调这一个 loader 也要拦得住
        with self.assertRaises(config.ConfigError):
            config.load_opening_balance(path)

    def test_文件不存在要报错(self):
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_case(Path("/tmp/绝对不存在的配置文件.toml"))
        self.assertIn("不存在", str(ctx.exception))


# ---------------------------------------------------------------------------
# 三、两条预测路径必须一致（回归测试：防止漏减折旧率）
# ---------------------------------------------------------------------------


class TestTwoStackReconcile(unittest.TestCase):
    """栈A（估值路径）与栈B（三表路径）算出的 FCFF 必须相等。

    前提是基期口径对齐：期初应收/存货/应付 = 基期收入 x 各自的占比。
    这份配置满足这个条件（60 = 400x15%，40 = 400x10%，40 = 400x10%）。
    """

    def setUp(self):
        self.drivers = config.load_case()[1]
        self.opening = config.load_case()[2]
        self.terminal = config.load_terminal_params()
        self.forecast = build_forecast(self.opening, self.drivers)

    def test_基期口径是对齐的(self):
        """不对齐的话，两条路径第一年会差一个数，必须先满足这个前提。"""
        for field, pct, actual in [
            ("应收账款", "ar_pct_revenue", self.opening.accounts_receivable),
            ("存货", "inventory_pct_revenue", self.opening.inventory),
            ("应付账款", "ap_pct_revenue", self.opening.accounts_payable),
        ]:
            expected = self.drivers.revenue_base * getattr(self.drivers, pct)[0]
            self.assertAlmostEqual(
                actual, expected, places=6, msg=f"基期{field}与收入比例不对齐"
            )

    def test_fcff_两条路径必须一致(self):
        assumptions = drivers_to_assumptions(self.drivers, self.terminal)
        from_dcf_path = project(
            assumptions, base_year=self.drivers.base_year
        ).fcff
        from_forecast_path = self.forecast.fcff_series

        self.assertEqual(len(from_dcf_path), len(from_forecast_path))
        for index, (left, right) in enumerate(
            zip(from_dcf_path, from_forecast_path), start=1
        ):
            self.assertAlmostEqual(
                left,
                right,
                places=6,
                msg=f"第 {index} 年 FCFF 不一致：折现路径 {left:.6f}，三表路径 {right:.6f}",
            )

    def test_逐年比率无法映射到估值引擎(self):
        """栈B 允许逐年不同的资本开支率，栈A 只接受单一比率。

        这条测试锁住一个真实的接缝问题：类型注解骗过了构造，
        要到 project() 里才炸，而且报错完全看不出是哪个字段。
        """
        varying = [0.06, 0.07, 0.08, 0.09, 0.10]
        broken_assumptions = ForecastAssumptions(
            revenue_base=400.0,
            revenue_growth=(0.1, 0.1, 0.1, 0.1, 0.1),
            ebit_margin=(0.2,) * 5,
            tax_rate=0.25,
            da_pct_revenue=0.05,
            capex_pct_revenue=varying,  # 类型注解写的是 float
            nwc_pct_revenue=0.15,
        )
        # 构造阶段不报错 —— 这正是问题所在
        self.assertEqual(broken_assumptions.capex_pct_revenue, varying)
        # 但用的时候会炸
        with self.assertRaises(TypeError):
            project(broken_assumptions)

    def test_映射漏减折旧率会高估(self):
        """把「毛利 − 费用」当成 EBIT 利润率（漏减折旧），会高估多少。

        做法：在正确的假设上只改 ebit_margin 一项，其余参数全不动 ——
        这样差额就只可能来自这一项，能断言一个精确的关系。
        """
        correct = drivers_to_assumptions(self.drivers, self.terminal)
        wrong = replace(
            correct,
            ebit_margin=[
                gross - opex
                for gross, opex in zip(
                    self.drivers.gross_margin, self.drivers.opex_pct_revenue
                )
            ],
        )
        right = project(correct, base_year=self.drivers.base_year)
        inflated = project(wrong, base_year=self.drivers.base_year)

        for index, (correct_fcff, wrong_fcff) in enumerate(
            zip(right.fcff, inflated.fcff), start=1
        ):
            self.assertGreater(
                wrong_fcff, correct_fcff, msg=f"第 {index} 年应该被高估却没有"
            )
            # 差额有精确公式：折旧摊销 × (1 − 税率)
            # 因为折旧在 FCFF 里是「先扣掉再加回来」，净效果只剩抵税额；
            # 漏掉「先扣」等于把整笔折旧白送一遍。
            da = right.da[index - 1]
            self.assertAlmostEqual(
                wrong_fcff - correct_fcff,
                da * (1 - self.drivers.tax_rate),
                places=6,
                msg=f"第 {index} 年差额应等于 折旧 {da:.2f} × (1 − 税率)",
            )

        # 头一年就高估两成以上
        self.assertGreater(inflated.fcff[0] / right.fcff[0] - 1, 0.2)


# ---------------------------------------------------------------------------
# 三之二、适配层：三个转换各测一遍
# ---------------------------------------------------------------------------


class TestAdapters(unittest.TestCase):
    def setUp(self):
        _, self.drivers, self.opening = config.load_case()
        self.terminal = config.load_terminal_params()

    def test_转成假设后逐年字段长度对得上(self):
        assumptions = drivers_to_assumptions(self.drivers, self.terminal)
        self.assertEqual(len(assumptions.revenue_growth), self.drivers.years)
        self.assertEqual(len(assumptions.ebit_margin), self.drivers.years)

    def test_转成假设时终值参数带到(self):
        assumptions = drivers_to_assumptions(self.drivers, self.terminal)
        self.assertEqual(
            assumptions.terminal_method, self.terminal.terminal_method
        )
        self.assertAlmostEqual(
            assumptions.terminal_growth, self.terminal.terminal_growth
        )
        self.assertEqual(assumptions.mid_year, self.terminal.mid_year)

    def test_逐年比率不同时适配层要报错(self):
        """这是适配层存在的理由之一：把隐蔽的崩溃变成一句人话。

        如果不在适配层拦住，会一路走到 dcf.project() 里才炸，
        报错是「can't multiply sequence by non-int of type 'float'」。
        """
        varying = replace(
            self.drivers, capex_pct_revenue=(0.06, 0.07, 0.08, 0.09, 0.10)
        )
        with self.assertRaises(config.ConfigError) as ctx:
            drivers_to_assumptions(varying, self.terminal)
        message = str(ctx.exception)
        self.assertIn("capex_pct_revenue", message)
        self.assertIn("只支持单一比率", message)

    def test_公司转市场数据字段一致(self):
        target, _ = config.load_market_snapshot()
        market = company_to_market_data(target)

        self.assertEqual(market.price, target.price)
        self.assertEqual(market.diluted_shares, target.diluted_shares)
        self.assertEqual(
            market.interest_bearing_debt, target.interest_bearing_debt
        )
        self.assertEqual(market.cash, target.cash)
        self.assertEqual(market.as_of, target.as_of)
        # 派生量也要对得上
        self.assertAlmostEqual(market.market_cap, target.market_cap)
        self.assertAlmostEqual(market.net_debt, target.net_debt)

    def test_填进预测后利润指标来自预测第一年(self):
        target, _ = config.load_market_snapshot()
        forecast = build_forecast(self.opening, self.drivers)
        filled = fill_target_from_forecast(target, forecast)
        first = forecast.years[0].income

        self.assertAlmostEqual(
            filled.net_profit_attributable, first.net_profit_attributable
        )
        self.assertAlmostEqual(filled.revenue, first.revenue)
        self.assertAlmostEqual(filled.ebit, first.ebit)
        self.assertAlmostEqual(filled.ebitda, first.ebitda)
        # 净资产取基期（抽取层就绪前是暂时的口径）
        self.assertAlmostEqual(
            filled.book_equity_attributable, self.opening.equity_attributable
        )
        # 市场数据没被动过
        self.assertEqual(filled.price, target.price)
        self.assertEqual(filled.diluted_shares, target.diluted_shares)

    def test_填完之后能算出倍数(self):
        target, _ = config.load_market_snapshot()
        forecast = build_forecast(self.opening, self.drivers)
        filled = fill_target_from_forecast(target, forecast)
        self.assertIsNotNone(filled.eps)
        self.assertGreater(filled.eps, 0)


# ---------------------------------------------------------------------------
# 四、端到端：跑一整遍
# ---------------------------------------------------------------------------


class TestEndToEnd(unittest.TestCase):
    """在临时目录里跑完整流水线，不污染真实的 outputs/ 和 logs/。"""

    def run_pipeline(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        logs = root / "logs"

        with mock.patch.object(run, "OUTPUT_ROOT", root / "outputs"), mock.patch.object(
            run, "RunTrace", lambda run_id: tracing.RunTrace(run_id, log_root=logs)
        ):
            with redirect_stdout(io.StringIO()):
                exit_code = run.main()
        return exit_code, root

    def test_跑完返回零(self):
        exit_code, _ = self.run_pipeline()
        self.assertEqual(exit_code, 0)

    def test_产物写出来了而且内容对(self):
        _, root = self.run_pipeline()
        produced = list((root / "outputs").glob("*/projections.json"))
        self.assertEqual(len(produced), 1)

        payload = json.loads(produced[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["company"]["code"], "T")
        self.assertEqual(len(payload["years"]), 5)
        # 产物要「自解释」：假设必须完整写在里面
        self.assertIn("revenue_base", payload["assumptions"])
        self.assertIn("gross_margin", payload["assumptions"])
        self.assertEqual(len(payload["summary"]["fcff_series"]), 5)

    def test_产物内容与三表预测一致(self):
        _, root = self.run_pipeline()
        produced = next((root / "outputs").glob("*/projections.json"))
        payload = json.loads(produced.read_text(encoding="utf-8"))

        drivers = config.load_case()[1]
        opening = config.load_case()[2]
        expected = build_forecast(opening, drivers).fcff_series
        for got, want in zip(payload["summary"]["fcff_series"], expected):
            self.assertAlmostEqual(got, want, places=6)

    def test_两次运行互不覆盖(self):
        first_code, root = self.run_pipeline()
        self.assertEqual(first_code, 0)
        with mock.patch.object(run, "new_run_id", lambda: "固定编号"):
            with mock.patch.object(
                run, "OUTPUT_ROOT", root / "outputs"
            ), mock.patch.object(
                run,
                "RunTrace",
                lambda run_id: tracing.RunTrace(run_id, log_root=root / "logs"),
            ):
                with redirect_stdout(io.StringIO()):
                    run.main()
        # 编号是固定的，所以第二次会写进同一个目录（这符合设计）
        self.assertTrue((root / "outputs" / "固定编号" / "projections.json").exists())

    def test_估值产物的_WACC_里有完整的七项(self):
        """报告里要列出 WACC 的完整构成 —— 四个输入 + 一个复用 + 两个推导。"""
        _, root = self.run_pipeline()
        produced = next((root / "outputs").glob("*/valuation.json"))
        payload = json.loads(produced.read_text(encoding="utf-8"))
        wacc = payload["wacc"]

        for field in [
            "value",
            "risk_free",           # 输入
            "beta",                # 输入
            "equity_risk_premium",  # 输入
            "cost_of_debt",        # 输入
            "tax_rate",            # 复用 [drivers]
            "equity_weight",       # 推导
            "debt_weight",         # 推导
        ]:
            self.assertIn(field, wacc, msg=f"WACC 缺少 {field}")

        # 权重必须归一
        self.assertAlmostEqual(
            wacc["equity_weight"] + wacc["debt_weight"], 1.0, places=9
        )
        # 税率必须与三表预测用的是同一个
        self.assertAlmostEqual(wacc["tax_rate"], config.load_case()[1].tax_rate)

    def test_估值产物里的每股价值与算法层一致(self):
        """端到端算出的每股价值，必须等于直接调算法模块的结果。

        这条保护「编排没有引入数值错误」：框架只负责传参和落盘，
        不应该改变任何数字。
        """
        _, root = self.run_pipeline()
        produced = next((root / "outputs").glob("*/valuation.json"))
        payload = json.loads(produced.read_text(encoding="utf-8"))

        _, drivers, opening = config.load_case()
        assumptions = drivers_to_assumptions(drivers, config.load_terminal_params())
        target, _ = config.load_market_snapshot()
        expected = value_dcf(
            assumptions,
            company_to_market_data(target),
            payload["wacc"]["value"],
            base_year=drivers.base_year,
        )
        self.assertAlmostEqual(
            payload["valuation"]["per_share"], expected.per_share, places=9
        )
        self.assertAlmostEqual(
            payload["valuation"]["enterprise_value"],
            expected.enterprise_value,
            places=6,
        )

    def test_同一份配置跑两次产物只有_run_id_不同(self):
        """可复现性：同样的输入，产物除了运行标识之外逐字段相同。

        为什么不比文件哈希：产物里写着 run_id（为了让单独拿出一个文件
        也知道它属于哪次运行），所以哈希必然不同。要证明的是「结果可复现」
        —— 那就逐字段比，把 run_id 排除掉。

        这条同时也在防「计算不确定」：如果哪天引入了依赖字典顺序、
        随机数、当前时间之类的计算，这里会挂。
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        def run_as(run_id: str) -> None:
            with mock.patch.object(run, "new_run_id", lambda: run_id), mock.patch.object(
                run, "OUTPUT_ROOT", root / "outputs"
            ), mock.patch.object(
                run,
                "RunTrace",
                lambda rid: tracing.RunTrace(rid, log_root=root / "logs"),
            ):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(run.main(), 0, msg=f"以 {run_id} 为标识的运行失败")

        run_as("第一次")
        run_as("第二次")

        for name in ("projections.json", "valuation.json"):
            first = json.loads((root / "outputs" / "第一次" / name).read_text("utf-8"))
            second = json.loads((root / "outputs" / "第二次" / name).read_text("utf-8"))

            # 各自的运行标识是对的
            self.assertEqual(first["run_id"], "第一次")
            self.assertEqual(second["run_id"], "第二次")

            # 除了运行标识，其余逐字段相同
            self.assertEqual(
                {key: value for key, value in first.items() if key != "run_id"},
                {key: value for key, value in second.items() if key != "run_id"},
                msg=f"{name} 除了 run_id 之外还有差异，说明存在不可复现的地方",
            )


# ---------------------------------------------------------------------------
# 五、日志层
# ---------------------------------------------------------------------------


class TestTracing(unittest.TestCase):
    def test_轨迹每一步一行且是合法_json(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        with mock.patch.object(run, "OUTPUT_ROOT", root / "outputs"), mock.patch.object(
            run,
            "RunTrace",
            lambda run_id: tracing.RunTrace(run_id, log_root=root / "logs"),
        ):
            with redirect_stdout(io.StringIO()):
                run.main()

        trace_file = next((root / "logs").glob("*/trace.jsonl"))
        lines = [line for line in trace_file.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(lines), 6)

        steps = []
        for index, line in enumerate(lines, start=1):
            entry = json.loads(line)  # 每行都是一个独立完整的 JSON
            self.assertEqual(entry["seq"], index)
            self.assertEqual(entry["status"], "ok")
            self.assertIsNotNone(entry["duration_s"])
            steps.append(entry["step"])
        # 步骤名和顺序都锁住：加阶段时这条会挂，提醒你确认是有意的
        self.assertEqual(
            steps,
            [
                "load_config",
                "forecast",
                "wacc",
                "valuation",
                "write_projections",
                "write_valuation",
            ],
        )

    def test_清单记了代码版本与全部产物哈希(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        with mock.patch.object(run, "OUTPUT_ROOT", root / "outputs"), mock.patch.object(
            run,
            "RunTrace",
            lambda run_id: tracing.RunTrace(run_id, log_root=root / "logs"),
        ):
            with redirect_stdout(io.StringIO()):
                run.main()

        manifest_file = next((root / "logs").glob("*/run_manifest.json"))
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))

        self.assertEqual(manifest["status"], "ok")
        self.assertIn("commit", manifest["code"])          # 代码版本
        self.assertIn("dirty", manifest["code"])
        self.assertTrue(manifest["inputs"])                 # 输入文件哈希
        self.assertTrue(manifest["outputs"])                # 产物哈希
        self.assertIsNotNone(manifest["trace_sha256"])      # 轨迹自身的哈希
        self.assertEqual(manifest["model_versions"], {})     # 给 AI 层留的位置


# ---------------------------------------------------------------------------
# 六、失败路径：出错时要留证据
# ---------------------------------------------------------------------------


class TestFailurePath(unittest.TestCase):
    def test_配置出错时返回一并且留下失败记录(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        def boom(*_args, **_kwargs):
            raise config.ConfigError("模拟的配置错误：税率写成了 0.9")

        with mock.patch.object(run, "load_case", boom), mock.patch.object(
            run, "OUTPUT_ROOT", root / "outputs"
        ), mock.patch.object(
            run,
            "RunTrace",
            lambda run_id: tracing.RunTrace(run_id, log_root=root / "logs"),
        ):
            with redirect_stdout(io.StringIO()):
                exit_code = run.main()

        self.assertEqual(exit_code, 1)

        trace_file = next((root / "logs").glob("*/trace.jsonl"))
        entry = json.loads(trace_file.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["status"], "failed")
        self.assertIn("模拟的配置错误", entry["extra"]["error"])

        manifest_file = next((root / "logs").glob("*/run_manifest.json"))
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")
        # 失败的清单里也要能查到当时读的是哪份配置
        self.assertTrue(manifest["inputs"], msg="失败记录没记输入文件")
        self.assertTrue(
            any(path.endswith("assumptions.toml") for path in manifest["inputs"])
        )

    def test_代码出错时也要留下失败记录(self):
        """不是配置问题、而是代码 bug 时，同样要留证据，但堆栈要抛出去。

        两类失败的处理方式不同：
            配置错 → 一句人话，退出码 1
            代码错 → 留记录，然后把异常抛出去（排查需要堆栈）
        但「留下记录」这一条对两类都成立。
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        def boom(*_args, **_kwargs):
            raise RuntimeError("模拟的代码 bug")

        with mock.patch.object(run, "load_case", boom), mock.patch.object(
            run, "OUTPUT_ROOT", root / "outputs"
        ), mock.patch.object(
            run,
            "RunTrace",
            lambda rid: tracing.RunTrace(rid, log_root=root / "logs"),
        ):
            with redirect_stdout(io.StringIO()):
                # 异常必须继续往外抛 —— 掩盖 bug 比丢记录更糟
                with self.assertRaises(RuntimeError):
                    run.main()

        manifest_file = next((root / "logs").glob("*/run_manifest.json"))
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")

        trace_file = next((root / "logs").glob("*/trace.jsonl"))
        entry = json.loads(trace_file.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["tool"], "RuntimeError")
        self.assertIn("模拟的代码 bug", entry["extra"]["error"])

    def test_算不出来哈希也不许让流水线崩(self):
        """日志层的铁律：记不了就记 unknown，但不能打断正事。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        trace = tracing.RunTrace("测试编号", log_root=Path(tmp.name))
        entry = trace.record(
            step="读一个不存在的文件",
            inputs=[Path("/tmp/这个文件肯定不存在.dat")],
        )
        self.assertIn("unavailable", list(entry["inputs"].values())[0])


if __name__ == "__main__":
    unittest.main()
