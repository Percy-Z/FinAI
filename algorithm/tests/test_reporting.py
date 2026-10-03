"""输出层的单元测试。"""

import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from algorithm.finai.comps import (
    CompanyMetrics,
    FootballField,
    FootballFieldBand,
    comps_bands,
    comps_valuation,
    dcf_band,
    football_field,
    implied_from_multiple,
)
from algorithm.finai.dcf import value_dcf
from algorithm.finai.forecast import (
    BalanceSheetState,
    ForecastDrivers,
    build_forecast,
    to_snapshots,
)
from algorithm.output.reporting import (
    DATA_FILES,
    RunArtifacts,
    RunOutputs,
    data_filename,
    export_run,
    make_run_id,
    sha256_of,
    summarize,
    write_json,
)
from algorithm.output.charts import CHART_FILES
from algorithm.output.charts import HAS_MATPLOTLIB as CHARTS_AVAILABLE
from algorithm.output.charts import chart_filename
from algorithm.output.pdf import HAS_REPORTLAB
from algorithm.finai.reverse_dcf import reverse_solve
from algorithm.finai.sensitivity import Variable, tornado, two_way_grid
from algorithm.finai.types import ForecastAssumptions, MarketData
from algorithm.finai.validate import run_checks

from algorithm.tests._helpers import assert_close  # noqa: E402


HAS_MATPLOTLIB = CHARTS_AVAILABLE


def sample_outputs() -> RunOutputs:
    assumptions = ForecastAssumptions(
        revenue_base=400.0,
        revenue_growth=(0.10,) * 5,
        ebit_margin=(0.20,) * 5,
        tax_rate=0.25,
        da_pct_revenue=0.05,
        capex_pct_revenue=0.06,
        nwc_pct_revenue=0.15,
        terminal_growth=0.025,
    )
    market = MarketData(
        price=10.0, diluted_shares=100.0, interest_bearing_debt=100.0, cash=55.0
    )
    dcf = value_dcf(assumptions, market, 0.10)

    rows = tornado(
        assumptions,
        market,
        0.10,
        [Variable.absolute("WACC", "wacc", base=0.10, delta=0.01)],
    )
    grid = two_way_grid(
        assumptions,
        market,
        0.10,
        Variable.bounds("永续增长率", "terminal_growth", 0.015, 0.035, base=0.025),
        Variable.bounds("WACC", "wacc", 0.09, 0.11, base=0.10),
        points=3,
    )
    reverse = [
        reverse_solve(
            assumptions,
            market,
            0.10,
            Variable.bounds("永续增长率", "terminal_growth", -0.10, 0.05, base=0.025),
        )
    ]

    target = CompanyMetrics(
        code="T",
        name="目标公司",
        price=10.0,
        diluted_shares=100.0,
        net_profit_attributable=62.0,
        book_equity_attributable=220.0,
        revenue=440.0,
        ebit=88.0,
        ebitda=110.0,
        interest_bearing_debt=100.0,
        cash=55.0,
    )
    peers = [
        CompanyMetrics("A", "甲", 21.0, 10.0, net_profit_attributable=15.0, ebitda=25.0),
        CompanyMetrics("B", "乙", 15.0, 20.0, net_profit_attributable=20.0, ebitda=35.0),
        CompanyMetrics("C", "丙", 30.0, 8.0, net_profit_attributable=18.0, ebitda=30.0),
    ]
    comps = comps_valuation(target, peers, multiples=("pe", "ev_ebitda"))

    field = football_field(
        [dcf_band(dcf, low=8.0, high=13.0)] + comps_bands(comps),
        price=10.0,
        check_pairs=(("DCF", "PE"),),
    )

    opening = BalanceSheetState(
        cash=55.0,
        accounts_receivable=60.0,
        inventory=40.0,
        fixed_assets=200.0,
        other_non_current_assets=30.0,
        accounts_payable=40.0,
        interest_bearing_debt=100.0,
        other_non_current_liabilities=25.0,
        share_capital=100.0,
        retained_earnings=120.0,
    )
    drivers = ForecastDrivers(
        revenue_base=400.0,
        base_year=2025,
        years=3,
        revenue_growth=0.10,
        gross_margin=0.40,
        opex_pct_revenue=0.15,
        da_pct_revenue=0.05,
        capex_pct_revenue=0.06,
        ar_pct_revenue=0.15,
        inventory_pct_revenue=0.10,
        ap_pct_revenue=0.10,
        interest_rate_on_debt=0.05,
        tax_rate=0.25,
        dividend_payout_ratio=0.30,
    )
    forecast = build_forecast(opening, drivers)
    reports = [run_checks(snapshot) for snapshot in to_snapshots(forecast)]

    return RunOutputs(
        dcf=dcf,
        football_field=field,
        comps=comps,
        tornado=rows,
        grid=grid,
        reverse=reverse,
        validation=reports,
        forecast=forecast,
    )


class TestRunId(unittest.TestCase):
    def test_default_format(self):
        moment = datetime(2026, 9, 27, 16, 58, 25)
        self.assertEqual(make_run_id(moment), "20260927T165825")

    def test_prefix(self):
        moment = datetime(2026, 9, 27, 16, 58, 25)
        self.assertEqual(make_run_id(moment, prefix="demo-"), "demo-20260927T165825")


class TestHelpers(unittest.TestCase):
    def test_write_json_creates_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(Path(tmp) / "a" / "b" / "x.json", {"键": "值"})
            self.assertTrue(path.exists())
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["键"], "值")

    def test_write_json_keeps_chinese_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(Path(tmp) / "x.json", {"名称": "目标公司"})
            raw = path.read_text(encoding="utf-8")
        self.assertIn("目标公司", raw)
        self.assertNotIn("\\u", raw)

    def test_sha256_matches_hashlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.bin"
            path.write_bytes(b"hello")
            expected = hashlib.sha256(b"hello").hexdigest()
            self.assertEqual(sha256_of(path), expected)


class TestRunArtifacts(unittest.TestCase):
    def test_path_of_returns_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = export_run(sample_outputs(), root=tmp, run_id="r1")
            self.assertTrue(artifacts.path_of("valuation").exists())

    def test_path_of_missing_raises(self):
        artifacts = RunArtifacts(run_id="r", directory=Path("/tmp"))
        with self.assertRaises(KeyError):
            artifacts.path_of("nope")


class TestExportRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.artifacts = export_run(
            sample_outputs(),
            root=self.root,
            run_id="20260101T000000",
            meta={"target": "T", "price": 10.0},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_directory_named_by_run_id(self):
        self.assertEqual(self.artifacts.directory, self.root / "20260101T000000")
        self.assertTrue(self.artifacts.directory.is_dir())

    def test_all_expected_files_written(self):
        expected = {
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
        }
        written = {path.name for path in self.artifacts.files.values()}
        self.assertTrue(expected.issubset(written), expected - written)

    def test_filenames_are_numbered_in_order(self):
        """数据文件 01-10、图表 11-16、报告 17-19，编号全局唯一且按顺序。

        Markdown（17）与 HTML（18）总是生成；PDF（19）要 reportlab、
        图表（11-16）要 matplotlib —— 缺依赖时对应编号整段缺席，
        那是缺产物，不是编号乱了。
        """
        names = sorted(path.name for path in self.artifacts.files.values())
        numbers = [name[:2] for name in names if name[:2].isdigit()]
        expected = [f"{index:02d}" for index in range(1, 11)]
        if HAS_MATPLOTLIB:
            expected += [f"{index:02d}" for index in range(11, 17)]
        expected += ["17", "18"]
        if HAS_REPORTLAB:
            expected += ["19"]
        self.assertEqual(numbers, expected)

    def test_charts_go_into_subdirectory(self):
        for name, path in self.artifacts.files.items():
            if name.startswith("chart_"):
                self.assertEqual(path.parent.name, "charts")

    def test_manifest_lists_every_output_with_hash(self):
        manifest = self.artifacts.manifest
        listed = {entry["name"] for entry in manifest["outputs"]}
        # 清单不列自己，因为它的哈希没法写进它自己
        self.assertEqual(listed, set(self.artifacts.files) - {"run_manifest"})
        for entry in manifest["outputs"]:
            path = self.artifacts.directory / entry["file"]
            self.assertTrue(path.exists(), entry["file"])
            self.assertEqual(entry["sha256"], sha256_of(path))
            self.assertEqual(entry["bytes"], path.stat().st_size)

    def test_manifest_records_meta_and_python(self):
        manifest = self.artifacts.manifest
        self.assertEqual(manifest["run_id"], "20260101T000000")
        self.assertEqual(manifest["inputs"]["target"], "T")
        self.assertIn("python", manifest)

    def test_manifest_is_json_serializable(self):
        raw = self.artifacts.path_of("run_manifest").read_text(encoding="utf-8")
        payload = json.loads(raw)
        self.assertEqual(payload["run_id"], "20260101T000000")

    def test_existing_directory_raises_without_overwrite(self):
        with self.assertRaises(FileExistsError):
            export_run(sample_outputs(), root=self.root, run_id="20260101T000000")

    def test_overwrite_allows_rerun(self):
        artifacts = export_run(
            sample_outputs(),
            root=self.root,
            run_id="20260101T000000",
            overwrite=True,
        )
        self.assertTrue(artifacts.path_of("valuation").exists())

    def test_only_present_results_produce_files(self):
        artifacts = export_run(
            RunOutputs(validation=list(sample_outputs().validation)),
            root=self.root,
            run_id="only-validation",
        )
        names = {path.name for path in artifacts.files.values()}
        self.assertIn("02_validation_report.json", names)
        self.assertNotIn("03_valuation.json", names)
        self.assertNotIn("01_projections.json", names)

    def test_empty_run_still_writes_manifest(self):
        artifacts = export_run(RunOutputs(), root=self.root, run_id="empty")
        # 什么数据都没有时，报告仍然会生成 —— 它只有章节骨架和破折号，
        # 正文里会写明哪些部分没有数据。这比「什么都不产出」更有用：
        # 至少能看出流水线跑通了，且缺的是什么。
        expected = {"run_manifest", "report", "report_html"}
        if HAS_REPORTLAB:
            expected.add("report_pdf")
        self.assertEqual(set(artifacts.files), expected)

    def test_summarize_mentions_run_id_and_files(self):
        text = summarize(self.artifacts)
        self.assertIn("20260101T000000", text)
        self.assertIn("03_valuation.json", text)

    def test_validation_report_holds_all_years(self):
        payload = json.loads(
            self.artifacts.path_of("validation_report").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["count"], 3)
        self.assertEqual(len(payload["reports"]), 3)

    def test_projections_report_balance_gap(self):
        payload = json.loads(
            self.artifacts.path_of("projections").read_text(encoding="utf-8")
        )
        self.assertLess(payload["worst_balance_gap"], 1e-9)
        self.assertEqual(len(payload["years"]), 3)


class TestSerialization(unittest.TestCase):
    """新加的几个 to_dict 必须能序列化，否则输出层写文件会炸。"""

    def test_football_field_to_dict(self):
        field = FootballField(
            bands=[
                FootballFieldBand("DCF", 8.0, 10.0, 12.0),
                FootballFieldBand("PE", 9.0, 9.0, 9.0),
            ],
            price=10.0,
            warnings=["示例警告"],
        )
        payload = field.to_dict()
        self.assertEqual(len(payload["bands"]), 2)
        assert_close(self, payload["bands"][0]["width"], 4.0)
        assert_close(self, payload["overall_low"], 8.0)
        assert_close(self, payload["overall_high"], 12.0)
        self.assertEqual(payload["warnings"], ["示例警告"])
        json.dumps(payload, ensure_ascii=False)

    def test_band_to_dict_includes_width(self):
        payload = FootballFieldBand("X", 1.0, 2.0, 3.5).to_dict()
        assert_close(self, payload["width"], 2.5)

    def test_reverse_result_to_dict(self):
        outputs = sample_outputs()
        payload = outputs.reverse[0].to_dict()
        for key in (
            "name",
            "field",
            "implied_value",
            "direction",
            "residual",
            "description",
        ):
            self.assertIn(key, payload)
        json.dumps(payload, ensure_ascii=False)

    def test_implied_valuation_payload_is_complete(self):
        target = CompanyMetrics("T", "目标", 10.0, 100.0, net_profit_attributable=62.0)
        item = implied_from_multiple(target, "pe", 12.0)
        assert_close(self, item.implied_per_share, 7.44, places=6)


class TestRunFileNaming(unittest.TestCase):
    def test_every_data_file_has_a_number(self):
        for name in DATA_FILES:
            filename = data_filename(name)
            self.assertTrue(filename[:2].isdigit(), filename)
            self.assertEqual(filename[2], "_", filename)

    def test_numbers_are_unique_and_ordered(self):
        numbers = [number for number, _ in DATA_FILES.values()]
        self.assertEqual(len(numbers), len(set(numbers)))
        # 01-10 是分析数据，17-19 是报告（md / html / pdf）。
        # 11-16 留给图表，它们不在 DATA_FILES 里，见 CHART_FILES。
        self.assertEqual(
            sorted(numbers),
            [f"{index:02d}" for index in range(1, 11)] + ["17", "18", "19"],
        )

    def test_unknown_name_raises(self):
        with self.assertRaises(KeyError):
            data_filename("nope")

    def test_chart_filenames_continue_the_sequence(self):
        numbers = [number for number, _ in CHART_FILES.values()]
        self.assertEqual(sorted(numbers), [f"{index:02d}" for index in range(11, 17)])

    def test_chart_filename_lookup(self):
        self.assertEqual(chart_filename("tornado"), "12_tornado.png")
        with self.assertRaises(KeyError):
            chart_filename("nope")


class TestChartsWiredIntoRun(unittest.TestCase):
    @unittest.skipUnless(HAS_MATPLOTLIB, "没有安装 matplotlib")
    def test_export_run_writes_all_charts(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = export_run(sample_outputs(), root=tmp, run_id="with-charts")
            chart_keys = {
                name for name in artifacts.files if name.startswith("chart_")
            }
            self.assertEqual(
                chart_keys,
                {
                    "chart_football_field",
                    "chart_tornado",
                    "chart_sensitivity",
                    "chart_peer_multiples",
                    "chart_forecast_trend",
                    "chart_validation_summary",
                },
            )
            for key in chart_keys:
                path = artifacts.path_of(key)
                self.assertTrue(path.exists(), key)
                self.assertGreater(path.stat().st_size, 5000, key)

    def test_charts_can_be_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = export_run(
                sample_outputs(), root=tmp, run_id="no-charts", render_charts=False
            )
            self.assertFalse(
                any(name.startswith("chart_") for name in artifacts.files)
            )
            self.assertIn("football_field", artifacts.files)


if __name__ == "__main__":
    unittest.main()
