# finai_web · 网上取数层（定标公司 → 年报 → 接进流水线）

这一层把「**定标公司 → 下年报 PDF → 抽出结构化字段 → 替换流水线的 `[opening_balance]`
与 `data/peers.csv` 读取**」这条链路打通，并且只把**抽取结果**当成数据源 ——
PDF、抽取 JSON、Wind/Choice 导出的行情表全部落在 `data/` 下，不进 git。

```
FinAI/
├── config/
│   ├── targets.example.csv     定标公司清单模板
│   └── targets.csv             你自己的清单（本地，不提交）
├── data/                       ← 全部产出都在这里（.gitignore 已忽略）
│   ├── raw_pdf/<代码>_<年份>.pdf
│   ├── extracted/<代码>_<年份>.json     单家单年的抽取结果（带页码与原文）
│   ├── extracted/pipeline_input.json    汇总：opening_balance + peers + 出处
│   ├── extracted/peers.csv              与 data/peers.csv 同构的生成版
│   ├── manifest.json                    下载清单（来源 URL + sha256）
│   └── market.csv                       你从 Wind/Choice 导出的行情表
└── finai_web/                  ← 本模块
    ├── config.py      路径与数据源开关
    ├── http.py        标准库 HTTP（重试、UA、证书）
    ├── targets.py     定标公司清单
    ├── cninfo.py      巨潮资讯网检索 → 年报 PDF 直链
    ├── firecrawl.py   Firecrawl 客户端（找 PDF / PDF 解析 / 结构化抽取，兜底）
    ├── downloader.py  下载编排 + manifest
    ├── schema.py      科目表与「年报科目 → 流水线字段」的映射、倒挤规则
    ├── extract.py     扫描页文本 → 抽取记录（含 Firecrawl 兜底与事后交叉验证）
    ├── adapter.py     抽取结果 → 流水线输入 / peers.csv / 占位符替换
    ├── market.py      行情表模板与校验
    ├── cli.py         python -m finai_web <子命令>
    └── tests/         纯标准库自测（不联网）
```

## 快速开始

```powershell
cd FinAI
copy config\targets.example.csv config\targets.csv   # 填你要估的公司
# 可选：在仓库根目录的 .env 里填 FIRECRAWL_API_KEY=...（只有兜底路径用得上）

python -m finai_web check                # 自检：依赖、密钥、清单、行情表
python -m finai_web fetch                # 下年报 PDF → data/raw_pdf/
python -m finai_web extract              # 抽 PDF → data/extracted/*.json
python -m finai_web build                # 汇总 → pipeline_input.json（+ peers.csv）
python -m finai_web market --template    # 生成行情表模板，等你从终端填数
python -m finai_web market --check       # 校验你导出的行情表

# 用抽取结果跑流水线（关键的一步）
$env:FINAI_DATA_SOURCE="extracted"       # bash: export FINAI_DATA_SOURCE=extracted
python -m pipeline.run
```

装过包之后（`pip install -e .`）也可以直接用 `finai ingest`：

```powershell
finai ingest check
finai ingest run            # fetch + extract + build
finai run --source extracted
```

**不联网也想验一遍**（人造年报文本，走同一套代码路径）：

```powershell
python finai_web\examples\offline_demo.py
$env:FINAI_DATA_SOURCE="extracted"; python -m pipeline.run
```

## 三个接入点

### 1. `[opening_balance]`：从手填改成年报抽取

`pipeline.config.load_opening_balance(source="extracted")` 读
`data/extracted/pipeline_input.json` 的 `opening_balance`，
构造出来的仍然是 `BalanceSheetState`（同样 11 个科目、同样做配平检查），
所以三表预测、勾稽校验、估值下游**一行都不用改**。

数据源用环境变量切换，默认仍是手填：

| `FINAI_DATA_SOURCE` | `[opening_balance]` 来自 | peers 来自 |
| --- | --- | --- |
| 不设 / `config` | `data/assumptions.toml` | `data/peers.csv` |
| `extracted` | `data/extracted/pipeline_input.json` | 同上文件（生成版 peers） |

默认必须是 `config`：仓库里的单元测试断言的是手填数据的具体数值，
如果默认行为随「本地有没有抽取结果」自动变化，测试与复现就会时绿时红。
设成 `extracted` 而文件不存在时**直接报错，不回退** —— 悄悄拿手填数据算一遍
是本层最危险的失败模式。

### 2. `peers.csv` 的读取

抽取结果能直接生成与 `data/peers.csv` **同构**的表（表头一致、`#` 注释约定一致），
`pipeline.config.load_peer_financials(path)` 读回来就是同一批 `PeerFinancials`。
生成的文件默认写在 `data/extracted/peers.csv`，**不覆盖**仓库里被 git 跟踪的
`data/peers.csv`；想覆盖要显式加 `--sync-peers`。

### 3. `market.csv`：只能由你在终端导出

股价、摊薄股数、有息负债这些是 Wind/Choice 的终端数据，程序不生成、
也不进仓库。本层提供 `--template`（按定标清单预填代码与公司名）与
`--check`（表头、role、基准日、单位可解析性、与清单的对应关系）。

## 单位与口径

- 年报常见的「单位：千元 / 万元」在抽取时统一换算成**元**（`extract.detect_scale`）。
- **英文年报也认**：美国 10-K 的科目名（`Total net sales` / `Total assets` /
  `Term debt` …）、`In millions` 这类单位写法、以及弯引号（`shareholders’ equity`）
  都在科目表与归一化里覆盖了。报表页按 `CONSOLIDATED BALANCE SHEETS` /
  `CONSOLIDATED STATEMENTS OF OPERATIONS` 这类标题识别。
- **币种必须显式换算**：流水线以人民币元为单位。若年报是别的币种（例如 Apple 的
  美元百万），`build` 必须给汇率，否则直接报错 —— 把「美元百万」当「人民币元」
  算进估值是最危险的静默错误：
  ```powershell
  python -m finai_web build --target AAPL --fx 7.15   # 1 USD = 7.15 CNY
  ```
  换算是线性的，不含汇兑损益的会计处理；汇率由使用者提供，本模块不联网取汇率。
  同一批记录里混用多种币种会被拒绝（按币种分批 build，或用 `FINAI_EXTRACTED_DIR`
  指到不同目录）。
- **期间口径写到日**：美国财年不是自然年，记录里会带上确切期末
  （Apple FY2025 → `2025-09-27`），`peers.csv` 的 `as_of` 用它而不是「2025A」。
- 流水线要 11 个资产负债表科目，年报上没有正好这 11 个标题，所以：

  | 流水线字段 | 来源 |
  | --- | --- |
  | cash / accounts_receivable / inventory / fixed_assets / accounts_payable / share_capital | 直接读年报 |
  | other_non_current_assets | 倒挤：资产总计 − 其余资产项 |
  | interest_bearing_debt | 倒挤：短期借款 + 一年内到期非流动负债 + 长期借款 + 应付债券 |
  | other_non_current_liabilities | 倒挤：负债合计 − 应付账款 − 有息负债 |
  | retained_earnings | 倒挤：归母权益 − 股本 |
  | minority_equity | 读年报，没有就按 0 并留提示 |

  每个字段在 `pipeline_input.json` 的 `provenance` 里都标了 `read` 还是 `derived(...)`，
  报告里也带页码与原文片段 —— 评审问「这个数哪来的」答得上。
- 可比公司的 EBIT / EBITDA 是推导出来的：
  `EBIT = 利润总额 + 利息费用`，`EBITDA = EBIT + 折旧摊销`；
  利息费用缺失时退到财务费用、利润总额缺失时退到「净利润 + 所得税」，
  每次退化都会在记录的 `notes` 里写清，不静默。
- 相对估值口径：用抽取结果时，**目标公司与可比公司的利润指标都取最近一个完整年度的实际数**
  （`pipeline.adapters.apply_actuals`），三表预测的前瞻口径只用于 DCF。
  报告第 9 节会按数据源自动改写这段说明。

## 已知边界

- **网络**：巨潮接口与 Firecrawl 都需要联网。没有网络时 `fetch` 会明确失败并给退出码 1，
  不会静默产出一份空结果；`extract` 本地可用（需要 `pdfplumber`/`pypdf`/`PyMuPDF` 之一）。
- **扫描件**：没有文字层的 PDF 抽不出数，记录里会写明「疑似扫描件，需要 OCR」，
  不猜数字。
- **Firecrawl 是兜底**：只在「PDF 抓不到」或「本地抽不出」时用，
  记录里 `method=firecrawl-*` 可区分；模型抽的数一律不给高置信度，标为「需与原文核对」。
- **同名不同年**：PDF 文件名带年份（`600519_2024.pdf`），
  但年报通常在**次年**披露，`years` 要写披露年份所在的区间。

## 自测

```powershell
python -m unittest discover -s finai_web/tests   # 28 个测试，不联网
python -m finai_web check                        # 环境与文件自检
```
