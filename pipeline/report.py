"""报告生成：把一次运行的产物拼成一份可读的估值报告。

设计取舍
--------
**从产物文件读，不从内存读。** 报告是流水线的最后一个阶段，输入就是
「交付物」本身 —— 这样改措辞不用重跑计算，而且报告里引用的数字与
评委拿到的产物必然一致（同一个来源）。

**只生成「事实性」内容。** 复述假设、展示结果、列出校验结论 —— 这些都是
从产物里读出来的事实，可以直接生成。涉及判断的文字（「我们认为当前被
低估，因为…」）留给 AI 层，本模块在对应位置留出明显的占位标记。

**缺产物不报错。** 某个阶段没跑（或还没接），对应的节整节跳过、其余照常生成 ——
这样报告可以用于部分运行，也方便在阶段还没接齐时先看效果。
"""

from __future__ import annotations

import csv
import importlib.util
import json
import re
from pathlib import Path
from typing import Any, Sequence

# 依赖探测放在模块级、只探测一次 —— 与输出层的 matplotlib 同一套做法：
# 没装也能 import 这个模块，只是不生成对应格式。
HAS_REPORTLAB = importlib.util.find_spec("reportlab") is not None


def _read_json(path: Path) -> Any | None:
    """读 JSON。文件不存在返回 None（报告里标注「未生成」）。"""
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_csv(path: Path) -> list[dict[str, str]] | None:
    """读 CSV。注意用 utf-8-sig —— 输出层写文件时带了 BOM。"""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8-sig")
    return list(csv.DictReader(text.splitlines()))


def _read_rows(path: Path) -> list[list[str]] | None:
    """按原始行读 CSV，不解析表头。

    有些产物一个文件里放了两张表（比如 07_comps.csv 先列公司、再列倍数统计），
    表头各不相同，用 DictReader 会把第二张表的行按第一张表的列名解析，全错。
    """
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8-sig")
    return [row for row in csv.reader(text.splitlines()) if row]


def _money(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def _percent(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}%}"


def _ratio(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """拼一张 Markdown 表格。"""
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _section(title: str, body: str) -> str:
    return f"\n## {title}\n\n{body.strip()}\n"


AI_PLACEHOLDER = (
    "> 🤖 **待 AI 层填写**：{hint}\n"
    ">\n"
    "> 可以引用的数字见本节表格；写作时请按通知要求区分"
    "「事实 / 推论 / 观点」。"
)


def _ai_todo(hint: str) -> str:
    return AI_PLACEHOLDER.format(hint=hint)


# ---------------------------------------------------------------------------
# 各节
# ---------------------------------------------------------------------------


def _section_conclusion(manifest: dict, valuation: dict, field: dict) -> str:
    per_share = valuation["per_share"]
    price = valuation["price"]
    implied = valuation["implied_return"]
    direction = "被低估" if implied > 0 else "被高估"

    rows = [
        ["每股价值（DCF）", f"{_ratio(per_share)} 元"],
        ["当前股价", f"{_ratio(price)} 元"],
        ["隐含回报", _percent(implied)],
        ["隐含结论", f"{direction} {abs(implied):.2%}"],
        [
            "估值区间（各方法汇总）",
            f"{_ratio(field['overall_low'])} ~ {_ratio(field['overall_high'])} 元",
        ],
    ]
    return _section(
        "一、结论",
        _table(["项目", "数值"], rows) + "\n\n" + _ai_todo(
            "用两三句话概括估值结论，并说明最关键的假设是哪一个。"
        ),
    )


def _section_assumptions(manifest: dict) -> str:
    params = manifest["inputs"]["run_parameters"]
    drivers = params["assumptions"]
    wacc = params["wacc"]

    def series(values, formatter=lambda x: f"{x:.4f}") -> str:
        if isinstance(values, list):
            if len(set(values)) == 1:
                return formatter(values[0]) + "（每年相同）"
            return " / ".join(formatter(v) for v in values)
        return formatter(values)

    growth = series(drivers["revenue_growth"], lambda x: f"{x:.2%}")
    gross = series(drivers["gross_margin"], lambda x: f"{x:.2%}")
    opex = series(drivers["opex_pct_revenue"], lambda x: f"{x:.2%}")
    da = series(drivers["da_pct_revenue"], lambda x: f"{x:.2%}")
    capex = series(drivers["capex_pct_revenue"], lambda x: f"{x:.2%}")
    nwc = (
        f"{drivers['ar_pct_revenue'] [0]:.2%} + "
        f"{drivers['inventory_pct_revenue'][0]:.2%} − "
        f"{drivers['ap_pct_revenue'][0]:.2%} = "
        f"{(drivers['ar_pct_revenue'][0] + drivers['inventory_pct_revenue'][0] - drivers['ap_pct_revenue'][0]):.2%}"
    )

    driver_rows = [
        ["基期", f"{drivers['base_year']} 年，营业收入 {_money(drivers['revenue_base'], 0)} 元"],
        ["预测期", f"{drivers['years']} 年"],
        ["收入增速", growth],
        ["毛利率", gross],
        ["期间费用率", opex],
        ["折旧摊销率", da],
        ["资本开支率", capex],
        ["营运资本占收入比", nwc],
        ["所得税率", _percent(drivers["tax_rate"])],
        ["股利支付率", _percent(drivers["dividend_payout_ratio"])],
    ]

    wacc_rows = [
        ["无风险利率", _percent(wacc["risk_free"]), "输入"],
        ["贝塔", _ratio(wacc["beta"]), "输入"],
        ["股权风险溢价", _percent(wacc["equity_risk_premium"]), "输入"],
        ["税前债务成本", _percent(wacc["cost_of_debt"]), "输入"],
        ["所得税率", _percent(wacc["tax_rate"]), "复用 [drivers]"],
        ["股权权重", _percent(wacc["equity_weight"]), "由市值与债务推导"],
        ["债务权重", _percent(wacc["debt_weight"]), "由市值与债务推导"],
        ["**WACC**", f"**{_percent(wacc['value'], 4)}**", "加权结果"],
    ]

    warnings = wacc.get("sanity_warnings") or []
    warn_text = (
        "\n\n**合理性检查**：" + "；".join(warnings) if warnings else "\n\n**合理性检查**：无异常。"
    )

    body = (
        "### 2.1 经营假设\n\n"
        + _table(["项目", "取值"], driver_rows)
        + "\n\n### 2.2 资本成本（WACC 的完整构成）\n\n"
        + _table(["参数", "取值", "性质"], wacc_rows)
        + warn_text
        + "\n\n"
        + _ai_todo(
            "说明每条关键假设的依据 —— 增速、毛利率、WACC 各参数的来源与"
            "判断理由。这是通知里「假设依据」那一项的主要落点。"
        )
    )
    return _section("二、主要假设", body)


def _section_forecast(projections: dict) -> str:
    years = projections["years"]
    header = ["科目"] + [str(item["year"]) for item in years]

    def row(label: str, getter) -> list[str]:
        return [label] + [_money(getter(item)) for item in years]

    rows = [
        row("营业收入", lambda y: y["income"]["revenue"]),
        row("毛利", lambda y: y["income"]["gross_profit"]),
        row("EBIT", lambda y: y["income"]["ebit"]),
        row("净利润", lambda y: y["income"]["net_profit"]),
        row("经营活动现金流", lambda y: y["cashflow"]["operating"]),
        row("资本开支", lambda y: -y["cashflow"]["capex"]),
        row("期末现金", lambda y: y["balance"]["cash"]),
        row("资产总计", lambda y: y["balance"]["total_assets"]),
        row("负债合计", lambda y: y["balance"]["total_liabilities"]),
        row("权益合计", lambda y: y["balance"]["total_equity"]),
        row("**FCFF**", lambda y: y["fcff"]),
    ]
    gap = projections["worst_balance_gap"]
    note = (
        f"\n\n**三表勾稽**：预测期最大平衡差额 {gap:.2e}"
        "（现金由现金流量表推出，理论上恒为 0）。"
    )
    return _section(
        "三、三表联动预测", _table(header, rows) + note
    )


def _section_validation(validation: dict) -> str:
    reports = validation["reports"]
    header = ["检查项", "说明"] + [str(r["fiscal_year"]) for r in reports]
    marks = {"pass": "通过", "warning": "告警", "fail": "失败", "skipped": "跳过"}

    ids: list[str] = []
    for report in reports:
        for check in report["checks"]:
            if check["check_id"] not in ids:
                ids.append(check["check_id"])

    rows = []
    descriptions: dict[str, str] = {}
    for report in reports:
        for check in report["checks"]:
            descriptions.setdefault(check["check_id"], check["description"])

    for check_id in ids:
        cells = []
        for report in reports:
            found = next(
                (c for c in report["checks"] if c["check_id"] == check_id), None
            )
            cells.append(marks[found["status"]] if found else "—")
        rows.append([check_id, descriptions[check_id], *cells])

    totals = {"pass": 0, "warning": 0, "fail": 0, "skipped": 0}
    for report in reports:
        for key in totals:
            totals[key] += report["summary"][key]

    summary = (
        f"\n\n**汇总**：通过 {totals['pass']}、告警 {totals['warning']}、"
        f"失败 {totals['fail']}、跳过 {totals['skipped']}。"
    )
    note = (
        "\n\n> 注意：「跳过」表示输入缺失、无法检查，**不等于通过**。"
        "预测期没有非经常性损益，所以 X-03 每年都会跳过；"
        "等抽取层接上年报数据后，这一项才有数据可查。"
    )
    return _section("四、财务勾稽校验", _table(header, rows) + summary + note)


def _section_valuation(valuation: dict) -> str:
    rows = [
        ["显式期现值合计", _money(sum(valuation["pv_fcff"]))],
        ["终值", _money(valuation["terminal_value"])],
        ["终值现值", _money(valuation["pv_terminal"])],
        ["**企业价值**", f"**{_money(valuation['enterprise_value'])}**"],
        ["减：净债务", _money(valuation["net_debt"])],
        ["**权益价值**", f"**{_money(valuation['equity_value'])}**"],
        ["摊薄股数", _money(valuation["diluted_shares"], 0)],
        ["**每股价值**", f"**{_ratio(valuation['per_share'])} 元**"],
        ["当前股价", f"{_ratio(valuation['price'])} 元"],
        ["隐含回报", _percent(valuation["implied_return"])],
        ["终值占企业价值", _percent(valuation["terminal_share_of_ev"])],
        ["隐含退出倍数", f"{_ratio(valuation['implied_exit_multiple'])}x"],
    ]
    diagnostics = valuation.get("diagnostics") or []
    diag = (
        "\n\n**诊断**：\n"
        + "\n".join(f"- {item}" for item in diagnostics)
        if diagnostics
        else ""
    )
    return _section("五、现金流折现估值", _table(["项目", "金额（元）"], rows) + diag)


def _section_sensitivity(
    manifest: dict, tornado: list[dict], grid_rows: list[list[str]]
) -> str:
    rows = [
        [
            item["变量"],
            _ratio(float(item["下界"]), 4),
            _ratio(float(item["基准"]), 4),
            _ratio(float(item["上界"]), 4),
            _ratio(float(item["下界每股价值"])),
            _ratio(float(item["上界每股价值"])),
            _ratio(float(item["波动幅度"])),
        ]
        for item in tornado
    ]
    body = "### 6.1 单因素（龙卷风图，按影响幅度降序）\n\n" + _table(
        ["变量", "下界", "基准", "上界", "估值（低）", "估值（高）", "波动幅度"],
        rows,
    )

    attribution = manifest["inputs"]["run_parameters"].get("attribution") or []
    if attribution:
        attr_rows = [
            [item["name"], f"{item['share']:.1%}"] for item in attribution
        ]
        body += (
            "\n\n### 6.2 区间归因\n\n"
            "各参数贡献的估值区间宽度占比：\n\n"
            + _table(["参数", "贡献占比"], attr_rows)
            + "\n\n"
            + _ai_todo(
                "解释贡献最大的那几个参数为什么可靠"
                "（比如「WACC 贡献了 X% 的区间宽度，所以必须说明这个折现率的来源」）。"
            )
        )

    if grid_rows and len(grid_rows) > 1:
        body += "\n\n### 6.3 双因素网格\n\n" + _table(
            grid_rows[0], grid_rows[1:]
        )

    return _section("六、敏感性分析", body)


def _section_reverse(reverse: dict) -> str:
    rows = []
    for item in reverse["results"]:
        base = item["base_value"]
        implied = item["implied_value"]
        label = "偏高" if item["direction"] == "higher" else "偏低"
        rows.append(
            [
                item["name"],
                _ratio(base, 4),
                _ratio(implied, 4),
                f"市场隐含值比基准{label}",
            ]
        )
    note = (
        "\n\n含义：把某个参数反解到「每股价值恰好等于现价」，得到的是"
        "**市场当前在定价的假设**。拿它和公司历史对照，"
        "就能说出一句具体且可证伪的判断。"
    )
    return _section(
        "七、反向验证：市场在定价什么",
        _table(["参数", "基准", "市场隐含", "方向"], rows) + note + "\n\n"
        + _ai_todo("把反解出来的假设与公司历史、行业情况对照，给出判断。"),
    )


def _section_comps(comps_rows: list[list[str]], implied: list[dict]) -> str:
    """07_comps.csv 里有上下两张表：先公司明细，再倍数统计。

    用原始行按位置切分 —— 两张表的表头不一样，不能用一个 DictReader 读到底。
    """
    header = comps_rows[0]
    title_at = next(
        (i for i, row in enumerate(comps_rows) if row and row[0] == "统计口径"),
        len(comps_rows),
    )
    companies = comps_rows[1:title_at]
    stats_header = comps_rows[title_at] if title_at < len(comps_rows) else []
    stats = comps_rows[title_at + 1 :]

    # 公司表的列位置（表头里找，避免以后加列就错位）
    def at(row: list[str], name: str, table_header: list[str]) -> str:
        index = table_header.index(name)
        return row[index] if index < len(row) else ""

    def num(text: str) -> float:
        return float(text.replace(",", "").replace("%", "").strip())

    company_rows = [
        [
            at(row, "名称", header),
            at(row, "类型", header),
            _ratio(num(at(row, "股价", header))),
            _ratio(num(at(row, "市值", header)), 0),
            _ratio(num(at(row, "净债务", header)), 0),
            _ratio(num(at(row, "PE", header))),
            _ratio(num(at(row, "PB", header))),
            _ratio(num(at(row, "EV/EBITDA", header))),
        ]
        for row in companies
    ]
    stat_rows = [
        [
            at(row, "统计口径", stats_header),
            at(row, "样本数", stats_header),
            _ratio(num(at(row, "下四分位", stats_header))),
            _ratio(num(at(row, "中位数", stats_header))),
            _ratio(num(at(row, "上四分位", stats_header))),
        ]
        for row in stats
    ]
    implied_rows = [
        [
            row["倍数"],
            row["统计口径"],
            _ratio(float(row["可比公司倍数"])),
            _ratio(float(row["隐含每股价值"])),
            row["相对现价"],
        ]
        for row in implied
    ]

    body = (
        "### 8.1 可比公司与目标公司\n\n"
        + _table(
            ["公司", "类型", "股价", "市值", "净债务", "PE", "PB", "EV/EBITDA"],
            company_rows,
        )
        + "\n\n### 8.2 倍数统计\n\n"
        + _table(["倍数", "样本数", "下四分位", "中位数", "上四分位"], stat_rows)
        + "\n\n### 8.3 隐含每股价值\n\n"
        + _table(["倍数", "口径", "可比公司倍数", "隐含每股价值", "相对现价"], implied_rows)
        + "\n\n"
        + _ai_todo(
            "说明可比公司是怎么选的（为什么纳入、为什么剔除），"
            "以及不同倍数的结论是否一致、不一致怎么解释。"
        )
    )
    return _section("八、相对估值", body)


def _section_field(field: dict) -> str:
    rows = [
        [band["name"], _ratio(band["low"]), _ratio(band["central"]), _ratio(band["high"])]
        for band in field["bands"]
    ]
    note = (
        f"\n\n现价 {_ratio(field['price'])} 元；"
        f"各方法汇总区间 {_ratio(field['overall_low'])} ~ "
        f"{_ratio(field['overall_high'])} 元。"
    )
    warnings = field.get("warnings") or []
    if warnings:
        note += "\n\n**提示**：\n" + "\n".join(f"- {item}" for item in warnings)
    return _section("九、估值区间汇总（足球场图）", _table(
        ["方法", "下界", "中心", "上界"], rows
    ) + note)


def _section_provenance(manifest: dict) -> str:
    code = manifest["inputs"]["code"]
    files = manifest["inputs"]["inputs"]
    outputs = manifest["outputs"]

    input_rows = [
        [Path(path).name, digest[:16] + "…"] for path, digest in sorted(files.items())
    ]
    output_rows = [
        [item["file"], f"{item['bytes']:,}", item["sha256"][:16] + "…"]
        for item in outputs
    ]
    body = (
        f"- 运行编号：`{manifest['run_id']}`\n"
        f"- 生成时间：{manifest['created_at']}\n"
        f"- 代码版本：`{code['commit_short']}`"
        f"（{'有未提交改动' if code['dirty'] else '工作区干净'}）\n"
        f"- Python：{manifest['python']}\n"
        "\n### 10.1 输入文件\n\n"
        + _table(["文件", "sha256"], input_rows)
        + "\n\n### 10.2 产物清单\n\n"
        + _table(["文件", "字节", "sha256"], output_rows)
    )
    return _section("十、数据来源与可复现性", body)


def _section_charts(run_dir: Path) -> str:
    charts_dir = run_dir / "charts"
    if not charts_dir.exists():
        return _section(
            "附录：图表",
            "本次运行未生成图表（未安装 matplotlib 时输出层会跳过渲染，"
            "安装方式见 README 的「依赖」一节）。",
        )
    files = sorted(charts_dir.glob("*.png"))
    lines = [f"![{path.stem}](charts/{path.name})" for path in files]
    return _section("附录：图表", "\n\n".join(lines))


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def build_report(run_dir: str | Path) -> str:
    """读一次运行的产物，生成估值报告的 Markdown 文本。

    缺产物不报错：对应小节标注「未生成」，其余照常生成。
    """
    run_dir = Path(run_dir)
    manifest = _read_json(run_dir / "10_run_manifest.json")
    if manifest is None:
        raise FileNotFoundError(
            f"{run_dir} 里没有 10_run_manifest.json —— 这不是一次成功的运行"
        )

    company = manifest["inputs"]["run_parameters"]["company"]
    title = f"# {company['name']}（{company['code']}）估值报告\n"
    subtitle = (
        f"\n> 运行编号 `{manifest['run_id']}`"
        f" ｜ 生成于 {manifest['created_at']}"
        f" ｜ 代码版本 `{manifest['inputs']['code']['commit_short']}`\n"
    )

    parts = [title, subtitle]

    projections = _read_json(run_dir / "01_projections.json")
    validation = _read_json(run_dir / "02_validation_report.json")
    valuation = _read_json(run_dir / "03_valuation.json")
    # 04 与 08 的表头规范，用 DictReader；05 是矩阵、07 一个文件两张表，
    # 表头各不相同，必须按原始行读。
    tornado = _read_csv(run_dir / "04_tornado.csv")
    grid = _read_rows(run_dir / "05_sensitivity.csv")
    reverse = _read_json(run_dir / "06_reverse_dcf.json")
    comps = _read_rows(run_dir / "07_comps.csv")
    implied = _read_csv(run_dir / "08_comps_implied.csv")
    field = _read_json(run_dir / "09_football_field.json")

    if valuation and field:
        parts.append(_section_conclusion(manifest, valuation, field))
    parts.append(_section_assumptions(manifest))
    if projections:
        parts.append(_section_forecast(projections))
    if validation:
        parts.append(_section_validation(validation))
    if valuation:
        parts.append(_section_valuation(valuation))
    if tornado:
        parts.append(_section_sensitivity(manifest, tornado, grid or []))
    if reverse:
        parts.append(_section_reverse(reverse))
    if comps and implied:
        parts.append(_section_comps(comps, implied))
    if field:
        parts.append(_section_field(field))
    parts.append(_section_provenance(manifest))
    parts.append(_section_charts(run_dir))

    return "".join(parts)


def write_report(run_dir: str | Path) -> Path:
    """生成报告并写到产物目录里的 report.md，返回路径。"""
    run_dir = Path(run_dir)
    target = run_dir / "report.md"
    target.write_text(build_report(run_dir), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# PDF：把上面那份 Markdown 渲染成 PDF
#
# 为什么要绕 Markdown 这一道：Markdown 是「唯一源」。如果 PDF 直接由数据
# 构建，就有两套布局逻辑，改一处忘一处 —— 报告的两个版本会不一致。
# 渲染器只处理我们自己生成的 Markdown 子集（标题/表格/列表/引用/图片），
# 不追求通用。
# ---------------------------------------------------------------------------


#
# 中文字体：优先用系统字体文件并「嵌入」进 PDF，这样任何阅读器都能显示。
# 退而求其次用 reportlab 内置的 CID 字体（STSong-Light）—— 它不嵌入，
# 依赖阅读器自带 Adobe-GB1 语言包；实测 poppler 就显示不出来，
# 所以只当兜底。
#

_FONT_CANDIDATES: list[tuple[str, str, int]] = [
    # macOS
    ("FinAINormal", "/System/Library/Fonts/STHeiti Light.ttc", 0),
    ("FinAIBold", "/System/Library/Fonts/STHeiti Medium.ttc", 0),
    # Windows
    ("FinAINormal", "C:/Windows/Fonts/msyh.ttc", 0),
    ("FinAIBold", "C:/Windows/Fonts/msyhbd.ttc", 0),
    # Linux
    ("FinAINormal", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("FinAIBold", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
]


def _register_fonts() -> str:
    """注册中文字体，返回正文用的字体名。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    registered: dict[str, str] = {}
    for name, path, index in _FONT_CANDIDATES:
        if name in registered or not Path(path).exists():
            continue
        try:
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, path, subfontIndex=index))
        except Exception:
            continue  # 这个字体读不了就试下一个
        registered[name] = path

    normal = "FinAINormal" if "FinAINormal" in registered else None
    bold = "FinAIBold" if "FinAIBold" in registered else None

    if normal is None:
        # 兜底：reportlab 内置 CID 字体（不嵌入，需要阅读器支持）
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont

        if "STSong-Light" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        return "STSong-Light"

    # 注册字体族，让 <b> 标签能找到粗体
    pdfmetrics.registerFontFamily(
        normal,
        normal=normal,
        bold=bold or normal,
        italic=normal,
        boldItalic=bold or normal,
    )
    return normal


def _pdf_styles():
    """定义排版样式。"""
    from reportlab.lib.styles import ParagraphStyle

    font = _register_fonts()
    return {
        1: ParagraphStyle("h1", fontName=font, fontSize=18, leading=26, spaceAfter=10),
        2: ParagraphStyle("h2", fontName=font, fontSize=14, leading=21, spaceBefore=14, spaceAfter=8),
        3: ParagraphStyle("h3", fontName=font, fontSize=11.5, leading=17, spaceBefore=10, spaceAfter=6),
        "body": ParagraphStyle("body", fontName=font, fontSize=9.5, leading=15, spaceAfter=5),
        "quote": ParagraphStyle(
            "quote", fontName=font, fontSize=9, leading=14, leftIndent=10,
            textColor="#4a5568", spaceAfter=6,
        ),
        "bullet": ParagraphStyle(
            "bullet", fontName=font, fontSize=9.5, leading=15, leftIndent=12, spaceAfter=2
        ),
    }


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(text: str) -> str:
    """把 Markdown 的行内标记转成 reportlab 认的简单标签。先转义再加标签。"""
    out = _escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"`(.+?)`", r'<font face="Courier">\1</font>', out)
    return out


def _flowables(markdown: str, base_dir: Path) -> list:
    """把 Markdown 子集转成 reportlab 的 flowables。"""
    from reportlab.platypus import Image, Paragraph, Spacer, Table, TableStyle

    styles = _pdf_styles()
    flow: list = []
    lines = markdown.splitlines()
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue

        # 标题
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            flow.append(Paragraph(_inline(stripped[level:].strip()), styles[min(level, 3)]))
            i += 1
            continue

        # 图片（附录里的图表）
        picture = re.match(r"!\[([^\]]*)\]\(([^)]+)\)", stripped)
        if picture:
            path = (base_dir / picture.group(2)).resolve()
            if path.exists():
                from reportlab.lib.utils import ImageReader

                width, height = ImageReader(str(path)).getSize()
                target_width = 160 * 72 / 25.4  # 160mm 宽
                flow.append(
                    Image(str(path), width=target_width, height=target_width * height / width)
                )
                flow.append(Spacer(1, 8))
            i += 1
            continue

        # 表格
        if stripped.startswith("|"):
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                # 跳过分隔行（全由 - 和 : 组成）
                if not all(set(c) <= set("-: ") and c for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                body = [
                    [Paragraph(_inline(c), styles["body"]) for c in row] for row in rows
                ]
                table = Table(body, repeatRows=1, hAlign="LEFT")
                table.setStyle(
                    TableStyle(
                        [
                            ("GRID", (0, 0), (-1, -1), 0.4, "#c8d0da"),
                            ("BACKGROUND", (0, 0), (-1, 0), "#eef2f6"),
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 4),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                            ("TOPPADDING", (0, 0), (-1, -1), 3),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                        ]
                    )
                )
                flow.append(table)
                flow.append(Spacer(1, 8))
            continue

        # 引用块（含 AI 占位标记）
        if stripped.startswith(">"):
            block = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                block.append(lines[i].strip().lstrip(">").strip())
                i += 1
            text = "<br/>".join(_inline(b) for b in block if b)
            if text:
                flow.append(Paragraph(text, styles["quote"]))
            continue

        # 无序列表
        if stripped.startswith("- "):
            while i < len(lines) and lines[i].strip().startswith("- "):
                flow.append(
                    Paragraph("• " + _inline(lines[i].strip()[2:]), styles["bullet"])
                )
                i += 1
            flow.append(Spacer(1, 4))
            continue

        # 普通段落（连续行合并成一段）
        block = []
        while i < len(lines):
            candidate = lines[i].strip()
            if not candidate or candidate.startswith(("#", "|", ">", "- ", "![")):
                break
            block.append(candidate)
            i += 1
        if block:
            flow.append(Paragraph(" ".join(_inline(b) for b in block), styles["body"]))

    return flow


def write_pdf(run_dir: str | Path) -> Path | None:
    """把 report.md 渲染成 report.pdf。

    没装 reportlab 时返回 None（不报错）—— 调用方据此打印提示。
    """
    if not HAS_REPORTLAB:
        return None

    from reportlab.lib.pagesizes import A4  # noqa: F401
    from reportlab.platypus import PageBreak, SimpleDocTemplate, Spacer

    run_dir = Path(run_dir)
    source = run_dir / "report.md"
    if not source.exists():
        write_report(run_dir)

    target = run_dir / "report.pdf"
    document = SimpleDocTemplate(
        str(target),
        pagesize=A4,
        title=source.stem,
        leftMargin=18 * 72 / 25.4,
        rightMargin=18 * 72 / 25.4,
        topMargin=18 * 72 / 25.4,
        bottomMargin=18 * 72 / 25.4,
    )

    markdown = source.read_text(encoding="utf-8")
    # 附录（图表）另起一页 —— 否则图片会插在文字中间
    head, sep, tail = markdown.partition("## 附录：图表")
    flow = _flowables(head, run_dir)
    if sep:
        flow += [PageBreak(), *_flowables(sep + tail, run_dir)]
    document.build(flow)
    return target


def _demo() -> None:
    """自检：对最近一次运行生成报告并打印摘要。python3 -m pipeline.report"""
    root = Path(__file__).resolve().parents[1] / "runs"
    if not root.exists():
        raise SystemExit("还没有任何运行，先跑 make run")
    latest = max((path for path in root.iterdir() if path.is_dir()), key=lambda p: p.name)
    target = write_report(latest)
    text = target.read_text(encoding="utf-8")
    print(f"已生成 {target}")
    print(f"  行数 {len(text.splitlines())}，字符 {len(text):,}")
    pdf = write_pdf(latest)
    print(f"  PDF: {pdf}" if pdf else "  PDF: 跳过（没有安装 reportlab）")
    print(f"  节标题：")
    for line in text.splitlines():
        if line.startswith("## "):
            print(f"    {line[3:]}")


if __name__ == "__main__":
    _demo()
