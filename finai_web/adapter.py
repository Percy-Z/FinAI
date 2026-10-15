"""接流水线：抽取结果 → 流水线认识的对象。

两个接入点，对应通知里说的「替换 [opening_balance] 和 peers.csv 的读取」：

    1. 基期资产负债表
       原先 `data/assumptions.toml` 的 `[opening_balance]` 小节是人手填的。
       现在改由年报抽取结果生成，写成 `data/extracted/pipeline_input.json`，
       `pipeline.config.load_opening_balance(source="extracted")` 读它。
       字段形状完全一致（都是 `BalanceSheetState` 的 11 个科目），
       所以三表预测、勾稽校验、估值——下游一行都不用改。

    2. 可比公司财务
       原先 `data/peers.csv` 是手填的。现在可以由抽取结果生成，
       而且仍然写出**同样的 CSV 格式**（同一套表头、同一套注释约定）——
       这样即使有人回退到 `source="config"`，读到的新旧两份文件也不会打架。

还额外提供 `render_placeholders`：报告/配置模板里写 `[cash]`、`[ebitda]`
这类占位符时，用它替换成抽取出来的数，并返回**没被替换掉的占位符**
（默认不静默：模板里留着 `[xxx]` 说明有字段没接上，必须让人看见）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import extract, schema
from .config import PATHS


class AdapterError(ValueError):
    """抽取结果拼不成流水线输入。"""


# 占位符形状：`[字段名]`。字段名允许中文（报告模板里写 [期初现金] 也认）。
# 后面跟 ( 或 [ 的不算 —— 那是 Markdown 链接 [文字](链接)，不是占位符，
# 报成「没命中」会把模板里的正常链接变成一条假警报。
_PLACEHOLDER = re.compile(
    r"\[([A-Za-z_\u4e00-\u9fff][A-Za-z0-9_.\u4e00-\u9fff]*)\](?![(\[])"
)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _scale(values: Mapping[str, Any], rate: float) -> dict[str, Any]:
    """把金额按汇率换算；非数字字段（code / as_of）原样保留。"""
    if rate == 1.0:
        return dict(values)
    return {
        key: (float(value) * rate if isinstance(value, (int, float)) else value)
        for key, value in values.items()
    }


def _resolve_fx(
    records: Mapping[str, Mapping[str, Any]],
    target_code: str,
    *,
    fx_rate: float | None,
    fx_rates: Mapping[str, float] | None = None,
    base_currency: str,
) -> dict[str, Any]:
    """确定每个币种用什么汇率换算成本位币，并把结果记下来。

    规则：
        - 报表币种 == 目标币种（默认人民币）→ 不换算，rate = 1.0
        - 报表币种 != 目标币种 → 必须显式给这个币种的汇率，否则报错（不猜、不假设 1:1）
        - **每个币种各用各的汇率**：目标公司是美元报表、可比公司是人民币报表时，
          只要把两种汇率都给出来就能放在同一张表里比；缺哪一个币种的汇率，
          那家公司单独跳过并写明原因（不拿两种货币的数字直接相减）
    """
    given: dict[str, float] = {}
    for name, value in (fx_rates or {}).items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            given[str(name).strip().upper()] = number
    currency = str(
        (records.get(target_code) or {}).get("currency") or ""
    ).upper() or base_currency
    if fx_rate is not None and fx_rate > 0:
        # 单币种的老写法：只给一个数就是「目标公司那个币种」的汇率
        given.setdefault(currency, float(fx_rate))
    rates = {base_currency: 1.0, **given}

    # 每个代码一份汇率：下游要用它把**价格**也换算过来 —— 价格是报价币种
    # （美股美元、港股港币），只换财报不换价格，市值就会差一个汇率。
    per_code: dict[str, dict[str, Any]] = {}
    for code, record in records.items():
        name = str(record.get("currency") or "").upper() or base_currency
        rate = rates.get(name)
        if rate:
            per_code[str(code)] = {"currency": name, "rate": float(rate)}

    if currency not in rates:
        raise AdapterError(
            f"这份报表以 {currency} 计价，而流水线的单位约定是 {base_currency}。\n"
            f"    必须显式给汇率（本模块不猜、也不假设 1:1）：\n"
            f"        工作台   在「③ 取数并生成假设」的「外币报表」一栏填（1 {currency} = ? {base_currency}），再点一次开始取数\n"
            f"        命令行   python -m finai_web build --target {target_code} --fx <1 {currency} 等于多少 {base_currency}>\n"
            "    例如 1 USD = 7.15 CNY 就写 --fx 7.15。"
        )
    return {
        "from": currency,
        "to": base_currency,
        "rate": float(per_code[str(target_code)]["rate"]) if str(target_code) in per_code else 1.0,
        "converted": currency != base_currency,
        "rates_by_currency": {name: float(value) for name, value in rates.items()},
        "rates": per_code,
        "note": (
            "金额与价格都按各自币种的汇率换算成本位币；汇率由使用者提供，"
            "本模块不联网取汇率"
            + ("" if currency == base_currency else f"（1 {currency} = {rates[currency]} {base_currency}）")
        ),
    }


def opening_balance_from(record: Mapping[str, Any]) -> dict[str, float]:
    """一条抽取记录 → `BalanceSheetState` 的构造参数。"""
    block = record.get("opening_balance")
    if not isinstance(block, Mapping):
        raise AdapterError(
            f"{record.get('code')} {record.get('year')} 的记录里没有 opening_balance —— "
            "多半是基期科目没抽全，先看该记录的 notes 再说。"
        )
    values = block.get("values") or {}
    missing = [name for name in schema.OPENING_FIELDS if name not in values]
    if missing:
        raise AdapterError(
            f"{record.get('code')} {record.get('year')} 的基期数据缺字段：{missing}"
        )
    return {name: float(values[name]) for name in schema.OPENING_FIELDS}


def peer_from(record: Mapping[str, Any], *, as_of: str | None = None) -> dict[str, Any]:
    """一条抽取记录 → `PeerFinancials` 的构造参数。"""
    block = record.get("peer")
    if not isinstance(block, Mapping):
        raise AdapterError(
            f"{record.get('code')} {record.get('year')} 的记录里没有 peer —— "
            "可比公司指标（收入 / 归母净利润 / 归母净资产）没抽全。"
        )
    values = block.get("values") or {}
    missing = [name for name in schema.PEER_FIELDS if name not in values]
    if missing:
        raise AdapterError(f"{record.get('code')} {record.get('year')} 的可比指标缺字段：{missing}")
    row: dict[str, Any] = {"code": str(record.get("code") or "")}
    for name in schema.PEER_FIELDS:
        row[name] = float(values[name])
    # 期间口径：优先写报表期末的确切日期（美国财年不是自然年，
    # 「2025A」会让人以为是 2025-12-31）；没有期末日期时退回「2025A」这种写法。
    row["as_of"] = (
        as_of
        or str(record.get("period_end") or "")
        or str(record.get("period") or f"{record.get('year')}A")
    )
    return row


def build_pipeline_input(
    records: Sequence[Mapping[str, Any]],
    *,
    target_code: str | None = None,
    generated_at: str | None = None,
    fx_rate: float | None = None,
    fx_rates: Mapping[str, float] | None = None,
    base_currency: str = "CNY",
) -> dict[str, Any]:
    """把逐条抽取记录汇总成流水线要的那一份输入。

    target_code 指定哪一家是被估值公司（role=target）；
    其余进 peers。目标公司同时给出 `target_actuals`，
    供相对估值把目标公司的利润指标也换成**最近一个完整年度的实际数** ——
    这样目标与可比才是同一个口径（明细见 pipeline/adapters.py 的说明）。

    币种（fx_rate）
        流水线的单位约定是人民币元。如果年报用别的货币计价（例如美国 10-K 的
        美元百万），必须显式给汇率，这里会统一换算并在 `fx` 里留痕；
        不给汇率就报错 —— 把「美元百万」当成「人民币元」直接算进估值，
        数字看着正常，结论会差一个数量级，而且没人看得出来。

        `fx_rates` 是**按币种**给汇率（{"USD": 7.15, "HKD": 0.92}）：这样目标公司是
        美元报表、可比公司是港元/人民币报表时也能放在同一张表里比。缺哪个币种的
        汇率，用那个币种的公司就单独跳过并写明原因。`fx` 里带出每个代码的汇率，
        下游要拿它换算**价格**（价格是报价币种，不换会把市值算错一个汇率）。
    """
    if not records:
        raise AdapterError(
            "没有任何抽取记录。先跑 `python -m finai_web extract`（或 `finai ingest run`）。"
        )

    newest = extract.latest_by_code(records)
    if target_code:
        if target_code not in newest:
            raise AdapterError(
                f"抽取结果里没有目标公司 {target_code}。已有：{sorted(newest)}"
            )
        target_code = str(target_code)
    else:
        target_code = str(next(iter(newest)))

    target_record = newest[target_code]
    _require_usable(target_record, role="目标公司")
    fx = _resolve_fx(
        newest,
        target_code,
        fx_rate=fx_rate,
        fx_rates=fx_rates,
        base_currency=base_currency,
    )
    rates = fx.get("rates") or {}

    def rate_of(code: Any) -> float:
        entry = rates.get(str(code)) or {}
        return float(entry.get("rate") or 1.0)

    payload: dict[str, Any] = {
        "version": 1,
        "generated_at": generated_at or _now(),
        "target_code": target_code,
        "fx": fx,
        "opening_balance": _scale(opening_balance_from(target_record), rate_of(target_code)),
        "target_actuals": _scale(peer_from(target_record), rate_of(target_code)),
        "history": history_from(target_record),
        "peers": {},
        "provenance": {},
    }

    for code, record in newest.items():
        payload["provenance"][code] = {
            "year": record.get("year"),
            "period": record.get("period"),
            "pdf": record.get("pdf"),
            "sha256": record.get("sha256"),
            "source_url": record.get("source_url"),
            "method": record.get("method"),
            "extracted_at": record.get("extracted_at"),
            "opening_provenance": (record.get("opening_balance") or {}).get("provenance", {}),
            "peer_provenance": (record.get("peer") or {}).get("provenance", {}),
            "notes": record.get("notes", []),
        }
        if str(code) == target_code:
            continue
        peer_currency = str(record.get("currency") or "").upper()
        if str(code) not in rates:
            # 这个币种没给汇率：不换算、也不静默按 1:1 算 —— 单独跳过并说清怎么补
            payload["provenance"][str(code)]["skipped"] = (
                f"币种 {peer_currency or '未标注'} 没给汇率 —— 换算成 {base_currency} "
                "需要它自己那一份汇率。工作台的「外币报表」里把 "
                f"「1 {peer_currency or '外币'} = ? {base_currency}」也填上就能一起比"
            )
            continue
        try:
            payload["peers"][str(code)] = _scale(peer_from(record), rate_of(code))
        except AdapterError as err:
            # 可比公司缺指标只跳过这一家：少一家可比不影响整条流水线，
            # 但必须写进 provenance，报告里要说得出「为什么这家没进表」。
            payload["provenance"][code]["skipped"] = str(err)

    # 可比公司里若有「文字层不可用」的记录，跳过它并写清原因（它本来也抽不出指标）
    for code, record in newest.items():
        if str(code) == target_code or record.get("text_layer_usable") is not False:
            continue
        payload["provenance"][str(code)]["skipped"] = _unusable_reason(record)

    return payload


def _unusable_reason(record: Mapping[str, Any]) -> str:
    return (
        f"{record.get('code')} 的 PDF 文字层不可用（字体缺 ToUnicode 映射，抽出来是 cid 码）——"
        "本地解析救不回来。三条出路：① 换一份带文字层的 PDF（例如英文版，或用阅读器"
        "「打印成 PDF」重新导出）；② 先 OCR 再抽；③ 用 Firecrawl 之类的云端解析。"
    )


def _require_usable(record: Mapping[str, Any], *, role: str) -> None:
    """目标公司的记录如果文字层不可用，直接停下 —— 不让下游拿到残缺的基期数据。"""
    if record.get("text_layer_usable") is False:
        raise AdapterError(_unusable_reason(record).replace("的 PDF", f"（{role}）的 PDF", 1))


def write_pipeline_input(payload: Mapping[str, Any], path: Path | None = None) -> Path:
    target = path or PATHS.pipeline_input
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def read_pipeline_input(path: Path | None = None) -> dict[str, Any]:
    target = path or PATHS.pipeline_input
    if not target.is_file():
        raise AdapterError(
            f"抽取结果汇总文件不存在：{target}\n"
            "    先跑 `python -m finai_web build`（或 `finai ingest run`）生成它。"
        )
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AdapterError(f"{target} 的内容不是一个 JSON 对象")
    return data


PEERS_HEADER = (
    "# FinAI 可比公司财务数据（由 finai_web 从年报抽取结果生成）\n"
    "#\n"
    "# 本文件是生成物，不要手改 —— 手改的内容下次 `finai ingest build` 会被覆盖。\n"
    "# 生成命令：python -m finai_web build\n"
    "# 口径与 data/peers.csv 一致：金额单位元，期间见 as_of 列。\n"
    "# 每家的数值出处（页码、原文、是否倒挤）见 data/extracted/pipeline_input.json 的 provenance。\n"
    "#\n"
)


def write_peers_csv(rows: Iterable[Mapping[str, Any]], path: Path | None = None) -> Path:
    """写出与 `data/peers.csv` 同构的文件。

    默认落在 `data/extracted/peers.csv`，**不碰** `data/peers.csv` ——
    后者是仓库里被 git 跟踪的手填数据。要让它也吃抽取结果，
    由人显式复制过去（或 `finai ingest build --sync-peers`），
    这样「手填 → 抽取」的切换永远是一个看得见的动作。
    """
    target = path or PATHS.peers_extracted
    fields = ["code", *schema.PEER_FIELDS, "as_of"]
    lines = [PEERS_HEADER, ",".join(fields) + "\n"]
    for row in rows:
        values: list[str] = []
        for name in fields:
            value = row.get(name, "")
            if isinstance(value, float):
                values.append(f"{value:.2f}")
            else:
                values.append(str(value))
        lines.append(",".join(values) + "\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(lines), encoding="utf-8")
    return target


def placeholders_from(payload: Mapping[str, Any]) -> dict[str, float]:
    """把流水线输入摊平成 `{占位符名: 数值}`，供 render_placeholders 使用。"""
    mapping: dict[str, float] = {}
    opening = payload.get("opening_balance") or {}
    for name, value in opening.items():
        mapping[str(name)] = float(value)
    if "cash" in mapping:
        # 报告模板里常用 [opening_balance] 指「基期货币资金/期初现金」
        mapping["opening_balance"] = mapping["cash"]
    target = payload.get("target_actuals") or {}
    for name, value in target.items():
        if name in ("code", "as_of"):
            continue
        mapping[str(name)] = float(value)
    mapping["book_equity_attributable"] = float(target.get("book_equity_attributable", 0.0) or 0.0)
    return mapping


def history_from(record: Mapping[str, Any]) -> dict[str, Any]:
    """把多年序列整理成「历史增速」——流水线里 drivers.revenue_growth 的现成来源。

    流水线原先要求手填逐年收入增速。有了多年列示，这件事可以直接从年报算出来：

        {"years": [2023, 2024, 2025],
         "revenue": {"2023": ..., "2024": ..., "2025": ...},
         "revenue_growth": {"2024": 0.0202, "2025": 0.0643}}

    这里只做**算术**，不做判断：历史增速能不能直接当预测增速用，是另一回事
    （要结合行业、管理层指引），报告里要说明。缺失年份不猜、不插值。
    """
    series = record.get("series") or {}
    revenue = {
        int(year): float(item["value_yuan"])
        for year, item in (series.get("revenue") or {}).items()
    }
    growth: dict[int, float] = {}
    years = sorted(revenue)
    for previous, current in zip(years, years[1:]):
        base = revenue[previous]
        if base:
            growth[current] = revenue[current] / base - 1.0
    return {
        "years": years,
        "revenue": {str(year): revenue[year] for year in years},
        "revenue_growth": {str(year): round(value, 6) for year, value in growth.items()},
        "unit": "金额单位与年报一致（见记录里的 currency）；增速是小数",
        "note": "历史增速由年报多年列示算出，供 [drivers].revenue_growth 参考；"
        "是否直接用作预测增速需要另行判断",
    }


def render_placeholders(
    text: str, mapping: Mapping[str, float], *, keep_unknown: bool = True
) -> tuple[str, list[str]]:
    """替换模板里的 `[key]`，返回 (替换后的文本, 没命中的键)。

    没命中的键默认原样保留（不换成空字符串）：模板里留下 `[xxx]` 是一眼能看见的
    缺口，换成空字符串则会变成「读起来通顺但没有数」的句子，没人会注意。
    """
    unmatched: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in mapping:
            return f"{mapping[key]:,.2f}"
        unmatched.append(key)
        return match.group(0) if keep_unknown else ""

    return _PLACEHOLDER.sub(replace, text), unmatched


def compare_with_toml(
    opening: Mapping[str, float], toml_path: Path | None = None
) -> list[str]:
    """把抽取出来的基期数据与手填的 TOML 对一遍，返回差异说明。

    这是给人用的核对工具：切换数据源之前先看看差多少。
    差得离谱（例如差一个数量级）通常是**抽取抽错了**，不是手填错了。
    """
    try:
        from pipeline.config import load_opening_balance
    except Exception as exc:  # pragma: no cover - 取决于运行位置
        return [f"读不到 pipeline.config，跳过比对：{exc}"]

    declared = load_opening_balance(toml_path) if toml_path else load_opening_balance()
    differences: list[str] = []
    for name in schema.OPENING_FIELDS:
        mine = float(opening.get(name, 0.0))
        theirs = float(getattr(declared, name, 0.0))
        delta = mine - theirs
        if abs(delta) > 1.0:
            relative = abs(delta) / abs(theirs) if theirs else float("inf")
            differences.append(
                f"{name}：抽取 {mine:,.2f}，手填 {theirs:,.2f}，差 {delta:+,.2f}"
                f"（相对 {relative:.1%}）"
            )
    return differences
