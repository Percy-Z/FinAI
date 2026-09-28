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
from pathlib import Path
from unittest import mock

# 让测试可以从任何目录运行
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from algorithm.finai.dcf import project  # noqa: E402
from algorithm.finai.forecast import build_forecast  # noqa: E402
from algorithm.finai.types import ForecastAssumptions  # noqa: E402
from algorithm.finai.wacc import weights_from_market, wacc_from_market  # noqa: E402
from pipeline import config, run, tracing  # noqa: E402


# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------


def read_real_config():
    """读仓库里那份真实配置。测的是「配置和算法层能不能对上」。"""
    company, drivers, opening = config.load_case()
    params = config.load_wacc_params()
    target, peers = config.load_market_snapshot()
    return company, drivers, opening, params, target, peers


def scalar_ratio(series, name: str) -> float:
    """把「逐年比率」取成单个数字。

    估值引擎（ForecastAssumptions）只接受单一比率，不接受逐年数组。
    所以映射之前必须先确认它是逐年相同的，否则信息会丢。
    """
    values = set(series)
    if len(values) != 1:
        raise AssertionError(
            f"{name} 逐年不同（{sorted(values)}），无法映射到估值引擎"
        )
    return series[0]


def drivers_to_assumptions(drivers) -> ForecastAssumptions:
    """ForecastDrivers → ForecastAssumptions。

    ⚠️ 这是框架里最容易写错的一段，将来会搬到 pipeline/adapters.py。
    关键在 ebit_margin 那一行：栈A 的 EBIT 是「扣过折旧摊销的」，
    所以必须减掉 da_pct_revenue。只写 毛利-费用 会高估约三成。
    """
    return ForecastAssumptions(
        revenue_base=drivers.revenue_base,
        revenue_growth=drivers.revenue_growth,
        ebit_margin=[
            gross - opex - da
            for gross, opex, da in zip(
                drivers.gross_margin,
                drivers.opex_pct_revenue,
                drivers.da_pct_revenue,
            )
        ],
        tax_rate=drivers.tax_rate,
        da_pct_revenue=scalar_ratio(drivers.da_pct_revenue, "da_pct_revenue"),
        capex_pct_revenue=scalar_ratio(
            drivers.capex_pct_revenue, "capex_pct_revenue"
        ),
        nwc_pct_revenue=(
            scalar_ratio(drivers.ar_pct_revenue, "ar_pct_revenue")
            + scalar_ratio(drivers.inventory_pct_revenue, "inventory_pct_revenue")
            - scalar_ratio(drivers.ap_pct_revenue, "ap_pct_revenue")
        ),
    )


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
        company, drivers, opening, params, target, peers = read_real_config()
        self.assertEqual(company.code, "T")
        self.assertEqual(drivers.years, 5)
        self.assertEqual(opening.total_assets, 385.0)
        self.assertGreater(params.beta, 0)
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
        assumptions = drivers_to_assumptions(self.drivers)
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
        """把「毛利 - 费用」当成 EBIT 利润率，会高估多少。"""
        correct = drivers_to_assumptions(self.drivers)
        wrong = ForecastAssumptions(
            revenue_base=self.drivers.revenue_base,
            revenue_growth=self.drivers.revenue_growth,
            ebit_margin=[
                gross - opex
                for gross, opex in zip(
                    self.drivers.gross_margin, self.drivers.opex_pct_revenue
                )
            ],
            tax_rate=self.drivers.tax_rate,
            da_pct_revenue=scalar_ratio(
                self.drivers.da_pct_revenue, "da_pct_revenue"
            ),
            capex_pct_revenue=scalar_ratio(
                self.drivers.capex_pct_revenue, "capex_pct_revenue"
            ),
            nwc_pct_revenue=(
                scalar_ratio(self.drivers.ar_pct_revenue, "ar_pct_revenue")
                + scalar_ratio(
                    self.drivers.inventory_pct_revenue, "inventory_pct_revenue"
                )
                - scalar_ratio(self.drivers.ap_pct_revenue, "ap_pct_revenue")
            ),
        )
        correct_fcff = project(correct, base_year=2025).fcff
        wrong_fcff = project(wrong, base_year=2025).fcff

        for index, (right, inflated) in enumerate(
            zip(correct_fcff, wrong_fcff), start=1
        ):
            self.assertGreater(
                inflated, right, msg=f"第 {index} 年应该被高估却没有"
            )
        # 第一条就高估了两成以上
        self.assertGreater(wrong_fcff[0] / correct_fcff[0] - 1, 0.2)


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
        self.assertEqual(len(lines), 3)  # load_config / forecast / write_projections

        steps = []
        for index, line in enumerate(lines, start=1):
            entry = json.loads(line)  # 每行都是一个独立完整的 JSON
            self.assertEqual(entry["seq"], index)
            self.assertEqual(entry["status"], "ok")
            self.assertIsNotNone(entry["duration_s"])
            steps.append(entry["step"])
        self.assertEqual(steps, ["load_config", "forecast", "write_projections"])

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
