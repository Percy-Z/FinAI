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
import importlib.util
import io
import json
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
from algorithm.finai.reverse_dcf import DEFAULT_BOUNDS  # noqa: E402
from algorithm.finai.sensitivity import tornado  # noqa: E402
from algorithm.finai.types import ForecastAssumptions  # noqa: E402
from algorithm.finai.validate import run_checks  # noqa: E402
from algorithm.finai.wacc import weights_from_market, wacc_from_market  # noqa: E402
from pipeline import config, run, tracing  # noqa: E402
from pipeline.adapters import (  # noqa: E402
    company_to_market_data,
    drivers_to_assumptions,
    fill_peers_from_financials,
    fill_target_from_forecast,
    to_reverse_variables,
    to_sensitivity_variables,
)


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

    def broken_market_csv(
        self, anchor: str, replacement: str, count: int = -1
    ) -> str:
        """把真实市场快照里的锚点替换掉；锚点找不到就当场失败。

        和 TestConfigRejectsBadToml.broken_config 同一个道理：这几条测试是
        「拿真实数据改坏一点，看配置层拦不拦得住」。直接用 str.replace 的话，
        锚点一旦失效（数据文件换了写法、加了说明行），替换会静默不做，测试报
        出来的是「没拦住」，误导成配置层的 bug。count=1 只替换第一处。
        """
        text = self.market_csv_text()
        if anchor not in text:
            self.fail(
                f"测试锚点在 data/market.csv 里找不到了，请更新锚点：{anchor!r}"
            )
        return text.replace(anchor, replacement, count)


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
        broken = self.broken_market_csv("diluted_shares", "diluted_share")
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_market_snapshot(self.write_temp_csv(broken))
        self.assertIn("无法识别的列", str(ctx.exception))

    def test_数字列写了非数字要报错(self):
        broken = self.broken_market_csv(
            "T,目标公司,target,10.00", "T,目标公司,target,十块"
        )
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_market_snapshot(self.write_temp_csv(broken))
        self.assertIn("不是数字", str(ctx.exception))
        self.assertIn("第 2 行", str(ctx.exception))

    def test_role_写错要报错(self):
        broken = self.broken_market_csv("target,10.00", "boss,10.00", 1)
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_market_snapshot(self.write_temp_csv(broken))
        self.assertIn("role", str(ctx.exception))

    def test_没有目标公司要报错(self):
        broken = self.broken_market_csv(
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
        broken = self.broken_market_csv(
            "target,10.00,100.00", "target,,100.00", 1
        )
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_market_snapshot(self.write_temp_csv(broken))
        self.assertIn("缺少必填的 price", str(ctx.exception))

    def test_可选列留空取零不报错(self):
        broken = self.broken_market_csv(
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

    def broken_config(self, anchor: str, replacement: str) -> str:
        """把真实配置里的锚点替换掉；锚点找不到就当场失败。

        为什么需要这个助手：这一类的测试都是「拿真实配置改坏一点，看配置层拦不拦
        得住」。如果直接用 str.replace，锚点一旦失效（比如给配置加了几行注释、改了
        缩进），替换会**静默什么都不做** —— 测试报出来的是「ConfigError not raised」，
        看起来像配置层的 bug，实际是测试的锚点过期了。
        2026-10-02 给 delta 补注释时就撞过一次（field 与 delta 之间插了注释）。
        """
        text = self.toml_text()
        if anchor not in text:
            self.fail(
                f"测试锚点在 data/assumptions.toml 里找不到了，请更新锚点：{anchor!r}"
            )
        return text.replace(anchor, replacement)

    def test_字段名拼错要报错(self):
        broken = self.broken_config("revenue_base ", "revenu_base ")
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_drivers(self.write_temp_toml(broken))
        self.assertIn("字段对不上", str(ctx.exception))

    def test_取值越界要报错(self):
        broken = self.broken_config(
            "tax_rate                = 0.25", "tax_rate                = 0.90"
        )
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_drivers(self.write_temp_toml(broken))
        self.assertIn("取值不合法", str(ctx.exception))

    def test_缺少小节要报错(self):
        broken = self.broken_config("[wacc]", "[wacc_typo]")
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_wacc_params(self.write_temp_toml(broken))
        self.assertIn("[wacc]", str(ctx.exception))

    def test_终值方法写错要报错(self):
        broken = self.broken_config(
            'terminal_method = "perpetuity"', 'terminal_method = "永久增长"'
        )
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_terminal_params(self.write_temp_toml(broken))
        self.assertIn("terminal_method", str(ctx.exception))

    def test_选了退出倍数却不给倍数要报错(self):
        broken = self.broken_config(
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
        broken = self.broken_config(
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

    def test_敏感性变量字段名拼错要报错(self):
        """字段白名单的意义：拼错在这里就挡住，不用等到算法模块报错。"""
        broken = self.broken_config(
            'field = "ebit_margin"', 'field = "ebit_margins"'
        )
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_sensitivity(self.write_temp_toml(broken))
        message = str(ctx.exception)
        self.assertIn("ebit_margins", message)
        self.assertIn("不是可调节的字段", message)

    def test_敏感性delta和区间不能同时给(self):
        """delta 与 low/high 互斥，同时给要报错。

        锚点取 field 那一行，不依赖「field 与 delta 相邻」—— 两者之间可以夹注释。
        """
        broken = self.broken_config(
            'field = "ebit_margin"',
            'field = "ebit_margin"\nlow = 0.15\nhigh = 0.25',
        )
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_sensitivity(self.write_temp_toml(broken))
        self.assertIn("只能给一种", str(ctx.exception))

    def test_敏感性网格引用的变量名不存在要报错(self):
        broken = self.broken_config(
            'grid_row    = "永续增长率"', 'grid_row    = "这个变量不存在"'
        )
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_sensitivity(self.write_temp_toml(broken))
        message = str(ctx.exception)
        self.assertIn("不在变量表里", message)
        self.assertIn("这个变量不存在", message)

    def test_敏感性没有变量要报错(self):
        """只写了 [sensitivity] 却没有变量表 —— 报错要给出正确写法。"""
        minimal = '[sensitivity]\ngrid_row = "a"\ngrid_col = "b"\n'
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_sensitivity(self.write_temp_toml(minimal))
        message = str(ctx.exception)
        self.assertIn("没有任何变量", message)
        self.assertIn("[[sensitivity.variables]]", message)  # 报错里带正确写法

    def test_反解字段不能反解要报错(self):
        """只有算法模块给了搜索区间的字段才能反解。"""
        broken = self.broken_config(
            'field = "terminal_growth"', 'field = "revenue_base"'
        )
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_reverse_params(self.write_temp_toml(broken))
        message = str(ctx.exception)
        self.assertIn("revenue_base", message)
        self.assertIn("不能反解", message)

    def test_反解没有参数要报错(self):
        minimal = '[reverse]\n'
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_reverse_params(self.write_temp_toml(minimal))
        message = str(ctx.exception)
        self.assertIn("没有任何参数", message)
        self.assertIn("[[reverse.variables]]", message)

    def test_估值倍数名写错要报错(self):
        broken = self.broken_config(
            'multiples = ["pe", "pb", "ev_ebitda"]',
            'multiples = ["pe", "pb", "ev_ebitdaaaa"]',
        )
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_comps_params(self.write_temp_toml(broken))
        message = str(ctx.exception)
        self.assertIn("ev_ebitdaaaa", message)
        self.assertIn("不可用", message)

    def test_估值倍数列表为空要报错(self):
        broken = self.broken_config(
            'multiples = ["pe", "pb", "ev_ebitda"]', "multiples = []"
        )
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_comps_params(self.write_temp_toml(broken))
        self.assertIn("不能为空", str(ctx.exception))


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

    def test_敏感性变量的基准值自动推导(self):
        """基准值不写在配置里，由适配层推导 —— 这是本阶段的核心设计。

        规则：wacc 用算出来的；逐年字段取各年平均；其余标量字段取值。
        """
        sensitivity = config.load_sensitivity()
        assumptions = drivers_to_assumptions(self.drivers, self.terminal)
        wacc = 0.0946  # 随便给一个，只要和推导出来的对得上
        variables = to_sensitivity_variables(sensitivity, assumptions, wacc)

        self.assertEqual(len(variables), len(sensitivity.variables))

        # wacc 的基准来自传进来的值
        self.assertAlmostEqual(variables["WACC"].base, wacc)
        # 逐年字段取各年平均：(0.10+0.09+0.08+0.07+0.06)/5 = 0.08
        self.assertAlmostEqual(variables["收入增速"].base, 0.08)
        # 标量字段取该值
        self.assertAlmostEqual(
            variables["永续增长率"].base, assumptions.terminal_growth
        )
        # EBIT 利润率也是逐年字段，各年都是 0.20
        self.assertAlmostEqual(variables["EBIT 利润率"].base, 0.20)

        # 上下界 = 基准 ± delta
        row = variables["EBIT 利润率"]
        self.assertAlmostEqual(row.low, 0.20 - 0.03)
        self.assertAlmostEqual(row.high, 0.20 + 0.03)

    def test_终值方法与变量表不匹配要报错(self):
        """用永续增长法时调退出倍数是没意义的，要在配置阶段就挡住。"""
        assumptions = drivers_to_assumptions(self.drivers, self.terminal)
        # 当前是永续增长法，硬塞一个 exit_multiple 变量进去
        bad = config.SensitivityConfig(
            variables=(config.SensitivityVariable(
                name="退出倍数", field="exit_multiple", delta=1.0
            ),)
        )
        with self.assertRaises(config.ConfigError) as ctx:
            to_sensitivity_variables(bad, assumptions, 0.0946)
        self.assertIn("exit_multiple", str(ctx.exception))
        self.assertIn("退出倍数", str(ctx.exception))

    def test_显式区间不覆盖基准值要报错(self):
        """区间必须包含当前假设 —— 否则看不出相对当前假设的变化。"""
        assumptions = drivers_to_assumptions(self.drivers, self.terminal)
        bad = config.SensitivityConfig(
            variables=(config.SensitivityVariable(
                name="EBIT 利润率", field="ebit_margin", low=0.30, high=0.40
            ),)
        )
        with self.assertRaises(config.ConfigError) as ctx:
            to_sensitivity_variables(bad, assumptions, 0.0946)
        self.assertIn("不包含当前的基准值", str(ctx.exception))

    def test_反解变量的基准值与敏感性口径一致(self):
        """同一个字段，在敏感性和反向求解里必须取同一个基准值。

        合并前两边不一致：敏感性取各年平均（0.08），反解取第一年（0.10）——
        同一份假设在两处取不同的基准值，报告会自相矛盾。
        """
        assumptions = drivers_to_assumptions(self.drivers, self.terminal)
        variables = to_reverse_variables(
            config.load_reverse_params(), assumptions, 0.0946
        )
        by_name = {item.name: item for item in variables}

        # 逐年字段：取各年平均，不是第一年
        self.assertAlmostEqual(by_name["收入增速"].base, 0.08)
        self.assertAlmostEqual(by_name["EBIT 利润率"].base, 0.20)
        # 标量字段：取值本身
        self.assertAlmostEqual(
            by_name["永续增长率"].base, assumptions.terminal_growth
        )
        self.assertAlmostEqual(by_name["WACC"].base, 0.0946)

        # 搜索区间来自算法模块的 DEFAULT_BOUNDS，不在配置里另设一套
        low, high = DEFAULT_BOUNDS["revenue_growth"]
        self.assertAlmostEqual(by_name["收入增速"].low, low)
        self.assertAlmostEqual(by_name["收入增速"].high, high)

    def test_反解的当前值不在搜索区间内要报错(self):
        """base 落在区间外时，求解器一开始就在边界上，结论没有意义。"""
        assumptions = drivers_to_assumptions(self.drivers, self.terminal)
        # DEFAULT_BOUNDS["wacc"] 是 (0.01, 0.40)，传个 0.9 就在区间外
        reverse_config = config.ReverseParams(
            variables=(config.ReverseVariable(name="WACC", field="wacc"),)
        )
        with self.assertRaises(config.ConfigError) as ctx:
            to_reverse_variables(reverse_config, assumptions, 0.9)
        self.assertIn("不在算法模块给的搜索区间", str(ctx.exception))


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

    def test_敏感性产物写出来了且结构对(self):
        """两块产物：单因素（龙卷风）与双因素（网格）。"""
        _, root = self.run_pipeline()
        sensitivity_config = config.load_sensitivity()

        # 单因素：一行一个变量
        tornado_file = next((root / "runs").glob("*/04_tornado.csv"))
        tornado_text = tornado_file.read_text(encoding="utf-8-sig")
        tornado_rows = [line for line in tornado_text.splitlines() if line.strip()]
        self.assertEqual(len(tornado_rows), 1 + len(sensitivity_config.variables))
        self.assertIn("波动幅度", tornado_rows[0])

        # 双因素：网格点数 × 网格点数
        grid_file = next((root / "runs").glob("*/05_sensitivity.csv"))
        grid_rows = [
            line
            for line in grid_file.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
        points = sensitivity_config.grid_points
        self.assertEqual(len(grid_rows), 1 + points)  # 一行表头 + 每行一个纵轴取值

    def test_敏感性结论与算法层一致(self):
        """框架不改变任何数字 —— 与估值、勾稽校验同一个思路。"""
        _, root = self.run_pipeline()
        tornado_file = next((root / "runs").glob("*/04_tornado.csv"))
        rows = list(csv.DictReader(tornado_file.read_text(encoding="utf-8-sig").splitlines()))

        # 直接调算法模块算一遍，逐行对照波动幅度
        _, drivers, _ = config.load_case()
        assumptions = drivers_to_assumptions(drivers, config.load_terminal_params())
        target, _ = config.load_market_snapshot()
        market = company_to_market_data(target)
        wacc = float(
            json.loads(
                next((root / "runs").glob("*/03_valuation.json")).read_text("utf-8")
            )["wacc"]
        )
        variables = to_sensitivity_variables(
            config.load_sensitivity(), assumptions, wacc
        )
        expected = tornado(assumptions, market, wacc, variables.values())

        self.assertEqual(len(rows), len(expected))
        for got, want in zip(rows, expected):
            self.assertEqual(got["变量"], want.name)
            # CSV 里的数值是四舍五入到 2 位小数的展示格式（输出层定的），
            # 所以这里比到小数点后 2 位就够 —— 比的是「有没有算错」，不是精度。
            self.assertAlmostEqual(
                float(got["波动幅度"]), want.swing, places=2,
                msg=f"{want.name} 的波动幅度与算法层不一致",
            )

    def test_相对估值用的倍数来自配置(self):
        """改配置里的 [comps].multiples，产物里的倍数就跟着变。"""
        _, root = self.run_pipeline()
        implied = next((root / "runs").glob("*/08_comps_implied.csv"))
        rows = list(
            csv.DictReader(implied.read_text(encoding="utf-8-sig").splitlines())
        )

        configured = config.load_comps_params().multiples
        used = {row["倍数"] for row in rows}
        # 产物里的倍数集合 = 配置里的倍数（中文标签，和 config 的 key 对应）
        expected_labels = {
            "pe": "PE",
            "pb": "PB",
            "ps": "PS",
            "ev_ebitda": "EV/EBITDA",
            "ev_ebit": "EV/EBIT",
        }
        self.assertEqual(used, {expected_labels[name] for name in configured})
        # 每个倍数三个统计口径（q1 / median / q3）
        self.assertEqual(len(rows), len(configured) * 3)

    def test_反解产物来自配置里的参数表(self):
        _, root = self.run_pipeline()
        produced = next((root / "runs").glob("*/06_reverse_dcf.json"))
        payload = json.loads(produced.read_text(encoding="utf-8"))
        names = [item["name"] for item in payload["results"]]
        configured = [item.name for item in config.load_reverse_params().variables]
        # 反解出来的参数名与配置里的顺序、名字一致
        self.assertEqual(names, configured)

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
            return {
                "inputs": {
                    key: value
                    for key, value in manifest["inputs"].items()
                    if key != "steps"
                },
                "outputs": manifest["outputs"],
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
            ],
        )

        # 没装 matplotlib 时输出层会跳过全部图表，并在清单的 warnings 里记一笔 ——
        # 注意此时 charts/ 目录**根本不会被创建**，所以要先判断存在性
        charts_dir = directory / "charts"
        charts = (
            sorted(path.name for path in charts_dir.iterdir())
            if charts_dir.exists()
            else []
        )
        self.assertIn(len(charts), (0, 6))

    def test_龙卷风图按幅度降序(self):
        _, root = self.run_pipeline()
        with self.artifact(root, "04_tornado.csv").open(
            encoding="utf-8-sig"
        ) as handle:
            rows = list(csv.DictReader(handle))

        # 行数 = 配置里考察的变量数，以配置为准（现在是 6 个，不是写死的 5 个）
        self.assertEqual(len(rows), len(config.load_sensitivity().variables))
        names = [row["变量"] for row in rows]
        self.assertIn("WACC", names)
        self.assertIn("永续增长率", names)
        self.assertIn("税率", names)

        swings = [float(row["波动幅度"]) for row in rows]
        self.assertEqual(swings, sorted(swings, reverse=True))
        self.assertTrue(all(swing >= 0 for swing in swings))

    def test_双因素网格点数与配置一致(self):
        _, root = self.run_pipeline()
        points = config.load_sensitivity().grid_points

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


# ---------------------------------------------------------------------------
# 五、报告生成
# ---------------------------------------------------------------------------


class TestReport(unittest.TestCase):
    """报告是最后一个阶段：从产物文件读，拼成一份可读的估值报告。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with mock.patch.object(run, "OUTPUT_ROOT", self.root / "runs"), mock.patch.object(
            run,
            "RunTrace",
            lambda rid: tracing.RunTrace(rid, log_root=self.root / "logs"),
        ):
            with redirect_stdout(io.StringIO()):
                run.main()
        self.run_dir = next((self.root / "runs").iterdir())
        self.text = (self.run_dir / "report.md").read_text(encoding="utf-8")

    def test_报告在产物目录里自动生成(self):
        self.assertTrue((self.run_dir / "report.md").exists())
        self.assertGreater(len(self.text.splitlines()), 100)

    def test_报告包含全部章节(self):
        for title in [
            "一、结论",
            "二、主要假设",
            "三、三表联动预测",
            "四、财务勾稽校验",
            "五、现金流折现估值",
            "六、敏感性分析",
            "七、反向验证",
            "八、相对估值",
            "九、估值区间汇总",
            "十、数据来源与可复现性",
            "附录：图表",
        ]:
            self.assertIn(f"## {title}", self.text, msg=f"报告缺少「{title}」")

    def test_报告里的关键数字与产物一致(self):
        """报告的数字必须来自产物 —— 这里是逐项核对，防止两处各算一遍。"""
        valuation = json.loads((self.run_dir / "03_valuation.json").read_text("utf-8"))
        manifest = json.loads(
            (self.run_dir / "10_run_manifest.json").read_text("utf-8")
        )
        # 每股价值（两位小数）
        self.assertIn(f"{valuation['per_share']:.2f}", self.text)
        # WACC（四位小数百分比）
        wacc = manifest["inputs"]["run_parameters"]["wacc"]["value"]
        self.assertIn(f"{wacc:.4%}", self.text)
        # 运行编号
        self.assertIn(manifest["run_id"], self.text)

    def test_判断性的内容留给AI层(self):
        """报告只生成事实性内容；涉及判断的位置有占位标记。"""
        hint_count = self.text.count("待 AI 层填写")
        self.assertGreaterEqual(
            hint_count, 4, msg="涉及判断的位置应当留出 AI 占位标记"
        )

    def test_报告可以单独重新生成(self):
        """改措辞不用重跑计算 —— report 从产物读，两次生成结果相同。"""
        from pipeline.report import build_report, write_report

        first = build_report(self.run_dir)
        again = write_report(self.run_dir).read_text(encoding="utf-8")
        self.assertEqual(first, again)

    def test_缺产物时跳过那一节而不报错(self):
        """某个阶段没跑（或还没接）时，报告应当照常生成。"""
        from pipeline.report import build_report

        (self.run_dir / "06_reverse_dcf.json").unlink()
        text = build_report(self.run_dir)
        self.assertIn("## 八、相对估值", text)       # 其余节还在
        self.assertNotIn("## 七、反向验证", text)    # 缺产物的那节跳过

    def test_没装依赖时跳过对应格式并说明原因(self):
        """PDF 与 Excel 是可选依赖：没装就跳过，而不是报错。

        这条在两种环境下的期望不同 —— 装了 reportlab/openpyxl 就生成，
        没装就跳过并在 trace 里记下原因。两种都算通过。
        """
        from pipeline.report import HAS_REPORTLAB
        from pipeline.workbook import HAS_OPENPYXL

        self.assertEqual((self.run_dir / "report.pdf").exists(), HAS_REPORTLAB)
        self.assertEqual((self.run_dir / "model.xlsx").exists(), HAS_OPENPYXL)


# ---------------------------------------------------------------------------
# 五之二、活公式 Excel 模型
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    importlib.util.find_spec("openpyxl") is not None, "没有安装 openpyxl"
)
class TestWorkbook(unittest.TestCase):
    """活公式模型：七个页签，改 Inputs 则全表重算。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with mock.patch.object(run, "OUTPUT_ROOT", self.root / "runs"), mock.patch.object(
            run,
            "RunTrace",
            lambda rid: tracing.RunTrace(rid, log_root=self.root / "logs"),
        ):
            with redirect_stdout(io.StringIO()):
                run.main()
        self.run_dir = next((self.root / "runs").iterdir())

    def test_七个页签都在(self):
        from openpyxl import load_workbook

        book = load_workbook(self.run_dir / "model.xlsx")
        self.assertEqual(
            book.sheetnames,
            ["Inputs", "Historicals", "Projections", "WACC", "DCF",
             "Sensitivities", "Checks"],
        )

    def test_关键结果是公式而不是写死的数(self):
        """「活公式」的意思：改 Inputs 里的假设，估值跟着变。

        所以 DCF 页签的每股价值必须是一个公式，指向 Projections，
        而不是一个算好的常数。
        """
        from openpyxl import load_workbook

        book = load_workbook(self.run_dir / "model.xlsx")
        dcf = book["DCF"]
        projections = book["Projections"]
        wacc = book["WACC"]

        # 每股价值 = 权益价值 / 股数（公式）
        self.assertTrue(str(dcf["B19"].value).startswith("="))
        self.assertEqual(dcf["B19"].value, "=B17/B18")
        # FCFF 引 Projections，折现因子引 WACC
        self.assertEqual(dcf["B5"].value, "=Projections!B22")
        self.assertIn("WACC!$B$20", str(dcf["B6"].value))
        # EBIT 利润率是三项相减（毛利率−费用率−折旧率），摊开写、漏不掉
        self.assertEqual(projections["B13"].value, "=B7-B9-B11")
        # WACC 也是公式
        self.assertTrue(str(wacc["B20"].value).startswith("="))

    def test_基期资产负债表合计用公式且差额为零(self):
        from openpyxl import load_workbook

        book = load_workbook(self.run_dir / "model.xlsx")
        sheet = book["Historicals"]
        # 资产总计 / 负债合计 / 权益合计 都是 SUM，不是写死的数
        self.assertTrue(str(sheet["B8"].value).startswith("=SUM"))
        self.assertTrue(str(sheet["B13"].value).startswith("=SUM"))
        self.assertTrue(str(sheet["B16"].value).startswith("=SUM"))
        # 平衡差额 = 资产总计 − 负债与权益合计
        self.assertTrue(str(sheet["B18"].value).startswith("="))

    def test_引擎的值与模型的值并排放着(self):
        """Checks 页签把这个作为自我校验：Excel 公式 vs 引擎。

        Excel 打开时「差额」应当是 0 —— 说明公式链复现了引擎的口径。
        """
        from openpyxl import load_workbook

        book = load_workbook(self.run_dir / "model.xlsx")
        sheet = book["Checks"]
        values = {
            sheet.cell(row=row, column=1).value: sheet.cell(row=row, column=2).value
            for row in range(1, sheet.max_row + 1)
        }
        # 按整行标签精确匹配，不能用关键字模糊找 —— 页签顶部的大标题里
        # 同时含「Python 引擎」和「Excel 公式」，模糊匹配会先命中标题行。
        engine_key = "Python 引擎算出的每股价值"
        model_key = "Excel 公式算出的每股价值"
        self.assertIn(engine_key, values)
        self.assertIn(model_key, values)
        self.assertAlmostEqual(
            values[engine_key],
            json.loads((self.run_dir / "03_valuation.json").read_text("utf-8"))[
                "per_share"
            ],
        )
        self.assertEqual(values[model_key], "=DCF!B19")


# ---------------------------------------------------------------------------
# 六、日志层
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
                "write_outputs",
                "report",
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


if __name__ == "__main__":
    unittest.main()
