"""叙述层与报告层的单元测试。

这里有个原则：**测试绝不真的调用大模型接口**。
所有 HTTP 交互都用假响应替掉，测试要验的是我们自己的逻辑 ——
提示词对不对、解析稳不稳、降级走不走得通、数字校验抓不抓得住。
真接口的行为（以及它会不会胡说）不是单元测试能保证的。
"""

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any, Literal
from unittest import mock

from algorithm.finai.dcf import value_dcf
from algorithm.finai.types import ForecastAssumptions, MarketData
from algorithm.output.narrative import (
    NARRATIVE_SECTIONS,
    NarrativeRequest,
    build_fact_sheet,
    build_messages,
    parse_sections,
    verify_numbers,
    write_narrative,
)
from algorithm.output.report import (
    Block,
    ReportContext,
    build_blocks,
    render_html,
    render_markdown,
)
from algorithm.output.reporting import RunOutputs


def sample_assumptions() -> ForecastAssumptions:
    return ForecastAssumptions(
        revenue_base=100.0,
        revenue_growth=(0.0,) * 5,
        ebit_margin=(0.20,) * 5,
        tax_rate=0.25,
        da_pct_revenue=0.05,
        capex_pct_revenue=0.05,
        nwc_pct_revenue=0.10,
        terminal_growth=0.0,
    )


def sample_market() -> MarketData:
    return MarketData(
        price=10.0,
        diluted_shares=10.0,
        interest_bearing_debt=40.0,
        cash=10.0,
        as_of="2026-09-30",
    )


def sample_outputs() -> RunOutputs:
    """只带 DCF 的产出。事实表要能在部分产出下也构造得出来。"""
    return RunOutputs(dcf=value_dcf(sample_assumptions(), sample_market(), 0.10))


CONTEXT = {
    "company": {"name": "示例公司", "code": "T", "source": "test"},
    "wacc": {"risk_free": 0.0235, "beta": 1.2, "value": 0.0947},
    "sensitivity": {
        "variables": [
            {
                "name": "WACC",
                "low": 0.08,
                "high": 0.10,
                "per_share_at_low": 12.87,
                "per_share_at_high": 8.05,
                "swing": 4.82,
            }
        ],
        "base_per_share": 9.30,
    },
    "notes": {"peers": ["可比公司 A 没有财务数据"], "reverse_skipped": []},
}


def fake_response(content: str) -> object:
    """构造一个假的 urlopen 返回值（支持 with 语句）。"""

    class _Response:
        def __init__(self, text: str) -> None:
            self._body = text.encode("utf-8")

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> Literal[False]:
            # 明确返回 False：不吞异常。写成 bool 会被 mypy 判为可能吞掉异常。
            return False

    return _Response(content)


def chat_envelope(content: str, prompt_tokens: int = 100) -> str:
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 50},
        },
        ensure_ascii=False,
    )


def all_sections_payload() -> str:
    """一份「九节都写全」的返回，用于让多轮逻辑在第一轮就收工。

    测试里必须给全：只给一节的话会触发补齐重试，跑三轮，
    既慢又让 token 断言变得难以理解。补齐逻辑本身另有专门的测试。
    """
    return json.dumps({name: f"{name} 的正文。" for name, _ in NARRATIVE_SECTIONS})


def request_without_key_file(**kwargs: Any) -> NarrativeRequest:
    """构造一个「只认环境变量」的请求。

    为什么必须显式关掉文件回退：仓库根目录可能真的放着 apikey.txt，
    那样「没有 key 就降级」的测试会**真的去调接口** —— 测试依赖环境里的凭据
    是绝对不能接受的（又慢、又会在别人机器上失败、还可能产生费用）。
    """
    return NarrativeRequest(api_key_file="", **kwargs)


class TestFactSheet(unittest.TestCase):
    def test_partial_outputs_still_build(self):
        facts = build_fact_sheet(sample_outputs(), context=CONTEXT)
        self.assertIn("dcf", facts)
        self.assertAlmostEqual(facts["dcf"]["per_share"], 12.0)
        self.assertAlmostEqual(facts["dcf"]["price"], 10.0)
        self.assertEqual(facts["company"]["name"], "示例公司")

    def test_empty_outputs_is_still_a_dict(self):
        facts = build_fact_sheet(RunOutputs(), context=None)
        self.assertIsInstance(facts, dict)
        self.assertIn("prompt_version", facts)

    def test_rounding_keeps_numbers_readable(self):
        facts = build_fact_sheet(sample_outputs())
        # 四舍五入到 4 位以内，避免把浮点尾巴塞进提示词
        for value in facts["dcf"].values():
            if isinstance(value, float):
                self.assertEqual(value, round(value, 4))

    def test_missing_field_is_none_not_zero(self):
        """缺数据要显示成缺数据。0 和「没有这个数」在报告里是两回事。

        这里走公开路径（渲染一份缺字段的报告），不碰私有格式化函数 ——
        测的是行为，不是实现。
        """
        context = ReportContext(
            run_id="R", created_at="2026-10-02 16:00",
            company_name="示例公司", company_code="T",
        )
        blocks = build_blocks(
            {"dcf": {"per_share": None, "price": 10.0}}, None, {}, context
        )
        markdown = render_markdown(blocks)
        self.assertIn("—", markdown, "缺的值应该显示成破折号")


class TestPrompt(unittest.TestCase):
    def setUp(self):
        self.facts = build_fact_sheet(sample_outputs(), context=CONTEXT)
        self.messages = build_messages(self.facts)

    def test_two_messages(self):
        self.assertEqual([m["role"] for m in self.messages], ["system", "user"])

    def test_system_prompt_states_the_hard_rules(self):
        system = self.messages[0]["content"]
        for keyword in ("绝不自己计算", "不确定性必须保留", "【推论】", "【观点】"):
            self.assertIn(keyword, system)

    def test_user_prompt_contains_every_section(self):
        user = self.messages[1]["content"]
        for name, _ in NARRATIVE_SECTIONS:
            self.assertIn(name, user)

    def test_user_prompt_contains_the_facts(self):
        user = self.messages[1]["content"]
        self.assertIn("事实表", user)
        self.assertIn("示例公司", user)


class TestParseSections(unittest.TestCase):
    def test_plain_json(self):
        sections, notes = parse_sections('{"risks": "风险一"}')
        self.assertEqual(sections, {"risks": "风险一"})
        self.assertTrue(any("缺少章节" in note for note in notes))

    def test_json_wrapped_in_code_fence(self):
        raw = '```json\n{"risks": "风险一"}\n```'
        sections, _ = parse_sections(raw)
        self.assertEqual(sections.get("risks"), "风险一")

    def test_json_with_surrounding_prose(self):
        raw = '好的，这是报告：\n{"risks": "风险一"}\n希望有帮助。'
        sections, _ = parse_sections(raw)
        self.assertEqual(sections.get("risks"), "风险一")

    def test_invalid_json_gives_empty_and_note(self):
        sections, notes = parse_sections("这不是 JSON")
        self.assertEqual(sections, {})
        self.assertTrue(notes)

    def test_extra_sections_are_dropped(self):
        raw = json.dumps({"risks": "风险", "moon": "月球的估值"})
        sections, notes = parse_sections(raw)
        self.assertNotIn("moon", sections)
        self.assertTrue(any("多出" in note for note in notes))

    def test_empty_values_are_ignored(self):
        sections, _ = parse_sections('{"risks": "   ", "executive_summary": "有内容"}')
        self.assertNotIn("risks", sections)
        self.assertIn("executive_summary", sections)


class TestVerifyNumbers(unittest.TestCase):
    def setUp(self):
        self.facts = {
            "dcf": {
                "per_share": 9.3,
                "enterprise_value": 975.44,
                "terminal_share_of_ev": 0.7415,
            },
            "years": [{"year": 2026, "revenue": 440.0}],
        }

    def test_number_from_facts_is_accepted(self):
        warnings = verify_numbers({"a": "每股价值 9.30 元，企业价值 975.44 元。"}, self.facts)
        self.assertEqual(warnings, [])

    def test_percent_form_is_accepted(self):
        warnings = verify_numbers({"a": "终值占比 74.15%。"}, self.facts)
        self.assertEqual(warnings, [])

    def test_rounded_percent_is_accepted(self):
        warnings = verify_numbers({"a": "终值占比约 74.2%。"}, self.facts)
        self.assertEqual(warnings, [])

    def test_year_is_accepted(self):
        self.assertEqual(verify_numbers({"a": "2026 年收入 440.00 元。"}, self.facts), [])

    def test_small_count_is_accepted(self):
        self.assertEqual(verify_numbers({"a": "三家可比公司。"}, self.facts), [])

    def test_invented_decimal_is_caught(self):
        warnings = verify_numbers({"a": "行业平均增速为 8.47%。"}, self.facts)
        self.assertEqual(len(warnings), 1)
        self.assertIn("8.47", warnings[0])

    def test_invented_large_number_is_caught(self):
        warnings = verify_numbers({"a": "对应企业价值 1234.5 元。"}, self.facts)
        self.assertTrue(warnings, "事实表里没有 1234.5，应该报出来")
        self.assertEqual(len(warnings), 1)

    def test_负号写法不同不算错(self):
        """事实表用的是数学减号（−）、正文用 ASCII 减号（-），是同一个数。

        真实案例（2026-10-13）：台账里写「同比 −10.27%」，大模型在报告里写
        「-10.27%」，数字校验把 4 处全报成「事实表之外的数字」。
        """
        facts = {"evidence": {"drivers.revenue_growth": "2025 年同比 −10.27%（年报第 87 页）"}}
        sections = {"company_overview": "公司收入同比 -10.27%，与上年一致。", "review": "下滑 −10.27%"}
        self.assertEqual(verify_numbers(sections, facts), [])

    def test_单位换算不算新数字(self):
        """事实表存「元」、正文写「亿元」，是同一个数。

        真实案例（2026-10-13）：企业价值 33,676,107,746.71 元 被写成「336.76 亿元」、
        净债务 −308,930,352.93 元 写成「−3.09 亿元」，全被报成「事实表之外的数字」——
        一份报告 6 处误报里 5 处是这个原因。演示数据只有几百元，所以一直没暴露。
        """
        facts = {
            "valuation": {
                "enterprise_value": 33676107746.71,
                "equity_value": 31124077800.00,
                "net_debt": -308930352.93,
            }
        }
        sections = {
            "valuation_analysis": "企业价值 336.76 亿元、权益价值 311.24 亿元、净债务 -3.09 亿元。"
        }
        self.assertEqual(verify_numbers(sections, facts), [])

    def test_千分位的数字算一个数(self):
        """14,698,269,693.83 是一个数，不能被拆成 14 / 698 / 269 / 693.83。

        真实案例（2026-10-14）：报告的预测表里写着 14,698,269,693.83，数字校验
        不认千分位，于是把碎片当成「事实表之外的数字」报了 5 处 —— 全是同一串
        数字的碎片。
        """
        facts = {"projections": {"years": [{"revenue": 14698269693.83}]}}
        sections = {
            "company_overview": "到 2030 年，预测收入为 14,698,269,693.83 元。"
        }
        self.assertEqual(verify_numbers(sections, facts), [])

    def test_multiple_invented_numbers_are_all_reported(self):
        warnings = verify_numbers({"a": "分别约为 3.33 和 6.66。"}, self.facts)
        self.assertEqual(len(warnings), 2)

    def test_warning_names_the_section(self):
        warnings = verify_numbers({"risks": "占比 8.47%。"}, self.facts)
        self.assertIn("risks", warnings[0])

    def test_range_dash_is_not_read_as_a_minus_sign(self):
        """区间写法（7.32-12.87、2026—2030）不能被当成负数。

        实测踩过：一份报告里因此产生 10 处误报，全是区间连接符。
        误报一多这道校验就没人看了，等于没有。
        """
        # 把区间两端的数字放进事实表，这样测的就只是「连字符有没有被当负号」
        facts = {"range": {"low": 7.32, "high": 12.87, "mid": 9.58, "mid2": 9.77}}
        cases = [
            "估值区间 7.32-12.87 元。",
            "区间为 9.58 - 9.77 元。",
            "预测期 2026—2030 年。",
            "每股价值 7.32~12.87 元。",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(verify_numbers({"a": text}, facts), [])

    def test_real_negative_numbers_are_still_checked(self):
        """真的负数还要能查出来 —— 修区间问题不能把负数一起放过。"""
        warnings = verify_numbers({"a": "隐含回报 -8.47%。"}, self.facts)
        self.assertEqual(len(warnings), 1)
        self.assertIn("8.47", warnings[0])


class TestWriteNarrative(unittest.TestCase):
    def setUp(self):
        self.facts = build_fact_sheet(sample_outputs(), context=CONTEXT)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_dir = Path(self.tmp.name)

    def test_missing_api_key_degrades(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            result = write_narrative(self.facts, request_without_key_file())
        self.assertFalse(result.ok)
        self.assertEqual(result.sections, {})
        self.assertTrue(any("DEEPSEEK_API_KEY" in note for note in result.notes))

    def test_disabled_does_not_call_anything(self):
        with mock.patch("urllib.request.urlopen") as opener:
            result = write_narrative(self.facts, request_without_key_file(enabled=False))
        opener.assert_not_called()
        self.assertFalse(result.ok)

    def test_successful_call_parses_sections(self):
        payload = all_sections_payload()
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}), mock.patch(
            "urllib.request.urlopen", return_value=fake_response(chat_envelope(payload))
        ):
            result = write_narrative(self.facts, request_without_key_file(), log_dir=self.log_dir)
        self.assertTrue(result.ok)
        self.assertEqual(result.sections["risks"], "risks 的正文。")
        self.assertEqual(len(result.sections), len(NARRATIVE_SECTIONS))
        self.assertEqual(result.rounds, 1, "九节一次写全时不应该有第二轮")
        self.assertEqual(result.usage["prompt_tokens"], 100)
        self.assertTrue(result.prompt_sha256)
        self.assertTrue(result.response_sha256)

    def test_raw_exchange_is_written_to_log_dir(self):
        payload = all_sections_payload()
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}), mock.patch(
            "urllib.request.urlopen", return_value=fake_response(chat_envelope(payload))
        ):
            result = write_narrative(self.facts, request_without_key_file(), log_dir=self.log_dir)
        raw_path = Path(result.raw_path)
        self.assertTrue(raw_path.exists())
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
        rounds = payload["rounds"]
        self.assertTrue(rounds)
        self.assertIn("request", rounds[0])
        self.assertIn("response", rounds[0])
        # 请求里必须带上模型名与提示词
        self.assertEqual(rounds[0]["request"]["model"], "deepseek-v4-pro")
        self.assertEqual(len(rounds[0]["request"]["messages"]), 2)

    def test_missing_sections_trigger_a_second_round(self):
        """模型只写出一部分时要自动补齐 —— 这修的是实测踩过的坑：
        九节一次要齐时，它写完第一节就停了（finish_reason = stop）。"""

        first = json.dumps({"investment_conclusion": "第一轮只写了这一节。"})
        second = json.dumps(
            {name: f"{name} 补写。" for name, _ in NARRATIVE_SECTIONS if name != "investment_conclusion"}
        )
        responses = [fake_response(chat_envelope(first)), fake_response(chat_envelope(second))]
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}), mock.patch(
            "urllib.request.urlopen", side_effect=responses
        ):
            result = write_narrative(
                self.facts, request_without_key_file(effort="low"), log_dir=self.log_dir
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.rounds, 2)
        self.assertEqual(len(result.sections), len(NARRATIVE_SECTIONS))
        self.assertEqual(result.sections["investment_conclusion"], "第一轮只写了这一节。")
        # 第二轮只应该索取缺的那几节
        second_request = json.loads(
            Path(result.raw_path).read_text(encoding="utf-8")
        )["rounds"][1]["request"]
        asked = second_request["messages"][1]["content"]
        self.assertIn("上一轮没有写出来", asked)

    def test_giving_up_after_max_rounds_keeps_what_it_has(self):
        """一直补不齐也要留下已经写好的部分，而不是整体判定失败。"""
        partial = json.dumps({"investment_conclusion": "只有这一节。"})
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}), mock.patch(
            "urllib.request.urlopen",
            side_effect=[fake_response(chat_envelope(partial))] * 3,
        ):
            result = write_narrative(self.facts, request_without_key_file())

        self.assertTrue(result.ok, "部分成功也要算成功，报告里缺的那几节会另作说明")
        self.assertEqual(result.rounds, 3)
        self.assertIn("investment_conclusion", result.sections)
        self.assertTrue(any("仍未写出" in note for note in result.notes))

    def test_http_error_degrades(self):
        from email.message import Message

        error = urllib.error.HTTPError(
            "https://api.deepseek.com", 401, "Unauthorized", Message(),
            io.BytesIO(b'{"error":"bad key"}'),
        )
        self.addCleanup(error.close)  # HTTPError 持有文件对象，用完要关
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-bad"}), mock.patch(
            "urllib.request.urlopen", side_effect=error
        ):
            result = write_narrative(self.facts, request_without_key_file())
        self.assertFalse(result.ok)
        self.assertTrue(any("401" in note for note in result.notes))

    def test_network_error_degrades(self):
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}), mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("网络不通"),
        ):
            result = write_narrative(self.facts, request_without_key_file())
        self.assertFalse(result.ok)
        self.assertTrue(any("网络" in note for note in result.notes))

    def test_envelope_without_choices_degrades(self):
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}), mock.patch(
            "urllib.request.urlopen", return_value=fake_response("{}")
        ):
            result = write_narrative(self.facts, request_without_key_file())
        self.assertFalse(result.ok)
        self.assertTrue(any("choices" in note for note in result.notes))

    def test_number_warnings_recorded_on_success(self):
        payload = json.dumps({"risks": "毛利率约 88.88%。"})
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-test"}), mock.patch(
            "urllib.request.urlopen", return_value=fake_response(chat_envelope(payload))
        ):
            result = write_narrative(self.facts, request_without_key_file())
        self.assertTrue(result.ok)
        self.assertTrue(result.number_warnings)

    def test_to_dict_is_json_serializable(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            result = write_narrative(self.facts, request_without_key_file())
        json.dumps(result.to_dict(), ensure_ascii=False)


class TestReportRendering(unittest.TestCase):
    def setUp(self):
        self.context = ReportContext(
            run_id="20261002T000000000",
            created_at="2026-10-02 15:00",
            company_name="示例公司",
            company_code="T",
            as_of="2026-09-30",
            data_source="test",
        )
        self.facts = build_fact_sheet(sample_outputs(), context=CONTEXT)

    def test_report_without_narrative_still_has_every_section(self):
        blocks = build_blocks(self.facts, None, {}, self.context)
        markdown = render_markdown(blocks)
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
            self.assertIn(heading, markdown)

    def test_missing_narrative_is_explained(self):
        blocks = build_blocks(self.facts, None, {}, self.context)
        markdown = render_markdown(blocks)
        self.assertIn("本节叙述文字未生成", markdown)

    def test_annotation_labels_present(self):
        markdown = render_markdown(build_blocks(self.facts, None, {}, self.context))
        for label in ("【事实】", "【推论】", "【观点】"):
            self.assertIn(label, markdown)

    def test_chart_reference_uses_relative_path(self):
        charts = {"chart_football_field": Path("/tmp/runs/x/charts/11_football_field.png")}
        markdown = render_markdown(
            build_blocks(self.facts, None, charts, self.context)
        )
        self.assertIn("(charts/11_football_field.png)", markdown)

    def test_html_escapes_model_output(self):
        """大模型写的尖括号必须被转义 —— 报告是给人看的文件，不是执行环境。"""
        from algorithm.output.narrative import NarrativeResult

        narrative = NarrativeResult(
            sections={"risks": "<script>alert(1)</script> 风险。"}, ok=True
        )
        html_text = render_html(
            build_blocks(self.facts, narrative, {}, self.context), title="测试"
        )
        self.assertNotIn("<script>", html_text)
        self.assertIn("&lt;script&gt;", html_text)

    def test_html_has_no_duplicate_closing(self):
        html_text = render_html(build_blocks(self.facts, None, {}, self.context), "T")
        self.assertTrue(html_text.startswith("<!doctype html>"))
        self.assertEqual(html_text.count("</html>"), 1)
        self.assertEqual(html_text.count("<body>"), 1)

    def test_table_pipe_is_escaped(self):
        blocks = [Block(kind="table", header=("a", "b"), rows=(("x|y", "z"),))]
        markdown = render_markdown(blocks)
        self.assertIn("x\\|y", markdown)


if __name__ == "__main__":
    unittest.main()
