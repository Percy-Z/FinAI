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

import csv
import io
import json
import re
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar, cast
from unittest import mock

# 让测试可以从任何目录运行
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from algorithm.finai.dcf import project, value_dcf  # noqa: E402
from algorithm.finai.forecast import (  # noqa: E402
    as_series,
    build_forecast,
    to_snapshots,
)
from algorithm.finai.types import ForecastAssumptions  # noqa: E402
from algorithm.finai.validate import run_checks  # noqa: E402
from algorithm.finai.wacc import weights_from_market, wacc_from_market  # noqa: E402
from algorithm.output.pdf import HAS_REPORTLAB  # noqa: E402
from pipeline import config, run, tracing  # noqa: E402
from pipeline.check_run import (  # noqa: E402
    REQUIRED_CHARTS,
    REQUIRED_DATA_FILES,
    REQUIRED_REPORTS,
    check_run,
    newest_run,
)
from pipeline.adapters import (  # noqa: E402
    company_to_market_data,
    drivers_to_assumptions,
    fill_peers_from_financials,
    fill_target_from_forecast,
)


# ---------------------------------------------------------------------------
# 全局桩：本模块的测试绝不调用真实的大模型接口
# ---------------------------------------------------------------------------
#
# 这件事踩过坑：仓库根目录放着 apikey.txt，于是 run.main() 里的叙述阶段
# 会真的发请求，每个端到端用例多花三分钟，还会产生费用、
# 在别人的机器上（没 key）表现不一致。
#
# 测试必须是自己自足的：不依赖环境里的凭据、不依赖网络、不花钱。
# 想看真实输出，跑 make run，不要跑 make test。

_llm_patcher: Any = None


def _stub_narrative(
    facts: dict[str, Any], request: Any, log_dir: Any = None
) -> Any:
    """假的叙述结果：结构完整、内容为空，等价于「没有 key 时的降级」。"""
    from algorithm.output.narrative import PROMPT_VERSION, NarrativeResult

    return NarrativeResult(
        ok=False,
        model=request.model,
        endpoint=request.endpoint,
        prompt_version=PROMPT_VERSION,
        temperature=request.temperature,
        notes=["测试桩：本用例不调用真实大模型接口"],
    )


def setUpModule() -> None:
    global _llm_patcher
    _llm_patcher = mock.patch("pipeline.run.write_narrative", _stub_narrative)
    _llm_patcher.start()


def tearDownModule() -> None:
    if _llm_patcher is not None:
        _llm_patcher.stop()


# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------


T = TypeVar("T")


def require(value: T | None, what: str = "值") -> T:
    """断言不为 None 并把值返回，好让类型检查器也能确定类型。

    CompanyMetrics 的利润类指标都是 float | None —— 市场快照里本来没有这些数，
    要等适配层从三表预测或 peers.csv 填进去。测试里大量出现「先断言填好了、
    再拿它去算」的写法，而 unittest 的 assertIsNotNone 不会让类型检查器
    收窄类型，所以统一走这个函数。
    """
    if value is None:
        raise AssertionError(f"{what} 不该是 None")
    return value


def trace_factory(log_root: Path) -> Callable[[str], tracing.RunTrace]:
    """造一个替代 RunTrace 的工厂，供 mock.patch.object 使用。

    为什么不直接写 lambda：lambda 的参数没法标类型，严格模式下类型检查器
    会报 reportUnknownLambdaType。换成带注解的内层函数即可，
    顺便把「日志目录」收进闭包 —— 原先七个调用点各写一遍。
    """

    def make(run_id: str) -> tracing.RunTrace:
        return tracing.RunTrace(run_id, log_root=log_root)

    return make


def run_pipeline_into(root: Path) -> None:
    """在指定目录里完整跑一遍流水线：产物与日志都收进去，屏幕输出丢掉。

    两个测试类都要用（一个验产物验收、一个验估值折现用的是哪一份现金流），
    所以放在模块层，避免各写一份。
    """
    with mock.patch.object(run, "OUTPUT_ROOT", root / "runs"), mock.patch.object(
        run, "RunTrace", trace_factory(root / "logs")
    ):
        with redirect_stdout(io.StringIO()):
            run.main()


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
        self.assertEqual(len(as_series(drivers.revenue_growth)), drivers.years)
        # 注意：ForecastDrivers 里没有 ebit_margin 这个字段 ——
        # EBIT 利润率是「毛利 - 费用 - 折旧」推出来的，只在 ForecastAssumptions
        # （估值路径）里才作为一个独立假设存在。两个栈的字段并不一样。
        self.assertEqual(len(as_series(drivers.gross_margin)), drivers.years)
        self.assertEqual(len(as_series(drivers.opex_pct_revenue)), drivers.years)
        # 这几个「占收入比」虽然不是数组字段，但数据类会把它们广播成年数组
        self.assertEqual(len(as_series(drivers.da_pct_revenue)), drivers.years)
        self.assertEqual(len(as_series(drivers.ar_pct_revenue)), drivers.years)

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

    def per_share_of_fresh_run(self) -> float:
        """跑一遍完整流水线，取这次的每股价值（产物落临时目录）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        run_pipeline_into(root)
        run_dir = next((root / "runs").iterdir())
        payload = json.loads(
            (run_dir / "03_valuation.json").read_text(encoding="utf-8")
        )
        return float(payload["per_share"])

    def test_估值折现用的是三表预测的现金流(self):
        """改掉三表算出的现金流，估值必须跟着变。

        这件事以前不成立：估值模块自己按单值比率重算一遍现金流，两条路径只靠
        上一条回归测试盯着数值相等 —— 改三表的序列不会影响估值。现在估值直接
        吃三表的序列（README 下一步第 2 条），序列一变，每股价值立刻跟着变。
        """
        from algorithm.finai.forecast import ThreeStatementForecast

        original = ThreeStatementForecast.fcff_series
        baseline = self.per_share_of_fresh_run()
        with mock.patch.object(
            ThreeStatementForecast,
            "fcff_series",
            property(lambda self: [value * 1.2 for value in original.fget(self)]),
        ):
            bumped = self.per_share_of_fresh_run()
        self.assertNotAlmostEqual(
            baseline,
            bumped,
            places=6,
            msg="三表的现金流改了 20%，估值却没动 —— 说明折现用的还是自己重算的那一份",
        )

    def test_逐年比率无法映射到估值引擎(self):
        """每年不同的资本开支率，在估值模块里「构造那一刻」就会被拒绝。

        行为变化记录（2026-09-29 合并 algorithm_finance 之后）：

            合并前  类型注解写的是 float 但不校验
                    → 构造能过，到 project() 里才炸
                    → 报错「can't multiply sequence by non-int of type 'float'」

            合并后  算法层在 __post_init__ 里用 require_finite 校验
                    → 构造时立刻拒绝

        所以这条测试现在断言「构造就报错」。
        适配层另外还拦一道，给出的是配置里的字段名 —— 见 TestAdapters
        里的 test_逐年比率不同时适配层要报错。两道不重复。
        """
        varying = [0.06, 0.07, 0.08, 0.09, 0.10]
        with self.assertRaises(TypeError):
            ForecastAssumptions(
                revenue_base=400.0,
                revenue_growth=(0.1, 0.1, 0.1, 0.1, 0.1),
                ebit_margin=(0.2,) * 5,
                tax_rate=0.25,
                da_pct_revenue=0.05,
                # 估值模块只接受单个数字，这里故意传数组 —— 验的就是运行时那道
                # 拦截。cast 是写给类型检查器的：这个类型错误是「故意犯」的。
                capex_pct_revenue=cast(Any, varying),
                nwc_pct_revenue=0.15,
            )

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
                    as_series(self.drivers.gross_margin),
                    as_series(self.drivers.opex_pct_revenue),
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
            require(filled.net_profit_attributable), first.net_profit_attributable
        )
        self.assertAlmostEqual(require(filled.revenue), first.revenue)
        self.assertAlmostEqual(require(filled.ebit), first.ebit)
        self.assertAlmostEqual(require(filled.ebitda), first.ebitda)
        # 净资产取基期（抽取层就绪前是暂时的口径）
        self.assertAlmostEqual(
            require(filled.book_equity_attributable), self.opening.equity_attributable
        )
        # 市场数据没被动过
        self.assertEqual(filled.price, target.price)
        self.assertEqual(filled.diluted_shares, target.diluted_shares)

    def test_填完之后能算出倍数(self):
        target, _ = config.load_market_snapshot()
        forecast = build_forecast(self.opening, self.drivers)
        filled = fill_target_from_forecast(target, forecast)
        self.assertGreater(require(filled.eps, "目标公司的 EPS"), 0)

    def test_可比公司填进财务数据后能算出倍数(self):
        """市场快照只有股价股数债务，PE / PB 需要的分母来自 peers.csv。"""
        _, peers = config.load_market_snapshot()
        financials = config.load_peer_financials()
        filled, notes = fill_peers_from_financials(peers, financials)

        self.assertEqual(len(filled), len(peers))
        self.assertEqual(notes, [], "三家可比公司都应该在 peers.csv 里找到")
        for peer in filled:
            self.assertGreater(require(peer.eps, f"{peer.code} 的 EPS"), 0)
            self.assertGreater(
                require(peer.book_value_per_share, f"{peer.code} 的每股净资产"), 0
            )

    def test_缺财务数据的可比公司只提示不中断(self):
        """少一家公司的财务数据不该让整条流水线跑不下去。"""
        _, peers = config.load_market_snapshot()
        financials = config.load_peer_financials()
        missing_code = peers[0].code
        financials.pop(missing_code)

        filled, notes = fill_peers_from_financials(peers, financials)

        self.assertEqual(len(filled), len(peers), "缺数据也要保留，只是算不出倍数")
        self.assertEqual(len(notes), 1)
        self.assertIn(missing_code, notes[0])
        # 缺的那家算不出 EPS，其余的照常
        self.assertIsNone(filled[0].eps)
        self.assertIsNotNone(filled[1].eps)


# ---------------------------------------------------------------------------
# 四、端到端：跑一整遍
# ---------------------------------------------------------------------------


class TestEndToEnd(unittest.TestCase):
    """在临时目录里跑完整流水线，不污染真实的 runs/ 和 logs/。"""

    def run_pipeline(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        logs = root / "logs"

        with mock.patch.object(run, "OUTPUT_ROOT", root / "runs"), mock.patch.object(
            run, "RunTrace", trace_factory(logs)
        ):
            with redirect_stdout(io.StringIO()):
                exit_code = run.main()
        return exit_code, root

    def test_跑完返回零(self):
        exit_code, _ = self.run_pipeline()
        self.assertEqual(exit_code, 0)

    def test_产物写出来了而且内容对(self):
        _, root = self.run_pipeline()
        produced = list((root / "runs").glob("*/01_projections.json"))
        self.assertEqual(len(produced), 1)

        payload = json.loads(produced[0].read_text(encoding="utf-8"))
        self.assertEqual(len(payload["years"]), 5)
        self.assertAlmostEqual(payload["worst_balance_gap"], 0.0, places=6)

    def test_这次运行的假设记在清单里(self):
        """输出层的产物文件只放「结果」，不放假设。

        假设、基期数据、WACC 构成都记在清单的 run_parameters 里 ——
        这样产物目录是自解释的：打开 runs/<编号>/ 就能看到这批数字
        是哪套假设算出来的，不用回去翻配置文件（配置文件日后会变）。
        """
        _, root = self.run_pipeline()
        manifest = json.loads(
            next((root / "runs").glob("*/10_run_manifest.json")).read_text(
                encoding="utf-8"
            )
        )
        params = manifest["inputs"]["run_parameters"]

        self.assertEqual(params["company"]["code"], "T")
        # 经营假设要完整
        self.assertIn("revenue_base", params["assumptions"])
        self.assertIn("gross_margin", params["assumptions"])
        # 基期数据要完整
        self.assertIn("cash", params["opening_balance"])
        self.assertIn("retained_earnings", params["opening_balance"])

        # 而且与配置文件里的一致
        _, drivers, opening = config.load_case()
        self.assertAlmostEqual(
            params["assumptions"]["revenue_base"], drivers.revenue_base
        )
        self.assertAlmostEqual(params["opening_balance"]["cash"], opening.cash)

    def test_产物内容与三表预测一致(self):
        _, root = self.run_pipeline()
        produced = next((root / "runs").glob("*/01_projections.json"))
        payload = json.loads(produced.read_text(encoding="utf-8"))

        _, drivers, opening = config.load_case()
        expected = build_forecast(opening, drivers).fcff_series
        got = [year["fcff"] for year in payload["years"]]
        self.assertEqual(len(got), len(expected))
        for left, right in zip(got, expected):
            self.assertAlmostEqual(left, right, places=6)

    def test_勾稽校验产物逐年都有且预测期无失败(self):
        """勾稽校验：把预测出来的三张表反过来验一遍。

        产物里每年一份报告，每份 11 项检查。注意「跳过」不等于通过 ——
        预测里没有非经常性损益，所以 X-03 每年都会跳过。
        """
        _, root = self.run_pipeline()
        produced = next((root / "runs").glob("*/02_validation_report.json"))
        payload = json.loads(produced.read_text(encoding="utf-8"))

        _, drivers, _ = config.load_case()
        self.assertEqual(payload["count"], drivers.years)
        self.assertEqual(len(payload["reports"]), drivers.years)

        for report in payload["reports"]:
            counts = report["summary"]
            # 预测出来的三张表内部应当自洽：一项失败都不该有
            self.assertEqual(
                counts["fail"], 0, msg=f"{report['fiscal_year']} 年有校验失败"
            )
            # 至少 11 项（算法侧以后加检查项，这里不该挂）
            self.assertGreaterEqual(counts["total"], 11)
            # 四项结论加起来等于总项数（跳过也是其中一项）
            self.assertEqual(
                counts["pass"] + counts["warning"] + counts["fail"] + counts["skipped"],
                counts["total"],
            )
            # 预测期没有非经常性损益 → 跳过的是 X-03
            skipped = {c["check_id"] for c in report["checks"] if c["status"] == "skipped"}
            self.assertIn("X-03", skipped, msg="预测里没有非经常性损益，X-03 应当跳过")

    def test_校验产物的结论与算法层一致(self):
        """框架只负责把校验结果搬进产物，不应该改变任何结论。

        这是「框架不引入错误」的又一处证明 —— 与估值那条同一个思路。
        """
        _, root = self.run_pipeline()
        produced = next((root / "runs").glob("*/02_validation_report.json"))
        payload = json.loads(produced.read_text(encoding="utf-8"))

        _, drivers, opening = config.load_case()
        forecast = build_forecast(opening, drivers)
        expected = [run_checks(snapshot) for snapshot in to_snapshots(forecast)]

        self.assertEqual(len(payload["reports"]), len(expected))
        for got, want in zip(payload["reports"], expected):
            self.assertEqual(got["summary"], want.summary)
            self.assertEqual(
                [c["check_id"] for c in got["checks"]],
                [c.check_id for c in want.checks],
            )
            self.assertEqual(
                [c["status"] for c in got["checks"]],
                [c.status for c in want.checks],
            )

    def test_同一编号跑两次会被明确拒绝(self):
        """产物目录已有内容时，输出层的 export_run 会拒绝覆盖。

        这比静默覆盖好：静默覆盖会让人以为看到的是最新结果，
        其实可能是上一次留下的。所以运行编号带毫秒，避免同一秒撞车。
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        def run_as_fixed_id() -> int:
            with mock.patch.object(
                run, "new_run_id", lambda: "固定编号"
            ), mock.patch.object(
                run, "OUTPUT_ROOT", root / "runs"
            ), mock.patch.object(
                run,
                "RunTrace",
                trace_factory(root / "logs"),
            ):
                with redirect_stdout(io.StringIO()):
                    return run.main()

        # 第一次：正常写出
        self.assertEqual(run_as_fixed_id(), 0)
        self.assertTrue((root / "runs" / "固定编号" / "01_projections.json").exists())

        # 第二次：同一个编号，目录已有内容 → 明确报错，而不是悄悄覆盖
        with self.assertRaises(FileExistsError):
            run_as_fixed_id()

    def test_清单里记了_WACC_的完整七项(self):
        """报告里要列出 WACC 的完整构成 —— 四个输入 + 一个复用 + 两个推导。

        产物文件（03_valuation.json）只放估值结果和 WACC 的数值；
        完整构成记在清单的 run_parameters 里，写报告时从这一处引用。
        """
        _, root = self.run_pipeline()
        manifest = json.loads(
            next((root / "runs").glob("*/10_run_manifest.json")).read_text(
                encoding="utf-8"
            )
        )
        wacc = manifest["inputs"]["run_parameters"]["wacc"]

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
        produced = next((root / "runs").glob("*/03_valuation.json"))
        payload = json.loads(produced.read_text(encoding="utf-8"))

        _, drivers, _ = config.load_case()
        assumptions = drivers_to_assumptions(drivers, config.load_terminal_params())
        target, _ = config.load_market_snapshot()
        expected = value_dcf(
            assumptions,
            company_to_market_data(target),
            payload["wacc"],
            base_year=drivers.base_year,
        )
        self.assertAlmostEqual(
            payload["per_share"], expected.per_share, places=9
        )
        self.assertAlmostEqual(
            payload["enterprise_value"], expected.enterprise_value, places=6
        )

    def test_同一份配置跑两次数据产物逐字节相同(self):
        """可复现性：同样的输入 → 完全相同的数据产物，一个字节都不差。

        输出层的产物文件里不含运行标识和时间戳，所以可以直接比字节 ——
        这比「逐字段比对」更强。清单里必然有 run_id 和 created_at
        （那是运行标识和时刻），但清单记录的实质内容两次必须一致。

        这条同时也在防「计算不确定」：如果哪天引入了依赖字典顺序、
        随机数、当前时间之类的计算，这里会挂。
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        def run_as(run_id: str) -> None:
            with mock.patch.object(run, "new_run_id", lambda: run_id), mock.patch.object(
                run, "OUTPUT_ROOT", root / "runs"
            ), mock.patch.object(
                run,
                "RunTrace",
                trace_factory(root / "logs"),
            ):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(run.main(), 0, msg=f"以 {run_id} 为标识的运行失败")

        run_as("第一次")
        run_as("第二次")

        # 数据产物逐字节相同
        for name in ("01_projections.json", "03_valuation.json"):
            first = (root / "runs" / "第一次" / name).read_bytes()
            second = (root / "runs" / "第二次" / name).read_bytes()
            self.assertEqual(first, second, msg=f"{name} 两次运行的内容不完全相同")

        # 报告：正文可复现，但抬头里的运行编号与出具时间随运行变化，
        # 所以整份文件不是逐字节相同。去掉那两行之后必须完全一致。
        def report_body(text: str) -> str:
            return "\n".join(
                line
                for line in text.splitlines()
                if "运行编号" not in line and "出具时间" not in line
            )

        self.assertEqual(
            report_body((root / "runs" / "第一次" / "17_report.md").read_text("utf-8")),
            report_body((root / "runs" / "第二次" / "17_report.md").read_text("utf-8")),
            msg="报告正文两次运行不一致（抬头的时间戳已经排除）",
        )

        # 清单：run_id 与 created_at 不同（那是运行时产生的），
        # 但记录的实质内容必须一致
        first_manifest = json.loads(
            (root / "runs" / "第一次" / "10_run_manifest.json").read_text("utf-8")
        )
        second_manifest = json.loads(
            (root / "runs" / "第二次" / "10_run_manifest.json").read_text("utf-8")
        )
        self.assertEqual(first_manifest["run_id"], "第一次")
        self.assertEqual(second_manifest["run_id"], "第二次")

        def reproducible_part(manifest: dict[str, Any]) -> dict[str, Any]:
            """清单里「应该可复现」的部分。

            排除三样必然不同的：run_id（运行标识）、created_at（时刻）、
            steps 里的 duration_s（耗时）。其余全比 —— 包括 outputs 的哈希、
            代码版本、输入文件哈希、参数快照。

            这样比「只挑几个字段比」严：以后往 meta 里新加了什么，
            只要它是不可复现的，这条测试就会挂。
            """
            inputs = {
                key: value
                for key, value in manifest["inputs"].items()
                if key != "steps"
            }
            # 叙述元信息里的耗时与日志路径由运行标识和时刻派生，天然不同；
            # 其余（模型名、提示词版本、提示词与响应哈希、token 用量）必须一致。
            if "narrative" in inputs:
                inputs["narrative"] = {
                    key: value
                    for key, value in inputs["narrative"].items()
                    if key not in ("elapsed_s", "raw_path")
                }
            return {
                "inputs": inputs,
                # 报告文件的内容含运行编号与出具时间，哈希自然每次不同。
                # 报告正文的可复现性由上面那条单独比。
                "outputs": [
                    entry
                    for entry in manifest["outputs"]
                    # 报告三种格式的内容都含运行编号与出具时间，哈希每次不同；
                    # PDF 还额外含 reportlab 写入的生成时间戳，即使内容一样
                    # 字节也不可能相同。所以三者都排除 —— 报告正文的可复现性
                    # 由上面那条单独比（比的是 Markdown）。
                    if entry["name"] not in ("report", "report_html", "report_pdf")
                ],
                "warnings": manifest["warnings"],
            }

        self.assertEqual(
            reproducible_part(first_manifest),
            reproducible_part(second_manifest),
            msg="清单里除了运行标识、时刻、步骤耗时之外，还有不一致的地方",
        )

    # ── 后半段四个阶段：敏感性 / 反向 / 相对估值 / 足球场图 ──────────

    def artifact(self, root: Path, name: str) -> Path:
        """取本次运行产出的某个文件。"""
        return next((root / "runs").glob(f"*/{name}"))

    def test_十个数据文件与六张图全部产出(self):
        """锁住「九个阶段都接上了」——少接一个阶段就会少几个文件。"""
        _, root = self.run_pipeline()
        directory = next((root / "runs").iterdir())

        data_files = sorted(
            path.name for path in directory.glob("[0-9][0-9]_*") if path.is_file()
        )
        self.assertEqual(
            data_files,
            [
                "01_projections.json",
                "02_validation_report.json",
                "03_valuation.json",
                "04_tornado.csv",
                "05_sensitivity.csv",
                "06_reverse_dcf.json",
                "07_comps.csv",
                "08_comps_implied.csv",
                "09_football_field.json",
                "10_run_manifest.json",
                "17_report.md",
                "18_report.html",
                "19_report.pdf",
            ],
        )

        charts = sorted(path.name for path in (directory / "charts").iterdir())
        # 没装 matplotlib 时输出层会跳过全部图表，并在清单的 warnings 里记一笔
        self.assertIn(len(charts), (0, 6))

    def test_龙卷风图五个参数按幅度降序(self):
        _, root = self.run_pipeline()
        with self.artifact(root, "04_tornado.csv").open(
            encoding="utf-8-sig"
        ) as handle:
            rows = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 5)
        names = [row["变量"] for row in rows]
        self.assertIn("WACC", names)
        self.assertIn("永续增长率", names)

        swings = [float(row["波动幅度"]) for row in rows]
        self.assertEqual(swings, sorted(swings, reverse=True))
        self.assertTrue(all(swing >= 0 for swing in swings))

    def test_双因素网格点数与配置一致(self):
        _, root = self.run_pipeline()
        points = config.load_sensitivity_params().grid_points

        with self.artifact(root, "05_sensitivity.csv").open(
            encoding="utf-8-sig"
        ) as handle:
            table = list(csv.reader(handle))

        self.assertEqual(len(table), points + 1, "表头 + 每轴点数的行")
        self.assertEqual(len(table[0]), points + 1, "行标签 + 每轴点数的列")

    def test_反向求解的每个结果都命中现价(self):
        """反解出的隐含参数代回模型，算出的每股价值必须等于目标价。"""
        _, root = self.run_pipeline()
        payload = json.loads(
            self.artifact(root, "06_reverse_dcf.json").read_text("utf-8")
        )
        price = config.load_market_snapshot()[0].price

        results = payload["results"]
        self.assertEqual(len(results), 4)
        for item in results:
            self.assertAlmostEqual(item["target_per_share"], price)
            self.assertAlmostEqual(
                item["achieved_per_share"], item["target_per_share"], places=6
            )
            self.assertIn(item["direction"], ("higher", "lower", "same"))

    def test_可比公司倍数算出来了(self):
        """四家公司的 PE 都不为空，说明财务数据确实填进去了。"""
        _, root = self.run_pipeline()
        with self.artifact(root, "07_comps.csv").open(
            encoding="utf-8-sig"
        ) as handle:
            rows = list(csv.reader(handle))

        pe_index = rows[0].index("PE")
        target_row = next(row for row in rows if row and row[0] == "目标")
        peer_rows = [row for row in rows if row and row[0] == "可比"]

        self.assertEqual(len(peer_rows), 3)
        for row in [target_row, *peer_rows]:
            self.assertNotEqual(row[pe_index], "")
            self.assertGreater(float(row[pe_index]), 0)

    def test_足球场图包含_DCF_与三个倍数区间(self):
        _, root = self.run_pipeline()
        payload = json.loads(
            self.artifact(root, "09_football_field.json").read_text("utf-8")
        )
        bands = payload["bands"]

        self.assertEqual(bands[0]["name"], "DCF")
        names = [band["name"] for band in bands]
        for expected in ("PE", "PB", "EV/EBITDA"):
            self.assertIn(expected, names)

        for band in bands:
            self.assertLessEqual(band["low"], band["central"])
            self.assertLessEqual(band["central"], band["high"])

    def test_足球场图里_DCF_区间来自敏感性网格(self):
        """DCF 那条横条不是单点，而是网格里的最小值和最大值。"""
        _, root = self.run_pipeline()
        payload = json.loads(
            self.artifact(root, "09_football_field.json").read_text("utf-8")
        )
        dcf_band = next(band for band in payload["bands"] if band["name"] == "DCF")

        with self.artifact(root, "05_sensitivity.csv").open(
            encoding="utf-8-sig"
        ) as handle:
            table = list(csv.reader(handle))
        cell_values = [float(cell) for row in table[1:] for cell in row[1:]]

        # CSV 里的数字只保留两位小数，所以这里按两位小数的精度比 ——
        # 比的是「两张产物说的是不是同一件事」，不是浮点完全相等。
        self.assertAlmostEqual(dcf_band["low"], min(cell_values), delta=0.01)
        self.assertAlmostEqual(dcf_band["high"], max(cell_values), delta=0.01)

    # ── 报告层 ──────────────────────────────────────────────────────

    def test_没配_API_key_也照样出报告(self):
        """大模型不可用是可接受的降级，不是失败。报告里必须写明这一点。"""
        _, root = self.run_pipeline()
        report = self.artifact(root, "17_report.md").read_text("utf-8")

        # 八个章节一个不少
        for heading in (
            "1. 投资结论",
            "2. 估值摘要",
            "3. 公司与预测概览",
            "4. 假设合理性审查",
            "5. 财务勾稽校验",
            "6. 估值分析",
            "7. 敏感性与区间归因",
            "8. 反向估值",
            "9. 风险与适用边界",
            "10. 附录",
        ):
            self.assertIn(heading, report)
        # 三种内容性质标注都在
        for label in ("【事实】", "【推论】", "【观点】"):
            self.assertIn(label, report)
        # 而且要说清叙述为什么没生成
        self.assertIn("未生成", report)

    def test_报告里的数字与估值产物一致(self):
        """报告里的关键数字必须来自产物，不能是大模型或模板自己编的。"""
        _, root = self.run_pipeline()
        report = self.artifact(root, "17_report.md").read_text("utf-8")
        valuation = json.loads(
            self.artifact(root, "03_valuation.json").read_text("utf-8")
        )

        per_share = f"{valuation['per_share']:,.2f}"
        self.assertIn(per_share, report)
        self.assertIn(f"{valuation['price']:,.2f}", report)

    def test_报告引用了图表且路径可解析(self):
        _, root = self.run_pipeline()
        directory = next((root / "runs").iterdir())
        report = (directory / "17_report.md").read_text("utf-8")

        markdown_images = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", report)
        self.assertTrue(markdown_images, "报告里至少要引用一张图")
        for relative in markdown_images:
            self.assertTrue((directory / relative).exists(), relative)

    def test_报告_html_是自包含的(self):
        _, root = self.run_pipeline()
        html_text = self.artifact(root, "18_report.html").read_text("utf-8")

        self.assertTrue(html_text.startswith("<!doctype html>"))
        self.assertIn("<style>", html_text)          # 样式内嵌，不依赖外部资源
        self.assertNotIn("<script", html_text)       # 报告不该带脚本
        # 徽章样式定义在，说明大模型段落的标注机制是活的
        self.assertIn(".ai-generated", html_text)

    def test_叙述信息记进清单(self):
        """正文不可复现，但「用哪个模型、哪版提示词」必须可查。"""
        _, root = self.run_pipeline()
        manifest = json.loads(
            self.artifact(root, "10_run_manifest.json").read_text("utf-8")
        )

        self.assertIn("narrative", manifest["inputs"])
        narrative = manifest["inputs"]["narrative"]
        self.assertIn("model", narrative)
        self.assertEqual(narrative["prompt_version"], "report-v2")
        self.assertIn("number_warnings", narrative)

    def test_报告里有图表与过程追溯(self):
        """报告不能只有文字表：图要嵌进去，执行过程要可追溯。

        这两条都对应通知的硬要求（成果以结构化形式呈现、完整记录执行过程），
        而且它们曾经因为「图表被静默跳过」而同时消失过，所以单独锁住。
        """
        _, root = self.run_pipeline()
        directory = next((root / "runs").iterdir())
        report = (directory / "17_report.md").read_text("utf-8")

        images = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", report)
        self.assertEqual(len(images), 6, "六张图都要出现在报告里")
        for relative in images:
            self.assertTrue((directory / relative).exists(), relative)

        self.assertIn("10.4 过程追溯", report)
        self.assertIn("load_config", report)   # 阶段名来自真实轨迹
        self.assertIn("narrative", report)

    def test_假设依据台账进报告(self):
        """通知把「假设依据」列为考察点，依据要能在报告里逐条看到。"""
        _, root = self.run_pipeline()
        report = self.artifact(root, "17_report.md").read_text("utf-8")
        evidence = config.load_evidence()

        self.assertTrue(evidence, "配置文件里应该有依据台账")
        for key in evidence:
            self.assertIn(key, report)


# ---------------------------------------------------------------------------
# 五、日志层
# ---------------------------------------------------------------------------


class TestTracing(unittest.TestCase):
    def test_轨迹每一步一行且是合法_json(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        with mock.patch.object(run, "OUTPUT_ROOT", root / "runs"), mock.patch.object(
            run,
            "RunTrace",
            trace_factory(root / "logs"),
        ):
            with redirect_stdout(io.StringIO()):
                run.main()

        trace_file = next((root / "logs").glob("*/trace.jsonl"))
        lines = [line for line in trace_file.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(lines), 11)

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
                "validate",
                "wacc",
                "valuation",
                "sensitivity",
                "reverse_dcf",
                "comps",
                "football_field",
                "narrative",
                "write_outputs",
            ],
        )

    def test_清单记了代码版本与全部产物哈希(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        with mock.patch.object(run, "OUTPUT_ROOT", root / "runs"), mock.patch.object(
            run,
            "RunTrace",
            trace_factory(root / "logs"),
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

        def boom(*_args: object, **_kwargs: object) -> None:
            raise config.ConfigError("模拟的配置错误：税率写成了 0.9")

        with mock.patch.object(run, "load_case", boom), mock.patch.object(
            run, "OUTPUT_ROOT", root / "runs"
        ), mock.patch.object(
            run,
            "RunTrace",
            trace_factory(root / "logs"),
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

        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("模拟的代码 bug")

        with mock.patch.object(run, "load_case", boom), mock.patch.object(
            run, "OUTPUT_ROOT", root / "runs"
        ), mock.patch.object(
            run,
            "RunTrace",
            trace_factory(root / "logs"),
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


# ---------------------------------------------------------------------------
# 七、产物验收：拦住「跑完了但产物不全」的运行
# ---------------------------------------------------------------------------


class TestCheckRun(unittest.TestCase):
    """验收层存在的理由：图表被跳过时，流水线不会失败，但报告会缺图。

    这件事真的发生过 —— 一次 make verify 产出的报告里一张图都没有，
    而 verify 仍然打印「全部检查通过」。所以验收必须验产物，不能只看退出码。
    """

    def make_fake_run(self, root: Path, *, charts: bool = True) -> Path:
        """造一个假运行目录，内容最小但齐全，方便按需删掉某些文件。"""
        directory = root / "20260101T000000000"
        (directory / "charts").mkdir(parents=True)
        for name in REQUIRED_DATA_FILES:
            (directory / name).write_text("{}", encoding="utf-8")
        for name in REQUIRED_REPORTS:
            (directory / name).write_text("# 报告\n", encoding="utf-8")
        # 装了 reportlab 时验收会要求 PDF，这里也补一个占位文件
        if HAS_REPORTLAB:
            (directory / "19_report.pdf").write_bytes(b"%PDF-1.4 stub")
        if charts:
            for name in REQUIRED_CHARTS:
                (directory / "charts" / name).write_bytes(b"png")
        # 报告总是写满 10 个章节；只有装图那一次才带图片引用。
        # 这样「缺图」与「章节不够」两种问题不会混在一起。
        report = (
            "".join(f"![图](charts/{name})\n" for name in REQUIRED_CHARTS)
            if charts
            else ""
        )
        report += "\n".join(f"## {i}. 章节" for i in range(10))
        (directory / "17_report.md").write_text(report, encoding="utf-8")
        return directory

    def test_complete_run_passes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = self.make_fake_run(Path(tmp.name))
        outcome = check_run(directory)
        self.assertTrue(outcome.ok, outcome.errors)

    def test_missing_charts_are_blocking(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = self.make_fake_run(Path(tmp.name), charts=False)
        outcome = check_run(directory)
        self.assertFalse(outcome.ok)
        self.assertTrue(any("缺图表" in err for err in outcome.errors))

    def test_missing_charts_can_be_explicitly_allowed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = self.make_fake_run(Path(tmp.name), charts=False)
        outcome = check_run(directory, require_charts=False)
        self.assertTrue(outcome.ok, outcome.errors)
        self.assertTrue(any("已声明可以没有" in note for note in outcome.notes))

    def test_missing_data_file_is_blocked(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = self.make_fake_run(Path(tmp.name))
        (directory / "03_valuation.json").unlink()
        outcome = check_run(directory)
        self.assertFalse(outcome.ok)
        self.assertIn("缺数据文件：03_valuation.json", outcome.errors)

    def test_report_without_images_is_blocked(self):
        """图生成了但报告没引用，等于没有 —— 这条也要拦。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = self.make_fake_run(Path(tmp.name))
        (directory / "17_report.md").write_text(
            "\n".join(f"## {i}. 章节" for i in range(10)), encoding="utf-8"
        )
        outcome = check_run(directory)
        self.assertFalse(outcome.ok)
        self.assertTrue(any("没有引用" in err for err in outcome.errors))

    def test_broken_image_reference_is_blocked(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = self.make_fake_run(Path(tmp.name))
        with (directory / "17_report.md").open("a", encoding="utf-8") as handle:
            handle.write("\n![断图](charts/99_not_there.png)\n")
        outcome = check_run(directory)
        self.assertFalse(outcome.ok)
        self.assertTrue(any("引用" in err for err in outcome.errors))

    def test_manifest_warnings_are_surfaced(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = self.make_fake_run(Path(tmp.name))
        (directory / "10_run_manifest.json").write_text(
            json.dumps({"warnings": ["没有安装 matplotlib，跳过全部图表的渲染"]}),
            encoding="utf-8",
        )
        outcome = check_run(directory)
        self.assertTrue(
            any("matplotlib" in warning for warning in outcome.warnings)
        )

    def test_no_narrative_is_reported_as_note_not_error(self):
        """没有大模型是可接受的降级，不该判不合格，但必须说出来。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = self.make_fake_run(Path(tmp.name))
        (directory / "10_run_manifest.json").write_text(
            json.dumps({"warnings": [], "inputs": {"narrative": {"ok": False, "notes": ["没有 key"]}}}),
            encoding="utf-8",
        )
        outcome = check_run(directory)
        self.assertTrue(outcome.ok, outcome.errors)
        self.assertTrue(any("数据版本" in note for note in outcome.notes))

    def test_short_report_is_blocked(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = self.make_fake_run(Path(tmp.name))
        (directory / "17_report.md").write_text("## 1. 只有一节\n", encoding="utf-8")
        outcome = check_run(directory)
        self.assertFalse(outcome.ok)
        self.assertTrue(any("章节偏少" in err for err in outcome.errors))

    def test_newest_run_picks_the_last_by_name(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name in ("20260101T000000", "20260202T000000", "20260115T000000"):
            (root / name).mkdir()
        self.assertEqual(newest_run(root).name, "20260202T000000")

    def test_empty_runs_dir_raises(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(FileNotFoundError):
            newest_run(Path(tmp.name))

    def test_real_pipeline_output_passes(self):
        """端到端跑一次，验收必须通过 —— 这是 make verify 最后一步的依据。"""
        root = self.run_pipeline_in_tmp()
        outcome = check_run(next((root / "runs").iterdir()))
        self.assertTrue(outcome.ok, outcome.errors)

    def run_pipeline_in_tmp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        run_pipeline_into(root)
        return root


if __name__ == "__main__":
    unittest.main()
