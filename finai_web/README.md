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

## 从公司名开始（工作台自动取数走的就是这条链路）

```
名称 → 代码/组织号 → 年报年份 → 下载 → 抽取 → 汇总 → 取价 → 假设草稿 → 人复核 → 估值
```

```powershell
# 老数据集收进公司目录（先打印清单，--yes 才真的移）
python -m finai_web company --migrate 000661 --yes

# 一家新公司：定标 + 下载 + 抽取 + 汇总 + 取价 + 假设草稿
python -m pipeline.job plan --company-dir data/companies/600519 --target-code 600519 `
        --target 贵州茅台 --peer 000858 --peer 000568

# 复核 assumptions.draft.json（填上标「需人工填」的），再跑估值
python -m pipeline.job run --company-dir data/companies/600519
```

新增的四个模块：

| 模块 | 干什么 | 关键约定 |
| --- | --- | --- |
| `resolve.py` | 按名称检索公司（巨潮 `topSearch/query`） | 中文名、简称、代码都能查；**板块按代码前缀判**，不看接口的 `type`（A 股的 type 一律是 `shj`，区分不出沪深）；匹配到多家就返回全部候选，不替人挑 |
| `quotes.py` | 股价与 beta | 股价走腾讯 + 东财**双源交叉核对**；beta 用 60 个月月线对沪深300 回归 |
| `company.py` | `company.json`（谁、什么时候、从哪个关键词选的）→ 生成 `targets.csv` | `company.json` 是唯一出处，`targets.csv` 是生成物 |
| `pipeline/assumptions_gen.py` | 假设草稿 | 四态：`derived` 自动推导 / `default` 规则默认 / `kept` 沿用上一版 / `missing` 需人工填；覆盖文件前备份，人写的 `[evidence]` 条目原样保留 |

**公司目录**（环境变量 `FINAI_COMPANY_DIR`）：

```
data/companies/<代码>/
    company.json  targets.csv  assumptions.toml  assumptions.draft.json  market.csv
    raw_pdf/  extracted/  manifest.json  cache/
```

不设这个环境变量时，行为与过去**完全一致**（仍读 `data/` 那套平铺布局）——
仓库里的单元测试因此一条都不用改。

取价的两条规矩，别混：

- 两个源**都有但数值打架** → 直接报错，把两个值都摆出来，绝不静默取其一；
- 备源**根本取不到**（限流、断网）→ 降级为单源，但在 `market.csv` 表头与假设草稿里
  明确写「这个价没有交叉核对」。2026-10-14 实测东财对连续请求会直接断连接，
  不降级的话「输入公司名出报告」这件事就没法用了。

## 真实年报的三个坑（2026-10-15 补）

这三条都是真跑新公司时踩出来的，都写进了测试：

1. **附注列**：报表在科目名与金额之间单列一个附注号，有的写「七、45」，有的**只写「45」**。
   只剥前者会把 45 当成金额 —— 贵州茅台 11 个科目里 8 个中招（货币资金抽成 1、固定资产抽成 19）。
   现在两种都剥，而且只在小整数后面**还跟着一个更像金额的数**时才剥：
   否则「股本 1000 1200」里的真金额会被误剥成上一年的数。
2. **附注目录行**：「所得税费用(1). 所得税费用表」是附注目录里的一行，那个 `(1)` 会被
   当成负一（会计上括号表示负数）。判据是「数很小、且后面跟着中文」——所以
   「存货 58,334,693.18 元」这种带单位的真金额不会被误伤。
3. **同一科目在不同表里的写法不同**：合并资产负债表写「归属于母公司所有者权益」，
   摘要表写「归属于上市公司股东的净资产」。摘要表在前，先匹配到它就会抽错
   （实测抽到 268 亿，真实值 2446 亿）。现在只认资产负债表那几种写法，
   认不出就留空报「需人工填」。

## 自测

```powershell
python -m unittest discover -s finai_web/tests   # 28 个测试，不联网
python -m finai_web check                        # 环境与文件自检
```
