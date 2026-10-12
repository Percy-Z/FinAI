"""离线演示：不联网、不装 pdfplumber，也能验一遍「抽取结果 → 流水线」。

为什么需要它
    真正的年报要联网下载、要 pdfplumber 解析。评审现场、或者刚克隆下来还没配
    环境的机器上，那两样都可能没有 —— 但「抽取结果能不能接进流水线」这件事
    与网络无关，不该被网络挡住验证。

    所以这个脚本直接给出**人造的年报页文本**（数字与仓库里 data/ 的示例数据一致），
    走同一套 `scan_pages → build_record → build_pipeline_input` 代码路径，
    产出 data/extracted/pipeline_input.json。然后用它跑一遍流水线：

        python finai_web/examples/offline_demo.py
        $env:FINAI_DATA_SOURCE="extracted"    # PowerShell
        python -m pipeline.run

局限（必须说清，免得被当成真数据）
    这里验的是**取数层与流水线之间的接法**：科目映射、倒挤规则、配平、
    数据源切换、可追溯字段。它**不验** PDF 解析质量 —— 那是真年报才能验的事，
    用 `python -m finai_web extract` 跑真 PDF。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from finai_web import adapter, extract  # noqa: E402  （要先把仓库根加进 sys.path）
from finai_web.config import PATHS  # noqa: E402
from pipeline.console import safe_console  # noqa: E402

# 目标公司 T：基期数字与 data/assumptions.toml 的 [opening_balance] 一致，
# 这样「抽取跑出来的数」和「手填的数」可以直接对账。
T_PAGES = [
    """目标公司 2024 年年度报告
合并资产负债表
单位：元
货币资金 55
应收账款 60
存货 40
固定资产 200
资产总计 385
应付账款 40
短期借款 100
负债合计 165
归属于母公司所有者权益合计 220
少数股东权益 0
所有者权益合计 220
股本 100
""",
    """合并利润表
单位：元
营业总收入 1,000
营业成本 600
利润总额 200
所得税费用 59
归属于母公司股东的净利润 141
利息费用 0

合并现金流量表（补充资料）
固定资产折旧、油气资产折耗、生产性生物资产折旧 50
无形资产摊销 0
长期待摊费用摊销 0
""",
]

# 可比公司 A / B / C：指标与 data/peers.csv 里的示例一致
# （营收 / 归母净利润 / 归母净资产 / 利润总额 / 所得税 / 折旧摊销）
PEERS: dict[str, dict[str, float]] = {
    "A": {"revenue": 110, "net": 14.85, "equity": 53, "pretax": 19.8, "tax": 4.95, "da": 5.5},
    "B": {"revenue": 150, "net": 18.75, "equity": 67, "pretax": 24.0, "tax": 5.25, "da": 7.5},
    "C": {"revenue": 120, "net": 15.6, "equity": 55.7, "pretax": 21.0, "tax": 5.4, "da": 5.4},
}


def peer_pages(code: str, values: dict[str, float]) -> list[str]:
    return [
        f"""可比公司 {code} 2024 年年度报告
合并资产负债表
单位：元
归属于母公司所有者权益合计 {values["equity"]}
""",
        f"""可比公司 {code} 2024 年年度报告
合并利润表
单位：元
营业总收入 {values["revenue"]}
利润总额 {values["pretax"]}
所得税费用 {values["tax"]}
归属于母公司股东的净利润 {values["net"]}
利息费用 0
""",
        """合并现金流量表（补充资料）
单位：元
固定资产折旧、油气资产折耗、生产性生物资产折旧 {da}
""".format(da=values["da"]),
    ]


def main() -> int:
    safe_console()
    PATHS.ensure()
    print("离线演示：人造年报页文本 → 抽取结果 → 流水线输入")
    print(f"  产出目录  {PATHS.extracted}")

    records = [
        extract.build_record(
            code="T", name="目标公司", year=2024, pages=T_PAGES, method="synthetic-demo"
        )
    ]
    for code, values in PEERS.items():
        records.append(
            extract.build_record(
                code=code,
                name=f"可比{code}",
                year=2024,
                pages=peer_pages(code, values),
                method="synthetic-demo",
            )
        )
    for record in records:
        record["notes"].insert(
            0,
            "⚠️ 人造数据（offline_demo.py）：只有科目映射与倒挤规则是真的，数字是编的。"
            "真数据请用 `python -m finai_web fetch` + `extract` 生成，会覆盖同名文件。",
        )
        path = extract.write_record(record)
        print(f"  ✓ {record['code']}  {len(record['accounts'])} 个科目 → {path.name}")

    payload = adapter.build_pipeline_input(records, target_code="T")
    written = adapter.write_pipeline_input(payload)
    peers_written = adapter.write_peers_csv(payload["peers"].values())

    opening = payload["opening_balance"]
    assets = sum(opening[name] for name in (
        "cash", "accounts_receivable", "inventory", "fixed_assets", "other_non_current_assets"
    ))
    liabilities = sum(opening[name] for name in (
        "accounts_payable", "interest_bearing_debt", "other_non_current_liabilities"
    ))
    equity = sum(opening[name] for name in ("share_capital", "retained_earnings", "minority_equity"))
    print(f"\n基期资产负债表：资产 {assets:,.2f} = 负债 {liabilities:,.2f} + 权益 {equity:,.2f}"
          f"（差额 {assets - liabilities - equity:,.2f}）")

    differences = adapter.compare_with_toml(opening)
    if differences:
        print("与 data/assumptions.toml 的 [opening_balance] 有差异：")
        for line in differences:
            print(f"  {line}")
    else:
        print("与 data/assumptions.toml 的 [opening_balance] 完全一致（说明映射与倒挤都对上了）")

    print(f"\n已写出 {written}")
    print(f"已写出 {peers_written}")
    print("\n下一步：")
    print('  $env:FINAI_DATA_SOURCE="extracted"   # PowerShell（bash 用 export）')
    print("  python -m pipeline.run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
