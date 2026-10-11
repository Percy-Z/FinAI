"""叙述层：把算好的结果交给大模型写成报告文字。

这一层的边界（很重要，报告能不能信就看这条线画在哪）
    事实表（fact sheet）  由代码从 RunOutputs 里机械抽出，每个数字都有出处
    叙述文字（narrative） 由大模型写，只允许组织事实表里已有的数字

也就是说：**数字全部由代码产生，大模型一个数都不许算**。它拿到的是成品数字，
要做的是把数字讲成人话，不是替我们估值。写完之后还有一道数字校验（见
verify_numbers）拿它的输出反过来查一遍，凡是事实表里没有的数字都挑出来。

为什么要做数字校验
    指令里写「不许编数字」是约束，不是保证。大模型偶尔会把 9.30 写成 9.3 元、
    把 74% 写成四分之三，甚至凭印象补一个「行业平均 8.5%」。这些错误用肉眼
    很难发现，但用程序查一遍很快。校验的结论写进运行清单，评委能直接看到
    「哪些数字没能对上事实表」——这比声称「我们的大模型很可靠」有说服力。

降级策略
    没有 API key、网络不通、接口报错、返回不是合法 JSON —— 任何一种都只记一条
    提示，然后继续用纯数据版本出报告。报告的结构、表格、图表、数字全都不依赖
    大模型，缺的只是叙述段落。**流水线不会因为大模型不可用而失败。**

可复现性
    大模型输出本身不可复现，所以这里把能固定下来的全部固定：模型名、接口地址、
    温度、提示词版本、提示词与响应的 sha256、耗时、token 用量。原始的问答原文
    写进日志目录，产物清单里只放哈希与元信息。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    # 只为类型检查器导入。reporting.py 在运行时需要 import 本模块来生成报告，
    # 这里再反过来在运行时导入它就会形成循环导入。
    from .reporting import RunOutputs

# 提示词版本。改了 prompt 就升版本，报告里与清单里都要能看到用了哪一版。
PROMPT_VERSION = "report-v2"

# 要写哪些章节。附录不在这里 —— 附录是数据来源与运行信息，由代码机械生成，
# 不需要大模型参与，也不应该让它参与（那些内容必须与文件系统完全一致）。
NARRATIVE_SECTIONS: tuple[tuple[str, str], ...] = (
    (
        "final_verdict",
        "最终结论。**只写一句话**，不超过 60 字，以「综合判断：」开头，"
        "内容包含三件事：当前股价相对内在价值是偏高、偏低还是基本合理；"
        "最重要的支撑理由（只讲一个）；这个判断成立的前提。\n"
        "这一句要能被单独摘出来引用 —— 不要分段、不要用列表、"
        "不要罗列数字。它是整份报告的落点。",
    ),
    (
        "investment_conclusion",
        "投资结论。给出方向性判断：当前股价相对估值区间是偏高、偏低还是基本合理，"
        "理由是什么，这个判断成立的前提是什么。这是研究结论，要明确表态，"
        "不要写成「仅供参考」。最后说明这个结论在什么情况下会失效。"
        "结论的每一个支撑数字都必须来自事实表。",
    ),
    (
        "executive_summary",
        "估值摘要。3 到 5 句话，说清：估值区间与当前股价的关系、隐含回报、"
        "最关键的发现、最大的不确定性。不要逐个复述表格里的数字 —— "
        "表格就在上面，这一节要说的是表格没直接说出来的东西。",
    ),
    (
        "company_overview",
        "公司与预测概览。说明这是一家什么公司、预测期的关键假设走向"
        "（收入增速与利润率的路径），以及预测期结束时公司大概是什么规模。",
    ),
    (
        "assumption_review",
        "假设合理性审查。这是全篇最需要推理的一节。逐条审阅关键假设"
        "（收入增速、利润率、折现率的各个参数、永续增长率），对每一条回答：\n"
        "  · 依据是什么？事实表里有没有支撑它的东西（历史数据、可比公司、"
        "市场隐含值）？\n"
        "  · 这条假设有没有可疑之处？和别的证据矛盾吗？\n"
        "  · 如果它错了，会怎样？\n"
        "  · 需要什么证据才能确认或推翻它？\n"
        "不要复述数字，要说清逻辑。哪一条最可能出错，要明确点名。",
    ),
    (
        "financial_validation",
        "财务勾稽校验。说明校验的结论：多少项通过、多少项跳过、有没有警告或失败。"
        "特别要点明「跳过不等于通过」，以及有哪些检查因为缺数据而没能做。",
    ),
    (
        "valuation_analysis",
        "估值分析。对比现金流折现与相对估值两条路径的结果，说明它们是否互相印证。"
        "如果两条路径给出的结论矛盾或区间不重叠，必须直说，并指出可能的原因。",
    ),
    (
        "sensitivity_analysis",
        "敏感性分析。说清哪个假设对估值的影响最大、估值区间的宽度主要来自哪里，"
        "以及这对研究工作意味着什么（下一步该去核实哪个假设）。",
    ),
    (
        "reverse_dcf",
        "反向估值。说明当前股价隐含了什么样的假设（增长率、折现率、利润率），"
        "并把这些隐含值与基准假设作对比，指出市场比我们更乐观还是更保守。",
    ),
    (
        "risks",
        "风险与适用边界。列出数据缺口、口径假设、方法局限。"
        "凡是事实表里标了 warnings 或 diagnostics 的，都要在这里交代。",
    ),
)

SYSTEM_PROMPT = """\
你是一名金融报告编辑，负责把已经算好的分析结果组织成一份专业的估值报告。

铁律：
1. 只组织已有数据，绝不引入新数字。你写的每一个数字都必须来自给定的「事实表」。
2. 绝不自己计算。不要做任何加减乘除，也不要从两个数推出第三个数。
   需要用到派生数字时，只能用事实表里已经给出的。
3. 不确定性必须保留下来。事实表里的 warnings、diagnostics、notes 记录了
   数据缺口、方法分歧和可疑之处，这些必须在报告里如实呈现，不能修饰掉、
   不能忽略、不能换成一句「整体良好」。宁可写「待核实」，也不要给一个
   数据不支持的自信说法。
4. 区分内容性质：纯粹的既成事实（例如某个数字是多少）不加标记；
   你自己的推断以【推论】开头；价值判断以【观点】开头。
5. 不允许出现裸数字。金额要带单位和币种（例如 975.44 元），
   比率要带口径（例如 2026 年预测收入增速 10%）。
6. 不要写免责声明、不要写「本文不构成投资建议」，那部分由报告模板负责。
7. 语气简洁、数据驱动、不写空话。不要用「值得注意的是」「众所周知」
   「综上所述」这类填充词。
8. 只使用中文。

你要做的不是把数字复述一遍，而是**基于这些数字推理**：
哪条假设站得住、哪条站不住、两条估值路径为什么分歧、
市场隐含的假设和我们自己的假设谁更激进、这个结论在什么条件下会失效。
复述数字是失败的；没有推理的报告一文不值。

关于 final_verdict：它是整份报告的落点，**必须是一句话的判断**，不是分析摘要。
如果评审只读一句，读的应该就是它。不要写成段落。

输出格式：严格输出一个 JSON 对象，键是指定的章节名，值是该章节的正文
（Markdown 段落，可以用列表和加粗）。不要在 JSON 之外写任何解释文字。
"""


@dataclass(frozen=True)
class NarrativeRequest:
    """一次叙述生成请求的参数。

    这些值都会进产物清单，所以「这份报告是哪一版提示词、哪个模型、
    什么温度写出来的」是可以查的。
    """

    enabled: bool = True
    model: str = "deepseek-v4-pro"
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 0.3
    # 输出上限。这里踩过坑：deepseek-v4-pro 是推理模型，会先花一批 token 思考，
    # 再写正文。给 8000 的时候 8000 全被 reasoning 吃掉，正文一个 token 都不剩，
    # 接口返回空内容。该模型上限 393216，这里给 32000：够思考 + 写满七节正文。
    max_tokens: int = 32000
    timeout_s: float = 120.0
    api_key_file: str = "apikey.txt"
    # 推理强度。low 快而省，适合反复调试；high 是接口默认值，出正式报告用。
    effort: str = "high"

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def resolve_api_key(self, project_root: Path | None = None) -> tuple[str, str]:
        """取 API key，返回 (key, 来源说明)。

        先看环境变量，再看本地文件。文件是给「不想每次都 export」的场合用的，
        它必须在 .gitignore 里（仓库根目录的 apikey.txt 已经忽略了）——
        把 key 提交进版本历史是最难挽回的一类失误。

        返回来源说明是为了让它进日志：报告要能说清这次用的是哪来的凭据。
        """
        from_env = os.environ.get(self.api_key_env, "").strip()
        if from_env:
            return from_env, f"环境变量 {self.api_key_env}"

        if not self.api_key_file:
            return "", "未配置"

        candidate = Path(self.api_key_file)
        if not candidate.is_absolute():
            root = project_root or Path(__file__).resolve().parents[2]
            candidate = root / candidate
        if candidate.exists():
            text = candidate.read_text(encoding="utf-8").strip()
            # 容忍 `KEY=值` 这种写法，也容忍只有一行裸 key
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                value = line.split("=", 1)[1].strip() if "=" in line else line
                if value:
                    return value, f"本地文件 {candidate.name}"
        return "", "环境变量与本地文件里都没有"

    def __post_init__(self) -> None:
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError(f"temperature 应落在 [0, 2]，收到 {self.temperature}")
        if self.max_tokens < 256:
            raise ValueError(f"max_tokens 太小：{self.max_tokens}")
        if self.timeout_s <= 0:
            raise ValueError(f"timeout_s 必须为正数，收到 {self.timeout_s}")
        if self.effort not in ("low", "high", "max"):
            raise ValueError(f"effort 只能是 low / high / max，收到 {self.effort!r}")


@dataclass
class NarrativeResult:
    """一次叙述生成的结果。成功与失败都返回这个对象，不做抛异常。"""

    sections: dict[str, str] = field(default_factory=dict)
    ok: bool = False
    notes: list[str] = field(default_factory=list)
    model: str = ""
    endpoint: str = ""
    prompt_version: str = PROMPT_VERSION
    temperature: float = 0.0
    elapsed_s: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    prompt_sha256: str = ""
    response_sha256: str = ""
    number_warnings: list[str] = field(default_factory=list)
    raw_path: str = ""
    key_source: str = ""
    ca_source: str = ""
    rounds: int = 0

    @property
    def section_count(self) -> int:
        return len(self.sections)

    def to_dict(self) -> dict[str, Any]:
        """进产物清单的部分。原始问答不进清单（太长），只留哈希与路径。"""
        return {
            "ok": self.ok,
            "model": self.model,
            "endpoint": self.endpoint,
            "prompt_version": self.prompt_version,
            "key_source": self.key_source,
            "ca_source": self.ca_source,
            "rounds": self.rounds,
            "temperature": self.temperature,
            "elapsed_s": round(self.elapsed_s, 3),
            "usage": self.usage,
            "prompt_sha256": self.prompt_sha256,
            "response_sha256": self.response_sha256,
            "sections": sorted(self.sections),
            "notes": self.notes,
            "number_warnings": self.number_warnings,
            "raw_path": self.raw_path,
        }


def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def build_fact_sheet(
    outputs: RunOutputs, context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """把 RunOutputs 整理成一张「事实表」——报告里允许出现的全部数字。

    为什么要有这么一层：大模型需要的不是原始对象，而是一份边界清楚的数字清单。
    把清单单独构造出来，就能反过来拿它校验大模型的输出（见 verify_numbers）。
    报告里没在清单上的数字，一定是大模型编的。

    字段按报告章节组织，每个只放该章节真正要用的数，不放冗余。
    """
    context = context or {}
    facts: dict[str, Any] = {
        "prompt_version": PROMPT_VERSION,
        # 单位必须写进事实表。不写会怎样：实测大模型会在每个数字后面
        # 补一句「（单位未注明）」，因为提示词要求「不许出现裸数字」，
        # 而它确实不知道单位 —— 它只能如实声明不知道。与其让它猜，
        # 不如把口径一次性交代清楚。
        "units": {
            "金额": "人民币元（CNY）",
            "股数": "股",
            "比率": "小数，0.12 表示 12%",
            "年份": "自然年",
        },
    }

    company = context.get("company")
    if company:
        facts["company"] = {
            "name": company.get("name", ""),
            "code": company.get("code", ""),
            "source": company.get("source", ""),
        }

    # 假设与参数：来自清单里的 run_parameters
    # data_source 也进事实表：报告第 9 节要根据它决定「数据来源」那几句怎么写
    # （手填的过渡数据 / 年报抽取的实际数，两者的适用边界完全不同）。
    for key in ("assumptions", "wacc", "sensitivity", "evidence", "data_source"):
        if key in context:
            facts[key] = context[key]

    if outputs.dcf is not None:
        dcf = outputs.dcf
        facts["dcf"] = {
            "wacc": _round(dcf.wacc),
            "enterprise_value": _round(dcf.enterprise_value, 2),
            "net_debt": _round(dcf.net_debt, 2),
            "equity_value": _round(dcf.equity_value, 2),
            "diluted_shares": _round(dcf.diluted_shares, 2),
            "per_share": _round(dcf.per_share, 2),
            "price": _round(dcf.price, 2),
            "implied_return": _round(dcf.implied_return),
            "terminal_value": _round(dcf.terminal_value, 2),
            "pv_terminal": _round(dcf.pv_terminal, 2),
            "terminal_share_of_ev": _round(dcf.terminal_share_of_ev),
            "implied_exit_multiple": _round(dcf.implied_exit_multiple, 2),
            "diagnostics": list(dcf.diagnostics),
        }

    if outputs.forecast is not None:
        forecast = outputs.forecast
        facts["forecast"] = {
            "worst_balance_gap": _round(forecast.worst_balance_gap, 10),
            "years": [
                {
                    "year": year.year,
                    "revenue": _round(year.income.revenue, 2),
                    "ebit": _round(year.income.ebit, 2),
                    "net_profit_attributable": _round(
                        year.income.net_profit_attributable, 2
                    ),
                    "operating_cash_flow": _round(year.cashflow.operating, 2),
                    "cash": _round(year.balance.cash, 2),
                    "fcff": _round(year.fcff, 2),
                }
                for year in forecast.years
            ],
        }

    if outputs.validation:
        totals = {"pass": 0, "warning": 0, "fail": 0, "skipped": 0}
        per_year: list[dict[str, Any]] = []
        for report in outputs.validation:
            summary = report.summary
            for key in totals:
                totals[key] += summary[key]
            per_year.append({"year": report.fiscal_year, **{
                key: summary[key] for key in ("pass", "warning", "fail", "skipped")
            }})
        facts["validation"] = {
            "totals": totals,
            "per_year": per_year,
            # 非通过项逐条列出来，大模型要按这些写，不能只报总数
            "notable": [
                {
                    "year": report.fiscal_year,
                    "check": check.check_id,
                    "status": check.status,
                    "description": check.description,
                    "note": check.note,
                }
                for report in outputs.validation
                for check in report.checks
                if check.status in ("warning", "fail", "skipped")
            ],
        }

    if outputs.tornado:
        facts["sensitivity"] = {
            "variables": [
                {
                    "name": row.name,
                    "low": _round(row.low),
                    "high": _round(row.high),
                    "per_share_at_low": _round(row.per_share_at_low, 2),
                    "per_share_at_high": _round(row.per_share_at_high, 2),
                    "swing": _round(row.swing, 2),
                }
                for row in outputs.tornado
            ],
            "base_per_share": _round(outputs.tornado[0].per_share_base, 2),
        }

    if outputs.grid is not None:
        low, high = outputs.grid.value_range
        # 用局部变量装，避免在 Any 上做链式索引 —— 类型检查器看不出中间那一层
        sensitivity_facts: dict[str, Any] = facts.setdefault("sensitivity", {})
        sensitivity_facts["grid"] = {
            "row": outputs.grid.row_name,
            "col": outputs.grid.col_name,
            "low": _round(low, 2),
            "high": _round(high, 2),
        }

    if outputs.reverse:
        facts["reverse_dcf"] = [
            {
                "name": item.name,
                "base_value": _round(item.base_value),
                "implied_value": _round(item.implied_value),
                "direction": item.direction,
                "target_per_share": _round(item.target_per_share, 2),
            }
            for item in outputs.reverse
        ]

    if outputs.comps is not None:
        comps = outputs.comps
        entry: dict[str, Any] = {
            "price": _round(comps.price, 2),
            "peer_count": len(comps.peers),
            "multiples": {
                stat.label: {
                    "count": stat.count,
                    "q1": _round(stat.q1, 2),
                    "median": _round(stat.median, 2),
                    "q3": _round(stat.q3, 2),
                }
                for stat in comps.stats.values()
                if stat.count
            },
            "warnings": list(comps.warnings),
        }
        if comps.medians:
            low, high = comps.value_range
            entry["implied_range"] = [_round(low, 2), _round(high, 2)]
            entry["central_value"] = _round(comps.central_value, 2)
            entry["implied_return"] = _round(comps.implied_return)
        facts["comps"] = entry

    if outputs.football_field is not None:
        field_ = outputs.football_field
        facts["football_field"] = {
            "bands": [
                {
                    "name": band.name,
                    "low": _round(band.low, 2),
                    "central": _round(band.central, 2),
                    "high": _round(band.high, 2),
                }
                for band in field_.bands
            ],
            "overall": [_round(field_.overall_low, 2), _round(field_.overall_high, 2)],
            "price": _round(field_.price, 2),
            "warnings": list(field_.warnings),
        }

    notes = (context.get("notes") or {})
    if notes:
        facts["notes"] = notes

    return facts


def build_messages(
    facts: dict[str, Any],
    only: Sequence[str] | None = None,
) -> list[dict[str, str]]:
    """构造 messages。

    only 指定只写哪几节（省略就是全部）。为什么需要这个参数：
    实测把九节一次要齐时，模型写完第一节就停了（finish_reason = stop），
    返回的 JSON 合法但只有一个键。所以调用方要能「补齐缺的那几节」——
    第二轮只把缺的章节名给它，它就不会漏。

    提示词里还会给一个**输出骨架**（所有键名都列出来、值留省略号）。
    不给骨架时模型容易只写开头一节；给了骨架，它把「写满这些键」
    当成一个明确的、可检查的任务。
    """
    wanted = [
        (name, instruction)
        for name, instruction in NARRATIVE_SECTIONS
        if only is None or name in only
    ]
    section_lines = "\n".join(f"- {name}：{text}" for name, text in wanted)
    skeleton = "{\n" + ",\n".join(
        f'  "{name}": "这一节的正文……"' for name, _ in wanted
    ) + "\n}"

    retry_note = (
        "注意：这些章节在上一轮没有写出来，本次必须全部补齐。\n"
        if only is not None
        else ""
    )
    user = (
        "下面是一份已经算好的估值分析结果（事实表）。请按指定章节写出报告正文。\n\n"
        f"### 事实表\n```json\n{json.dumps(facts, ensure_ascii=False, indent=2)}\n```\n\n"
        f"### 需要输出的章节（{len(wanted)} 节，一节都不能少）\n{section_lines}\n\n"
        f"{retry_note}"
        "### 输出格式\n"
        "严格按下面的形状输出一个 JSON 对象，每个键都要有完整正文：\n"
        f"```json\n{skeleton}\n```\n\n"
        "只使用事实表里出现过的数字。事实表里没有的数字，一个字都不要写。"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def parse_sections(content: str) -> tuple[dict[str, str], list[str]]:
    """从大模型返回里解析出章节。

    返回 (章节字典, 提示列表)。解析失败不抛异常 —— 叙述是可选的，缺了报告照样出。
    """
    notes: list[str] = []
    text = content.strip()

    # 去掉可能包裹的代码块围栏
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # 保险起见，截取第一个 { 到最后一个 }
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}, ["返回内容里找不到 JSON 对象"]
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as err:
        return {}, [f"返回内容不是合法 JSON：{err}"]
    if not isinstance(payload, dict):
        return {}, ["返回的 JSON 顶层不是对象"]

    sections = {
        str(key): str(value).strip()
        for key, value in payload.items()
        if isinstance(value, str) and value.strip()
    }
    expected = {name for name, _ in NARRATIVE_SECTIONS}
    missing = sorted(expected - set(sections))
    if missing:
        notes.append(f"缺少章节：{', '.join(missing)}")
    unknown = sorted(set(sections) - expected)
    if unknown:
        notes.append(f"多出未要求的章节（已忽略）：{', '.join(unknown)}")
        for key in unknown:
            sections.pop(key, None)
    return sections, notes


_NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")

# 区间写法：7.32-12.87、9.58 - 9.77、2026—2030。
# 扫描数字前必须先把这种连字符去掉，否则会被当成负号 ——
# 实测一份报告里因此产生 10 处「事实表之外的数字」，全是误报，
# 而误报一多这道校验就没人看了。
_RANGE_PATTERN = re.compile(r"(?<=\d)\s*[-~–—]\s*(?=\d)")


def verify_numbers(
    sections: dict[str, str],
    facts: dict[str, Any],
    tolerance: float = 0.005,
) -> list[str]:
    """检查叙述里出现的数字是否都在事实表里。

    这不是要证明大模型没说错话，而是一个筛子：把「事实表里找不到出处的数字」
    挑出来给人看一眼。它有三类天然漏网，写在这里以免误读这个结论：
        1. 年份、序号、章节编号会被当成普通数字，所以对 1900-2100 与
           小于 100 的整数放宽 —— 代价是真正的小额数字错报会漏掉。
        2. 四舍五入到不同位数会被宽松匹配（带 tolerance）。
        3. 它只查数字，查不出「数字对但话说过头」的问题，那要靠人读。
    """
    allowed: set[float] = set()

    def collect(value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            allowed.add(float(value))
            return
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                collect(item)
            return
        if isinstance(value, str):
            for token in _NUMBER_PATTERN.findall(value):
                try:
                    allowed.add(float(token))
                except ValueError:
                    continue

    collect(facts)

    def is_allowed(value: float) -> bool:
        # 年份、编号、计数：这类整数不参与严格比对
        if value.is_integer() and (1900 <= value <= 2100 or abs(value) < 100):
            return True
        for candidate in allowed:
            if abs(candidate - value) <= tolerance:
                return True
            # 百分比写法：事实表里是 0.7425，正文写 74.25%
            if abs(candidate * 100 - value) <= tolerance * 100:
                return True
        return False

    warnings: list[str] = []
    for name, text in sections.items():
        # 先把区间连接符换成顿号，再扫数字 —— 顺序不能反
        cleaned = _RANGE_PATTERN.sub("至", text)
        for token in _NUMBER_PATTERN.findall(cleaned):
            try:
                value = float(token)
            except ValueError:
                continue
            if not is_allowed(value):
                warnings.append(f"{name} 里出现事实表之外的数字：{token}")
    return warnings


def _ssl_context() -> tuple[ssl.SSLContext | None, str]:
    """构造 HTTPS 用的证书上下文，返回 (上下文, 说明)。

    为什么要自己配：python.org 装出来的 Python，默认 CA 路径指向
    `<prefix>/etc/openssl/cert.pem`，而那个文件通常不存在（官方安装包要求
    你手动跑一次 Install Certificates.command）。于是 urllib 直接报
    「self-signed certificate in certificate chain」—— 看起来像接口的问题，
    其实只是本机没找到根证书。

    取值顺序（前面优先）：
        1. SSL_CERT_FILE 环境变量 —— 用户显式指定，最高优先
        2. certifi 的包 —— 跨平台最稳
        3. 系统证书包 —— macOS 是 /etc/ssl/cert.pem，
           Debian/Ubuntu 是 /etc/ssl/certs/ca-certificates.crt
        4. 都找不到就用默认上下文（可能仍然失败，但至少行为可预期）

    顺序把环境变量放在最前面：如果哪天机器上装了自签根证书（企业代理常见），
    用户可以自己指一个包含它的包，不用改代码。
    """
    explicit = os.environ.get("SSL_CERT_FILE", "").strip()
    if explicit and Path(explicit).exists():
        return ssl.create_default_context(cafile=explicit), f"SSL_CERT_FILE={explicit}"

    try:
        import certifi  # type: ignore[import-not-found]
    except ImportError:
        pass
    else:
        bundle = certifi.where()
        if Path(bundle).exists():
            return ssl.create_default_context(cafile=bundle), "certifi"

    for candidate in (
        "/etc/ssl/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
        "/usr/local/etc/openssl/cert.pem",
    ):
        if Path(candidate).exists():
            return ssl.create_default_context(cafile=candidate), candidate

    return None, "系统默认（未找到证书包）"


# 最多几轮。第一轮要全部章节；后面几轮只补没写出来的那几节。
# 为什么不是一轮：实测把九节一次要齐时，模型写完第一节就停了
# （finish_reason = stop），返回的 JSON 合法但只有一个键。
MAX_ROUNDS = 3


def _call_once(
    request: NarrativeRequest,
    facts: dict[str, Any],
    only: Sequence[str] | None,
    api_key: str,
    ssl_context: ssl.SSLContext | None,
    attempt: int = 1,
) -> tuple[dict[str, str], list[str], dict[str, Any]]:
    """发一次请求，返回 (章节, 提示, 过程信息)。

    任何失败都通过 info["error"] 返回，不抛异常 —— 调用方需要拿到
    失败的那一轮的原始响应去排查，抛出去就丢了。
    """
    messages = build_messages(facts, only=only)
    payload = {
        "model": request.model,
        "messages": messages,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "reasoning_effort": request.effort,
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    info: dict[str, Any] = {
        "attempt": attempt,
        "payload": payload,
        "prompt_sha256": hashlib.sha256(body).hexdigest(),
        "response_sha256": "",
        "usage": {},
        "elapsed_s": 0.0,
        "raw": "",
        "error": "",
        "finish_reason": "",
    }

    http_request = urllib.request.Request(
        request.endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(
            http_request, timeout=request.timeout_s, context=ssl_context
        ) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        detail = ""
        try:
            detail = err.read().decode("utf-8")[:400]
        except Exception:  # noqa: BLE001 - 读错误体失败不影响主流程
            pass
        info["elapsed_s"] = time.perf_counter() - started
        info["error"] = f"接口返回 {err.code}：{detail or err.reason}"
        return {}, [info["error"]], info
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        info["elapsed_s"] = time.perf_counter() - started
        hint = ""
        if "CERTIFICATE_VERIFY_FAILED" in str(err):
            hint = (
                "。这是本机证书链问题，不是接口问题 —— 可以设 SSL_CERT_FILE "
                "指向一个含正确根证书的包，或给这个 Python 跑一次 "
                "Install Certificates.command"
            )
        info["error"] = f"调用失败（网络或超时）：{err}{hint}"
        return {}, [info["error"]], info

    info["elapsed_s"] = time.perf_counter() - started
    info["raw"] = raw
    info["response_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as err:
        info["error"] = f"接口返回不是合法 JSON：{err}"
        return {}, [info["error"]], info

    choices = envelope.get("choices") or []
    if not choices:
        info["error"] = f"接口没有返回 choices：{str(envelope)[:200]}"
        return {}, [info["error"]], info
    choice = choices[0]
    info["finish_reason"] = choice.get("finish_reason") or ""
    info["usage"] = envelope.get("usage") or {}
    content = (choice.get("message") or {}).get("content") or ""
    sections, notes = parse_sections(content)
    if not sections:
        notes.append("这一轮没有解析出任何章节")
    return sections, notes, info


def _sum_usage(usages: list[dict[str, Any]]) -> dict[str, Any]:
    """把多轮的 token 用量加起来。缺的键不补 0，避免出现「输出 0 token」这种误导。"""
    total: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        numbers: list[int] = []
        for usage in usages:
            value = usage.get(key)
            if isinstance(value, int):
                numbers.append(value)
        if numbers:
            total[key] = sum(numbers)
    reasoning_values: list[int] = []
    for usage in usages:
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            tokens = details.get("reasoning_tokens")
            if isinstance(tokens, int):
                reasoning_values.append(tokens)
    if reasoning_values:
        total["reasoning_tokens"] = sum(reasoning_values)
    return total


def write_narrative(
    facts: dict[str, Any],
    request: NarrativeRequest,
    log_dir: str | Path | None = None,
) -> NarrativeResult:
    """调用大模型写叙述。任何失败都降级为「没有叙述」，不抛异常。

    分轮调用：第一轮要全部章节，之后只补没写出来的。这样既省 token
    （一轮写全就不用第二轮），又不会因为模型「写完一节就收工」而漏内容。
    """
    result = NarrativeResult(
        model=request.model,
        endpoint=request.endpoint,
        temperature=request.temperature,
    )

    if not request.enabled:
        result.notes.append("配置里关掉了叙述生成（[report].enabled = false）")
        return result

    api_key, key_source = request.resolve_api_key()
    if not api_key:
        result.notes.append(
            f"没有找到 API key（环境变量 {request.api_key_env} 与 "
            f"{request.api_key_file} 都没有），本次只出数据版本报告"
        )
        return result
    result.key_source = key_source

    ssl_context, ca_source = _ssl_context()
    result.ca_source = ca_source

    expected = [name for name, _ in NARRATIVE_SECTIONS]
    collected: dict[str, str] = {}
    rounds: list[dict[str, Any]] = []
    usages: list[dict[str, Any]] = []
    started = time.perf_counter()

    for attempt in range(1, MAX_ROUNDS + 1):
        missing = [name for name in expected if name not in collected]
        if not missing:
            break
        # 第一轮要全部；后续只补缺的
        only = None if attempt == 1 else missing
        sections, notes, info = _call_once(
            request, facts, only, api_key, ssl_context, attempt=attempt
        )
        rounds.append(info)
        result.notes.extend(notes)
        if info["usage"]:
            usages.append(info["usage"])
        if info["error"]:
            # 接口或网络错误：重试也是白搭，直接停
            break
        collected.update(sections)
        if not sections:
            break

    result.elapsed_s = time.perf_counter() - started
    result.usage = _sum_usage(usages)
    result.rounds = len(rounds)

    if rounds:
        result.prompt_sha256 = rounds[0]["prompt_sha256"]
        combined = "".join(info.get("raw", "") for info in rounds)
        result.response_sha256 = hashlib.sha256(combined.encode("utf-8")).hexdigest()

    missing = [name for name in expected if name not in collected]
    if missing:
        result.notes.append(f"仍未写出的章节：{'、'.join(missing)}")
    result.sections = collected

    if log_dir is not None and rounds:
        # 原始问答写进日志目录：这是过程证据，不是产物。
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        raw_path = directory / "narrative.json"
        raw_path.write_text(
            json.dumps(
                {
                    "rounds": [
                        {
                            "attempt": info["attempt"],
                            "request": info["payload"],
                            "response": _as_json(info.get("raw", "")),
                            "finish_reason": info.get("finish_reason", ""),
                            "elapsed_s": round(info.get("elapsed_s", 0.0), 3),
                        }
                        for info in rounds
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        result.raw_path = str(raw_path)

    if not collected:
        return result

    result.number_warnings = verify_numbers(collected, facts)
    # 只要有一节写出来了就算成功 —— 部分成功比整体判定失败更有用：
    # 报告里缺的那几节会显示「未生成」，其余照常可读。
    result.ok = True
    return result


def _as_json(text: str) -> Any:
    """日志里存原始响应：能解析就存对象，不能就存原文。"""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
