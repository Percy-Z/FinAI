"""活公式 Excel 模型：七个页签，改输入则全表重算。

为什么要有它
------------
通知要求成果以「结构化数据、**表格**或分析报告」呈现。一份单元格里是
活公式的模型既是表格，又能直接证明你们会建模 —— 评委改一个假设，
整个估值跟着变，这比一堆算好的数字有说服力。

和 Python 引擎的关系
--------------------
**口径必须一致**：本模型的公式链复现的是 `algorithm/finai/dcf.py` 那条
路径（收入 → EBIT → NOPAT → FCFF → 折现 → 估值桥），也就是真正产出
`valuation.json` 的那条。所以 Excel 算出的每股价值应当等于引擎的结果 ——
`Checks` 页签最后一行就是这个对照，差额应当是 0。

顺带一提：Excel 里 EBIT 利润率写成「毛利率 − 费用率 − 折旧率」三项相减，
和引擎的映射口径一致。这也是那个「漏减折旧率会高估三成」的坑在 Excel 里
不会重演的原因 —— 公式摊开写，漏不掉。

依赖
----
需要 openpyxl（可选依赖，没装则不生成这个文件）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

HAS_OPENPYXL = importlib.util.find_spec("openpyxl") is not None

YEAR_COLUMNS = ["B", "C", "D", "E", "F", "G", "H", "I", "J"]


def _read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _sheet_title(sheet, text: str, span: int = 6) -> None:
    """在页签顶部写一行标题并加粗。"""
    sheet["A1"] = text
    sheet["A1"].font = _bold_font()
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=span)


def _bold_font():
    from openpyxl.styles import Font

    return Font(bold=True)


def _section_header(sheet, cell: str, text: str) -> None:
    sheet[cell] = text
    sheet[cell].font = _bold_font()


def _build_inputs(book, params: dict, market: dict) -> None:
    """Inputs 页签：所有可改的假设集中在这里。"""
    sheet = book.create_sheet("Inputs")
    sheet.column_dimensions["A"].width = 26
    for column in YEAR_COLUMNS:
        sheet.column_dimensions[column].width = 12
    _sheet_title(sheet, "FinAI 自动化估值模型 — 输入（改这里，Projections 与 DCF 会自动重算）")

    drivers = params["assumptions"]
    wacc = params["wacc"]

    _section_header(sheet, "A3", "【基本参数】")
    rows = [
        ("基期年份", drivers["base_year"], ""),
        ("预测期年数", drivers["years"], "逐年假设的列数应与它一致"),
        ("基期营业收入（元）", drivers["revenue_base"], ""),
    ]
    for offset, (label, value, note) in enumerate(rows, start=4):
        sheet.cell(row=offset, column=1, value=label)
        sheet.cell(row=offset, column=2, value=value)
        sheet.cell(row=offset, column=3, value=note)

    _section_header(sheet, "A8", "【逐年假设】")
    sheet.cell(row=9, column=1, value="项目 \\ 年份")
    for index in range(drivers["years"]):
        sheet.cell(row=9, column=2 + index, value=f"第 {index + 1} 年")

    series_rows = [
        ("收入增速", drivers["revenue_growth"]),
        ("毛利率", drivers["gross_margin"]),
        ("期间费用率", drivers["opex_pct_revenue"]),
        ("折旧摊销率", drivers["da_pct_revenue"]),
        ("资本开支率", drivers["capex_pct_revenue"]),
        ("营运资本占收入比", [
            ar + inv - ap
            for ar, inv, ap in zip(
                drivers["ar_pct_revenue"],
                drivers["inventory_pct_revenue"],
                drivers["ap_pct_revenue"],
            )
        ]),
    ]
    for offset, (label, values) in enumerate(series_rows, start=10):
        sheet.cell(row=offset, column=1, value=label)
        for index, value in enumerate(values[: drivers["years"]]):
            sheet.cell(row=offset, column=2 + index, value=value)
    sheet.cell(row=16, column=1, value="（营运资本 = 应收 + 存货 − 应付，已合并成一个占比）")

    _section_header(sheet, "A18", "【税务与分配】")
    sheet.cell(row=19, column=1, value="所得税率")
    sheet.cell(row=19, column=2, value=drivers["tax_rate"])
    sheet.cell(row=20, column=1, value="股利支付率")
    sheet.cell(row=20, column=2, value=drivers["dividend_payout_ratio"])

    _section_header(sheet, "A22", "【WACC 参数】")
    for offset, (label, key) in enumerate(
        [
            ("无风险利率", "risk_free"),
            ("贝塔", "beta"),
            ("股权风险溢价", "equity_risk_premium"),
            ("税前债务成本", "cost_of_debt"),
        ],
        start=23,
    ):
        sheet.cell(row=offset, column=1, value=label)
        sheet.cell(row=offset, column=2, value=wacc[key])
    sheet.cell(row=27, column=1, value="（税率与权重不在这里 —— 税率复用上面那一个，权重由市值与债务推导）")

    _section_header(sheet, "A29", "【市场数据】")
    market_rows = [
        ("股价（元）", market["price"]),
        ("摊薄股数", market["diluted_shares"]),
        ("有息负债（元）", market["interest_bearing_debt"]),
        ("现金（元）", market["cash"]),
        ("少数股东权益（元）", market["minority_interest"]),
    ]
    for offset, (label, value) in enumerate(market_rows, start=30):
        sheet.cell(row=offset, column=1, value=label)
        sheet.cell(row=offset, column=2, value=value)

    _section_header(sheet, "A36", "【终值假设】")
    sheet.cell(row=37, column=1, value="永续增长率")
    sheet.cell(row=37, column=2, value=drivers.get("terminal_growth", 0.025))


def _build_historicals(book, opening: dict) -> None:
    """Historicals 页签：基期资产负债表。合计行用公式。"""
    sheet = book.create_sheet("Historicals")
    sheet.column_dimensions["A"].width = 26
    sheet.column_dimensions["B"].width = 16
    _sheet_title(sheet, "基期资产负债表（历史事实，来自年报；合计由公式校验）")

    assets = [
        ("货币资金", opening["cash"]),
        ("应收账款", opening["accounts_receivable"]),
        ("存货", opening["inventory"]),
        ("固定资产", opening["fixed_assets"]),
        ("其他非流动资产", opening["other_non_current_assets"]),
    ]
    liabilities = [
        ("应付账款", opening["accounts_payable"]),
        ("有息负债", opening["interest_bearing_debt"]),
        ("其他非流动负债", opening["other_non_current_liabilities"]),
    ]
    equity = [
        ("股本", opening["share_capital"]),
        ("留存收益", opening["retained_earnings"]),
    ]

    row = 3
    for label, value in assets:
        sheet.cell(row=row, column=1, value=label)
        sheet.cell(row=row, column=2, value=value)
        row += 1
    assets_end = row - 1
    sheet.cell(row=row, column=1, value="资产总计").font = _bold_font()
    sheet.cell(row=row, column=2, value=f"=SUM(B3:B{assets_end})")
    assets_total_row = row
    row += 2

    for label, value in liabilities:
        sheet.cell(row=row, column=1, value=label)
        sheet.cell(row=row, column=2, value=value)
        row += 1
    liabilities_end = row - 1
    sheet.cell(row=row, column=1, value="负债合计").font = _bold_font()
    sheet.cell(row=row, column=2, value=f"=SUM(B{liabilities_end - 2}:B{liabilities_end})")
    liabilities_total_row = row
    row += 1

    equity_start = row
    for label, value in equity:
        sheet.cell(row=row, column=1, value=label)
        sheet.cell(row=row, column=2, value=value)
        row += 1
    equity_end = row - 1
    sheet.cell(row=row, column=1, value="权益合计").font = _bold_font()
    sheet.cell(row=row, column=2, value=f"=SUM(B{equity_start}:B{equity_end})")
    equity_total_row = row
    row += 1

    sheet.cell(row=row, column=1, value="负债与权益合计").font = _bold_font()
    sheet.cell(
        row, column=2, value=f"=B{liabilities_total_row}+B{equity_total_row}"
    )
    combined_row = row
    row += 1

    sheet.cell(row=row, column=1, value="平衡差额（应为 0）").font = _bold_font()
    sheet.cell(row=row, column=2, value=f"=B{assets_total_row}-B{combined_row}")


def _build_projections(book, drivers: dict) -> None:
    """Projections 页签：从收入推到 FCFF，全部公式。"""
    sheet = book.create_sheet("Projections")
    sheet.column_dimensions["A"].width = 26
    for column in YEAR_COLUMNS:
        sheet.column_dimensions[column].width = 13
    _sheet_title(sheet, "三表联动预测（全部由公式计算，改 Inputs 会自动重算）", span=7)

    years = drivers["years"]
    columns = YEAR_COLUMNS[:years]

    sheet.cell(row=3, column=1, value="科目").font = _bold_font()
    for index, column in enumerate(columns):
        sheet.cell(row=3, column=2 + index, value=f"第 {index + 1} 年").font = _bold_font()

    def put(row: int, label: str, first: str, later: str, *, bold: bool = False) -> None:
        cell = sheet.cell(row=row, column=1, value=label)
        if bold:
            cell.font = _bold_font()
        for index, column in enumerate(columns):
            formula = first.format(col=column, prev=columns[index - 1]) if index == 0 else later.format(
                col=column, prev=columns[index - 1]
            )
            target = sheet.cell(row=row, column=2 + index, value=formula)
            if bold:
                target.font = _bold_font()

    # 年份与假设（直接从 Inputs 引）
    put(4, "年份（基期+1 起）", "=Inputs!B4+1", "={prev}4+1")
    put(5, "收入增速", "=Inputs!B10", "=Inputs!{col}10")
    put(6, "营业收入", "=Inputs!B6*(1+B5)", "={prev}6*(1+{col}5)", bold=True)
    put(7, "毛利率", "=Inputs!B11", "=Inputs!{col}11")
    put(8, "毛利", "=B6*B7", "={col}6*{col}7")
    put(9, "期间费用率", "=Inputs!B12", "=Inputs!{col}12")
    put(10, "期间费用", "=B6*B9", "={col}6*{col}9")
    put(11, "折旧摊销率", "=Inputs!B13", "=Inputs!{col}13")
    put(12, "折旧摊销", "=B6*B11", "={col}6*{col}11")

    # EBIT 利润率 = 毛利率 − 费用率 − 折旧率
    # 这三项相减就是引擎的映射口径；漏掉任何一项都会高估估值。
    sheet.cell(row=13, column=1, value="EBIT 利润率（毛利率−费用率−折旧率）")
    for index, column in enumerate(columns):
        sheet.cell(row=13, column=2 + index, value=f"={column}7-{column}9-{column}11")
    put(14, "EBIT", "=B6*B13", "={col}6*{col}13", bold=True)

    put(15, "所得税率", "=Inputs!B19", "=Inputs!B19")
    put(16, "NOPAT（EBIT×(1−税率)）", "=B14*(1-B15)", "={col}14*(1-{col}15)", bold=True)
    put(17, "资本开支率", "=Inputs!B14", "=Inputs!{col}14")
    put(18, "资本开支", "=B6*B17", "={col}6*{col}17")
    put(19, "营运资本占收入比", "=Inputs!B15", "=Inputs!{col}15")
    put(20, "营运资本", "=B6*B19", "={col}6*{col}19")

    # 第一期上年营运资本 = 基期收入 × 占比；之后取上一期的营运资本
    sheet.cell(row=21, column=1, value="营运资本增加")
    first = columns[0]
    sheet.cell(row=21, column=2, value=f"={first}20-Inputs!$B$6*Inputs!B15")
    for index in range(1, years):
        column, previous = columns[index], columns[index - 1]
        sheet.cell(
            row=21, column=2 + index, value=f"={column}20-{previous}20"
        )

    put(22, "FCFF", "=B16+B12-B18-B21", "={col}16+{col}12-{col}18-{col}21", bold=True)


def _build_wacc(book) -> None:
    """WACC 页签：CAPM 与加权，全部公式。"""
    sheet = book.create_sheet("WACC")
    sheet.column_dimensions["A"].width = 26
    sheet.column_dimensions["B"].width = 14
    _sheet_title(sheet, "资本成本（全部由公式计算）")

    _section_header(sheet, "A3", "股权成本（CAPM）")
    for row, (label, formula) in enumerate(
        [
            ("无风险利率", "=Inputs!B23"),
            ("贝塔", "=Inputs!B24"),
            ("股权风险溢价", "=Inputs!B25"),
            ("股权成本 = Rf + β×ERP", "=B4+B5*B6"),
        ],
        start=4,
    ):
        sheet.cell(row=row, column=1, value=label)
        sheet.cell(row=row, column=2, value=formula)

    _section_header(sheet, "A9", "债务成本")
    for row, (label, formula) in enumerate(
        [
            ("税前债务成本", "=Inputs!B26"),
            ("所得税率", "=Inputs!B19"),
            ("税后债务成本 = Rd×(1−t)", "=B10*(1-B11)"),
        ],
        start=10,
    ):
        sheet.cell(row=row, column=1, value=label)
        sheet.cell(row=row, column=2, value=formula)

    _section_header(sheet, "A14", "资本结构（由市值与债务推导）")
    for row, (label, formula) in enumerate(
        [
            ("市值", "=Inputs!B30*Inputs!B31"),
            ("有息负债", "=Inputs!B32"),
            ("股权权重", "=B15/(B15+B16)"),
            ("债务权重", "=B16/(B15+B16)"),
        ],
        start=15,
    ):
        sheet.cell(row=row, column=1, value=label)
        sheet.cell(row=row, column=2, value=formula)

    sheet.cell(row=20, column=1, value="WACC").font = _bold_font()
    sheet.cell(row=20, column=2, value="=B17*B7+B18*B12").font = _bold_font()


def _build_dcf(book, drivers: dict) -> None:
    """DCF 页签：折现、终值、估值桥，全部公式。"""
    sheet = book.create_sheet("DCF")
    sheet.column_dimensions["A"].width = 26
    for column in YEAR_COLUMNS:
        sheet.column_dimensions[column].width = 13
    _sheet_title(sheet, "现金流折现与估值桥（全部由公式计算）", span=7)

    years = drivers["years"]
    columns = YEAR_COLUMNS[:years]

    sheet.cell(row=3, column=1, value="科目").font = _bold_font()
    for index in range(years):
        sheet.cell(row=3, column=2 + index, value=f"第 {index + 1} 年").font = _bold_font()

    sheet.cell(row=4, column=1, value="期数")
    for index in range(years):
        sheet.cell(row=4, column=2 + index, value=index + 1)

    sheet.cell(row=5, column=1, value="FCFF")
    for index, column in enumerate(columns):
        sheet.cell(row=5, column=2 + index, value=f"=Projections!{column}22")

    sheet.cell(row=6, column=1, value="折现因子 = 1/(1+WACC)^t")
    for index, column in enumerate(columns):
        sheet.cell(row=6, column=2 + index, value=f"=1/(1+WACC!$B$20)^{column}4")

    sheet.cell(row=7, column=1, value="现值")
    for index, column in enumerate(columns):
        sheet.cell(row=7, column=2 + index, value=f"={column}5*{column}6")

    last = columns[-1]
    first = columns[0]
    summary = [
        ("显式期现值合计", f"=SUM(B7:{last}7)"),
        ("末年 FCFF", f"={last}5"),
        ("永续增长率", "=Inputs!B37"),
        ("终值 = FCFF_末×(1+g)/(WACC−g)", "=B9*(1+B10)/(WACC!B20-B10)"),
        ("终值现值", f"=B11*{last}6"),
        ("企业价值", "=B8+B12"),
        ("减：有息负债", "=Inputs!B32"),
        ("加：现金", "=Inputs!B33"),
        ("减：少数股东权益", "=Inputs!B34"),
        ("权益价值", "=B13-B14+B15-B16"),
        ("摊薄股数", "=Inputs!B31"),
        ("每股价值", "=B17/B18"),
        ("当前股价", "=Inputs!B30"),
        ("隐含回报", "=B19/B20-1"),
        ("终值占企业价值", "=B12/B13"),
        ("隐含退出倍数（终值/末年EBITDA）", f"=B11/(Projections!{last}14+Projections!{last}12)"),
    ]
    row = 8
    for label, formula in summary:
        sheet.cell(row=row, column=1, value=label)
        sheet.cell(row=row, column=2, value=formula)
        row += 1
    # 关键结果加粗
    for target in ("B13", "B17", "B19"):
        sheet[target].font = _bold_font()
    _ = first  # 显式期起点，供以后扩展用


def _build_sensitivities(book, math_result: dict) -> None:
    """Sensitivities 页签：敏感性结果。

    这块**不放活公式** —— 龙卷风图的每一行都要完整重算一次 DCF，
    用工作表公式表达要么爆炸要么得靠数据表功能。所以这里放引擎算好的结果，
    并注明来源与重算方式。
    """
    sheet = book.create_sheet("Sensitivities")
    sheet.column_dimensions["A"].width = 18
    for column in "BCDEFGH":
        sheet.column_dimensions[column].width = 13
    _sheet_title(
        sheet,
        "敏感性分析结果（由 Python 引擎一次算出；变量表与幅度来自 data/assumptions.toml）",
        span=7,
    )

    _section_header(sheet, "A3", "单因素（按波动幅度降序）")
    header = ["变量", "下界", "基准", "上界", "估值（低）", "估值（高）", "波动幅度"]
    for index, text in enumerate(header):
        sheet.cell(row=4, column=1 + index, value=text).font = _bold_font()
    for offset, item in enumerate(math_result["tornado"]):
        row = 5 + offset
        for index, key in enumerate(
            ["变量", "下界", "基准", "上界", "下界每股价值", "上界每股价值", "波动幅度"]
        ):
            value = item[key]
            try:
                value = float(value)
            except (TypeError, ValueError):
                pass
            sheet.cell(row=row, column=1 + index, value=value)

    start = 5 + len(math_result["tornado"]) + 1
    _section_header(sheet, f"A{start}", "区间归因（各参数贡献的区间宽度占比）")
    for index, text in enumerate(["参数", "贡献占比"]):
        sheet.cell(row=start + 1, column=1 + index, value=text).font = _bold_font()
    for offset, item in enumerate(math_result["attribution"]):
        sheet.cell(row=start + 2 + offset, column=1, value=item["name"])
        sheet.cell(row=start + 2 + offset, column=2, value=item["share"])

    grid_start = start + 3 + len(math_result["attribution"])
    _section_header(sheet, f"A{grid_start}", "双因素网格")
    for offset, row_values in enumerate(math_result["grid"]):
        for index, cell in enumerate(row_values):
            sheet.cell(row=grid_start + 1 + offset, column=1 + index, value=cell)


def _build_checks(book, checks: dict) -> None:
    """Checks 页签：勾稽校验结论 + 与 Python 引擎的对照。"""
    sheet = book.create_sheet("Checks")
    sheet.column_dimensions["A"].width = 20
    sheet.column_dimensions["B"].width = 46
    for column in "CDEFG":
        sheet.column_dimensions[column].width = 10
    _sheet_title(sheet, "勾稽校验结论（由 Python 引擎算出）+ 与 Excel 公式的对照", span=7)

    marks = {"pass": "通过", "warning": "告警", "fail": "失败", "skipped": "跳过"}
    reports = checks["reports"]
    header = ["检查项", "说明"] + [str(item["fiscal_year"]) for item in reports]
    for index, text in enumerate(header):
        sheet.cell(row=3, column=1 + index, value=text).font = _bold_font()

    ids: list[str] = []
    descriptions: dict[str, str] = {}
    for report in reports:
        for check in report["checks"]:
            if check["check_id"] not in ids:
                ids.append(check["check_id"])
            descriptions.setdefault(check["check_id"], check["description"])

    for offset, check_id in enumerate(ids):
        row = 4 + offset
        sheet.cell(row=row, column=1, value=check_id)
        sheet.cell(row=row, column=2, value=descriptions[check_id])
        for index, report in enumerate(reports):
            found = next(
                (c for c in report["checks"] if c["check_id"] == check_id), None
            )
            sheet.cell(
                row=row,
                column=3 + index,
                value=marks[found["status"]] if found else "—",
            )

    row = 4 + len(ids) + 1
    sheet.cell(row=row, column=1, value="注意「跳过」不等于通过").font = _bold_font()
    sheet.cell(
        row, column=2, value="输入缺失时无法检查，与「平了」是两回事。"
    )

    row += 2
    _section_header(sheet, f"A{row}", "活公式的自我校验：Excel 与 Python 引擎应当一致")
    engine_value = checks["engine_per_share"]
    rows = [
        ("Python 引擎算出的每股价值", engine_value),
        ("Excel 公式算出的每股价值", "=DCF!B19"),
        ("差额（应为 0 —— 改了 Inputs 也应当保持）", f"=B{row + 2}-B{row + 1}"),
    ]
    for offset, (label, value) in enumerate(rows, start=row + 1):
        sheet.cell(row=offset, column=1, value=label)
        sheet.cell(row=offset, column=2, value=value)


def build_workbook(run_dir: str | Path) -> Path | None:
    """生成活公式 Excel 模型。没装 openpyxl 时返回 None。"""
    if not HAS_OPENPYXL:
        return None

    from openpyxl import Workbook

    run_dir = Path(run_dir)
    manifest = _read_json(run_dir / "10_run_manifest.json")
    if manifest is None:
        raise FileNotFoundError(f"{run_dir} 里没有运行清单，无法生成模型")

    params = manifest["inputs"]["run_parameters"]
    valuation = _read_json(run_dir / "03_valuation.json") or {}
    checks_raw = _read_json(run_dir / "02_validation_report.json") or {"reports": []}
    tornado = _read_csv_or_empty(run_dir / "04_tornado.csv")
    grid = _read_rows_or_empty(run_dir / "05_sensitivity.csv")

    book = Workbook()
    book.remove(book.active)  # 去掉默认的 Sheet
    _build_inputs(book, params, params["market"])
    _build_historicals(book, params["opening_balance"])
    _build_projections(book, params["assumptions"])
    _build_wacc(book)
    _build_dcf(book, params["assumptions"])
    _build_sensitivities(
        book,
        {
            "tornado": tornado,
            "attribution": params.get("attribution") or [],
            "grid": grid,
        },
    )
    _build_checks(
        book,
        {
            "reports": checks_raw.get("reports", []),
            "engine_per_share": valuation.get("per_share"),
        },
    )

    target = run_dir / "model.xlsx"
    book.save(target)
    return target


def _read_csv_or_empty(path: Path) -> list[dict[str, str]]:
    import csv

    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8-sig")
    return list(csv.DictReader(text.splitlines()))


def _read_rows_or_empty(path: Path) -> list[list[str]]:
    import csv

    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8-sig")
    return [row for row in csv.reader(text.splitlines()) if row]


def _demo() -> None:
    """自检：对最近一次运行生成 Excel 模型。python3 -m pipeline.workbook"""
    root = Path(__file__).resolve().parents[1] / "runs"
    latest = max((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    target = build_workbook(latest)
    if target is None:
        raise SystemExit("没有安装 openpyxl，跳过（pip install openpyxl）")
    print(f"已生成 {target}")


if __name__ == "__main__":
    _demo()
