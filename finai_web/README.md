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

### 多年列示（2025-10-14 加）

报表有几年就取几年 —— Apple 的 10-K 列 3 年、华为年报列 2 年，全都取出来，
放在记录的 `years` 与 `series` 里：

```powershell
python -m finai_web series                 # 看多年序列（默认 6 个关键科目，--all 看全部）
python -m finai_web series --code AAPL
python -m finai_web audit                  # 体检：单列/多年/出处是否自相矛盾、基期是否配平
```

换一份新年报时先跑 `audit`：抽取「跑通」不等于「数对」，它专门抓「跑通但数错」。

- **怎么做到**（见 `columns.py`）：用 pdfplumber 的词坐标把金额词按右边界聚成「列」，
  再把表头的年份词按 x 位置就近配对到列上，最后把每个科目标签右侧的金额按 x 归属到年份。
- **只认报表正文页**：五年摘要页、管理层讨论页、附注页都带年份列，但口径不同 ——
  Apple 现金流量表里的「Accounts payable (1,889)」是**变动额**，不是余额。
  所以资产负债表科目只从标着资产负债表标题的页取数，收入类只从利润表页取数。
- **相加规则**：拆成两段列示的科目（借款、租赁负债、Term debt）在同页同年相加；
  其它科目不加 —— 同一个数在一页出现两次是「另一种写法」，加了会翻倍。
- **历史增速**：`build` 会把多年收入序列与逐年增速写进 `payload.history`，
  可直接参考填 `[drivers].revenue_growth`（历史归历史，能不能当预测用仍需判断）。
- **限制**：词坐标只有 pdfplumber 后端提供；扫描件、或把报表做成图片的 PDF 取不到，
  这时 `series` 为空、`years` 为 `[]`，单列抽取（`accounts`）仍然照常工作。

### 两条防「静默错数」的规则（2025-10-14 加，均来自真实年报）

- **标签匹配不是朴素子串**（`extract.label_position`）：标签**前面还连着汉字/字母/数字**时不算命中 ——
  「长短期借款」不是「短期借款」、「总股本」不是「股本」、「借款利息费用」不是利润表的「利息费用」。
  标签右边接着字是允许的（「应收账款及应收票据」要能命中「应收账款」）。
  完整的 IFRS 标题要显式写进科目表（例如「财务收入及财务费用」）。
- **报表科目只从报表页取**：文档里只要有资产负债表页，资产负债表科目就只从那些页取数。
  管理层讨论、五年摘要、附注里也有类似的表，但口径不同 —— 华为第 65/66 页的债务结构分析
  写着「长短期借款 239,284」「其中：长期借款 219,861」，拿它当 `short_term_debt` /
  `long_term_debt` 会给出**看着合理但其实错**的数。

### 港交所 / 繁体口径（2025-10-14 加，腾讯 2025 年报实测）

港交所年报（腾讯这类）与内地、美国的写法都不同，这几条是跑完腾讯年报后补的：

- **繁体自动转简体**：`資產總額`→`资产总额`、`應收賬款`→`应收账款`、`經營盈利`→`经营盈利`。
  不转的话是**静默匹配不上**（科目表只维护简体写法）。
- **中文数字年份与日期**：`二零二五年` → 2025，`於二零二五年十二月三十一日` → 2025-12-31。
- **港交所科目名**：资产总额 / 负债总额 / 权益总额、经营盈利、除税前盈利、年度盈利、
  财务成本、本公司权益持有人应占权益、物业、设备及器材。
- **财务概要页不参与取数**：`財務概要 / 簡明綜合全面收益表` 这类页也带年份列，
  但列的是五年、数字口径不同（腾讯第 4 页收入 560,118 vs 本期 751,766）。
- **科目名与合计分行**：腾讯的 `收入` 单独一行、合计在下方（`6 751,766 660,257`），
  这时往下找「整行都是数字」的那一行。
- **同一张报表跨页相加**：腾讯的借款分两页（非流动 208,369 + 流动 42,618 = 250,987），
  只要两张纸属于同一张报表（同报表类型 + 同合并口径）就相加。
- **破折号行不取数**：`股本 33 – –` 里的 33 是附注编号，正文用「–」表示未列示；
  这种行不算有金额，也不会往下找合计。
- **完整标题**：`股本` 不会被 `股本溢价` 命中（`schema.EXACT_LABELS`）。
- **附注可取数的科目**：折旧摊销、利息费用常常只在附注里分项列示
  （`allow_notes=True`），允许从没有报表标题的页取；其它科目仍然只认报表页。

### 文字层不可用的 PDF（2025-10-14 加，小米 2025 年报实测）

有些 PDF 的字体**没有 ToUnicode 映射表**，抽出来的不是汉字而是 `(cid:14062)(cid:11830)…`
这样的字符编号（小米 2025 年报 415 页全部如此）。这不是「扫描件」，换 pypdf / PyMuPDF
也同样读不出来 —— 本地解析在这类文件上没有任何办法。

代码会自己认出来（`extract.cid_ratio`），并做三件事：

1. 记录里写 `text_layer_usable: false`，附一条说明，讲清「为什么」和「两条出路」；
2. `build` 直接拒绝这种记录（`AdapterError`），不会拿零星几个科目硬算估值；
3. `python -m finai_web audit` 也会把它标出来。

出路：① 换一份带文字层的 PDF（英文版通常正常；或在阅读器里「打印成 PDF」重新导出，
那会重建字体）；② 先 OCR 再抽；③ 用 Firecrawl 之类的云端解析 ——
`extract.record_from_firecrawl()` 可以直接把它的 JSON 输出变成同样的记录。

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
