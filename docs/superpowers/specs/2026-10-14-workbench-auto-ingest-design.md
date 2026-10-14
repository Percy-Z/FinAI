# 工作台自动取数：输入公司名 → 出估值报告（设计）

- 日期：2026-10-14
- 状态：设计已确认，待写实施计划
- 涉及目录：`finai_web/`、`pipeline/`、`web/js/`、`data/`、`.gitignore`
- 相关文档：`finai_web/README.md`（取数层现状）、`data/real-000661/README.md`（真实数据集口径）、`docs/reproduce.md`

---

## 一、要解决的问题

整条链路其实已经通了，问题出在**入口分裂**：

1. 取数层（`finai_web`）能自动跑，但起点是人先手填六位代码与巨潮组织号
   （`config/targets.csv` 的注释里写着怎么查 —— 那一步是人在浏览器里手工完成的）。
2. 工作台的取数页是**刻意只读**的（`GET /api/ingest` 摊开现状，`POST` 返回 404），
   页面文案明确写着「界面不触发任何网络请求」。当时的理由是现场演示点一下等两分钟是风险。
3. 换一家公司要设四个环境变量再重启服务，且所有取数产物平铺在 `data/raw_pdf`、`data/extracted`，
   只有一套位置 —— 2026-10-12 踩过一次：演示公司把长春高新的 `pipeline_input.json` 覆盖掉了。

目标：**在工作台输入公司名称，程序自动完成 解析 → 下载年报 → 抽取 → 组装 → 确认假设 → 估值 → 出报告。**

---

## 二、目标与成功标准

### 目标

1. 输入中文公司名（支持模糊），从候选里点选目标公司与 2–4 家可比公司。
2. 自动确定最新年报年份，并下载**最近两份**年报（当年那份给基期资产负债表，上一份给利润表）。
3. 自动生成行情表与假设草稿，在确认屏上逐项标出「自动推导 / 规则默认 / 你来定」与出处。
4. 点「确认并估值」后一次跑完，直接看到报告。
5. 每家公司一个目录，互不覆盖。

### 成功标准（可验收）

- 换一家**从未处理过的公司**（不是长春高新）能完整跑出报告。
- 长春高新重跑，结论与今天一致（每股 76.30 元、WACC 8.775%）。
- 已经取过数的公司，**断网也能复跑**到报告（命中本地年报与抽取记录就不联网）。
- 每个数字可追溯：抽取记录带页码与原文，行情带来源与查询时间，假设带算法或规则说明。
- 现有 563 个测试保持全绿（三个夹具路径不能变）。

### 非目标（本次不做）

- **自动挑可比公司**：巨潮不提供行业分类，要做得再接一个行业数据源，而且评审一定会问
  「凭什么这三家可比」，自动选的依据反而更难辩护。可比公司在界面上由人勾选。
- **港股 / 北交所的完整估值**：解析层能识别并给出候选，但估值口径（汇率、板块差异）未验证，
  本次只保证「识别得出、候选显示得出」。
- **OCR**：没有文字层的 PDF 明确报错，不猜数字。
- **Wind / Choice 自动取数**：终端数据无法自动化，行情走公开源。
- **把配置层从「模块级环境变量」改成「显式传参」**（见第四节被否方案）。

---

## 三、现状（2026-10-14 实测）

### 已经有的（可以直接复用）

| 能力 | 位置 | 说明 |
| --- | --- | --- |
| 年报检索 → PDF 直链 | `finai_web/cninfo.py` | `query()` / `pick_annual_report()` 已按标题打分挑正文，排除摘要、英文版、更正稿 |
| 下载编排 + 下载清单 | `finai_web/downloader.py` | `fetch_all()` / `save_manifest()`，清单记 URL 与 sha256 |
| PDF → 结构化科目 | `finai_web/extract.py` | `extract_pdf()` / `write_record()`，每个科目带页码与原文 |
| 抽取结果 → 流水线输入 | `finai_web/adapter.py` | `build_pipeline_input()` / `write_pipeline_input()` / `write_peers_csv()` |
| 标准库 HTTP（重试、UA、证书） | `finai_web/http.py` | `get_json()` / `post_form()` / `download()` |
| 工作台九阶段进度 | `pipeline/web.py` | 直接读 `logs/<run_id>/trace.jsonl`，**前端不硬编码阶段名**（`web/js/pages.js` 按 step 字段渲染） |
| 每公司路径开关 | 两个 `config.py` | `FINAI_ASSUMPTIONS` / `FINAI_MARKET_CSV` / `FINAI_PEERS_CSV` / `FINAI_DATA_SOURCE` / `FINAI_TARGET_ACTUALS` |

### 缺的三块

| 缺口 | 现状 | 本次怎么补 |
| --- | --- | --- |
| 公司名 → 代码 + 组织号 | 人手工查，填进 `config/targets.csv` | 新增 `finai_web/resolve.py`，走巨潮检索接口 |
| 股价 | `finai_web/market.py` 只做模板与校验，明确写着「Wind/Choice 导出，程序不生成」 | 新增 `finai_web/quotes.py`，公开行情源 + 双源交叉核对 |
| 假设文件 | 人填 `assumptions.toml`（含逐条依据台账） | 新增 `pipeline/assumptions_gen.py`，生成草稿 + 确认屏 + 覆盖前备份 |

### 本次探测的实测证据（只读联网）

这三条决定了整套设计能不能成立，都已在 2026-10-14 实测：

1. **按名称检索可用**：`POST https://www.cninfo.com.cn/new/information/topSearch/query`
   传 `keyWord=长春高新&maxNum=10` 返回
   `{"code":"000661","orgId":"gssz0000661","category":"A股","zwjc":"长春高新","delisted":"false"}`。
   模糊输入同样可用：传「长春」返回 3 家（长春高新、长春一东、长春燃气）；
   传「腾讯控股」返回 `{"code":"00700","category":"港股","type":"hke","orgId":"gshk0000700"}`。
2. **最新年报年份可自动确定**：`POST /new/hisAnnouncement/query` 把 `seDate` 留空，
   返回 `totalAnnouncement=55`、按时间倒序，第一条就是「2025年年度报告」（披露日 2026-04-22）。
   所以「最新一份年报是哪年」不需要人填披露年份。
3. **行情源可用**：腾讯历史行情接口取到 2026-09-30 收盘价，与东方财富接口交叉核对一致
   （长春高新 71.83 —— 这正是 `data/real-000661/market.csv` 里那个数）。
   东方财富接口出现过限流，所以必须主备双源，不能只依赖一个。

---

## 四、方案选择

### 方案一（采用）：服务器只当调度与渲染，重活放进子进程

工作台收到「确认并估值」后，用一份**带该公司目录环境变量的子进程**去跑「取数 → 估值」。

为什么这么选，而不是在服务器进程里跑：

- `pipeline/config.py` 的路径是**导入时**读环境变量的
  （`DEFAULT_CONFIG_PATH = _env_path("FINAI_ASSUMPTIONS", ...)`），而工作台的
  `/api/run` 是在**同一进程里起线程**跑的（`pipeline/web.py:715` 的 `_start_run`）。
  同进程跑第二家公司，必须改掉那批模块级默认值 —— 那是被 561 个测试盯着的代码。
- 子进程天然隔离，且**进度机制一行都不用改**：子进程照旧写
  `logs/<运行号>/trace.jsonl`，界面现在就是这么读的（`pipeline/web.py:84` 的 `_read_trace`）。
- 取数与估值两段各自独立失败、各自有日志与超时，演示时一段挂了另一段还在。

改动面落在四个新模块 + 取数页；`algorithm/` 完全不动，`pipeline/` 只加新文件与薄改动。

### 方案二（被否）：把配置改成显式传参，服务器进程内跑多家

长期更干净，但要动 `pipeline/config.py` 的模块级默认值，561 个测试里有一批依赖它，
风险与工期都大得多，收益只是省一个子进程。

### 方案三（被否）：工作台只做输入框，取数仍要人去命令行跑

最省事，但和「输入名字就出估值」的目标对不上。

---

## 五、目录与数据布局

### 每家公司一个目录

```
data/companies/<代码>/
    company.json              目标公司 + 可比公司（名称、代码、组织号、年报年份、来源）
    targets.csv               由 company.json 生成的取数清单（生成物，不手改）
    assumptions.toml          假设（自动推导 + 人确认过的值；覆盖前自动备份）
    assumptions.draft.json    假设草稿（机器可读：值 + 来源 + 状态），界面读它
    market.csv                行情表（股价走公开行情源，报表侧字段取自年报）
    raw_pdf/<代码>_<年份>.pdf  年报原文
    extracted/<代码>_<年份>.json
    extracted/pipeline_input.json
    extracted/peers.csv
    manifest.json             下载清单（URL + sha256）
    cache/                    检索缓存
```

**向后兼容是这个设计的硬约束**：未设置 `FINAI_COMPANY_DIR` 时，两个 `config.py` 的行为
必须与今天**完全一致**（`ROOT/data` 那套平铺布局），老测试一条都不用改。

```python
# finai_web/config.py 的改动形状
_company = os.environ.get("FINAI_COMPANY_DIR", "").strip()
BASE = Path(_company) if _company else ROOT / "data"
data      = _env_path("FINAI_DATA_DIR", BASE)
raw_pdf   = _env_path("FINAI_RAW_PDF_DIR", data / "raw_pdf")
extracted = _env_path("FINAI_EXTRACTED_DIR", data / "extracted")
manifest_json = data / "manifest.json"      # 现在是硬编码 ROOT/data/manifest.json，要让位
cache         = data / "cache"              # 同上
peers_extracted = extracted / "peers.csv"   # 同上
```

`pipeline/config.py` 同理：设了 `FINAI_COMPANY_DIR` 且没有单独覆盖时，
`assumptions.toml` / `market.csv` / `peers.csv` 从公司目录取；单独覆盖（`FINAI_ASSUMPTIONS` 等）优先级更高。

### 工作台进程里的坑：路径要显式传，不能靠模块级 `PATHS`

`finai_web/config.py` 的 `PATHS` 是**模块级单例**，在服务器进程第一次导入时就定死了。
所以工作台的取数页**不能**指望 `PATHS` 跟着当前选中的公司变 —— 它是启动那一刻的形状。

规定：一切「读某家公司的文件」的调用都**显式传路径**（现有函数本来就是 `path=None` → 回落到 `PATHS.x`
的形状，传参即可，不用改函数签名）；`_ingest_payload()` 增加 `company_dir` 参数，
内部构造一份覆盖过的 `Paths`（`Paths` 是 frozen dataclass，可直接用显式字段实例化），
禁止在请求处理路径上读模块级 `PATHS`。子进程里因为启动前就设好了 `FINAI_COMPANY_DIR`，
模块级 `PATHS` 反而是对的 —— 两处的差别写进代码注释，免得后人踩回来。

### 迁移长春高新

提供一条命令而不是让人手工搬：

```
python -m finai_web company --migrate 000661
```

把 `data/real-000661/{assumptions.toml,market.csv,peers.csv}` 与平铺的
`data/raw_pdf/*.pdf`、`data/extracted/*`、`data/manifest.json` 迁进 `data/companies/000661/`。
**迁移只搬 000661 相关的文件**，`data/assumptions.toml`、`data/market.csv`、`data/peers.csv`
（单元测试夹具）原地不动。迁移前打印将要移动的清单并要求确认，不静默移动。

### 进不进仓库

跟 `data/real-000661/` 现在的口径保持一致：

| 文件 | 进仓库？ | 理由 |
| --- | --- | --- |
| `company.json`、`assumptions.toml`、`market.csv` | 进 | 小，是人写过 / 确认过的输入 |
| `raw_pdf/`、`extracted/`、`cache/`、`manifest.json` | 不进 | 体积大且可重建；`.gitignore` 加 `/data/companies/*/raw_pdf/` 等四条 |

---

## 六、组件设计

### 6.1 `finai_web/resolve.py` —— 公司名 → 候选公司

```
search(keyword, *, max_num=10, transport=None) -> list[Candidate]
```

- `Candidate`：`code`、`name`（巨潮的 `zwjc` 简称）、`market`、`org_id`、`delisted`、`cninfo_column`。
- 过滤掉 `delisted == "true"` 的；`category=港股` 或 `type=hke` → 港股。
- 巨潮板块映射按**代码前缀**判断（A 股）：`6` → `sse`、`0/3` → `szse`、`4/8` → `bj`；
  港股 → `hke`。北交所这一支本次没验到（那次探测 SSL 抖动），写测试时用假数据覆盖，
  第一次真跑到北交所时核对一次并在 README 记一笔。
- 网络调用走 `transport` 参数（`cninfo.py` 已定的约定），单测不联网。
- 一次名称匹配到多家时，返回全部候选交给界面点选，**不替用户挑**。

### 6.2 `finai_web/quotes.py` —— 股价

```
latest_close(code, *, as_of=None, transport=None) -> Quote
```

- 主源腾讯历史行情接口，备源东方财富；**两个源都对得上才用**，对不上就报错并把两个值摆出来。
- `Quote`：`code`、`price`、`as_of`（实际取到的交易日）、`source`、`cross_source`、`queried_at`。
- 写进 `market.csv` 的表头注释里，格式照抄 `data/real-000661/market.csv`
  （来源 + 查询日期 + 口径说明）。
- 报表侧的四个字段（`diluted_shares`、`interest_bearing_debt`、`cash`、`minority_interest`）
  **不从行情源取**，直接从抽取结果拿 —— 它们本来就在 `pipeline_input.json` 里。
- `preferred_stock` 与 `non_operating_assets` 年报没有可归入此类的披露时填 0，并在表头注释里写明这一点
  （`data/real-000661/market.csv` 就是这么做的，报告第 9 节也要说）。

### 6.3 `pipeline/assumptions_gen.py` —— 假设草稿

```
build_draft(company_dir: Path) -> dict     # 读 pipeline_input.json + market.csv + company.json
write_draft(draft, path)                   # 写 assumptions.draft.json
render_toml(draft, *, previous: Path|None) # 渲染 assumptions.toml，并做备份
```

草稿里每个字段是一条记录：`{value, status, source}`，`status ∈ {derived, default, human}`。
规则表见第八节。`render_toml` 的两条硬要求：

1. 目标文件已存在 → 先备份成 `assumptions.<时间戳>.bak`，再写新的。
2. 旧文件里人写的 `[evidence]` 条目**原样保留**，自动推导的条目追加在后面。
   （按节拼接，用测试钉住：给一份带 `[evidence]` 的文件，渲染后人的条目一字不少。）
   `tomllib` 是只读的，所以渲染走模板字符串，不指望 TOML 库回写。

### 6.4 `pipeline/job.py` —— 任务编排（子进程的入口）

```
python -m pipeline.job plan --company 000661        # 取数 + 生成草稿，不估值
python -m pipeline.job run  --company 000661        # 写 assumptions.toml + 跑流水线
```

- `plan`：解析候选 → 写 `company.json` 与生成的 `targets.csv` → `fetch_all()` →
  `extract.extract_pdf()` + `write_record()` → `adapter.build_pipeline_input()` + `write_pipeline_input()` →
  `quotes.latest_close()` → 写 `market.csv` → `assumptions_gen` 写草稿。
  调的是这些库函数，不是 `finai_web/cli.py` 里的 argparse 命令。
- `run`：渲染并落盘 `assumptions.toml` → 带环境变量调用 `pipeline.run.run_pipeline(run_id)`。
- 取数步骤也写进**同一份** `logs/<job_id>/trace.jsonl`，步骤名：
  `ingest_resolve`、`ingest_fetch`、`ingest_extract`、`ingest_build`、`ingest_market`、`ingest_assumptions`。
  前端不硬编码阶段名，所以这六个名字会直接出现在现有进度视图里，不需要改前端映射。
- 幂等：已下过的 PDF、已抽过的记录、已生成的 `pipeline_input.json` 默认复用（命中就不联网）。

### 6.5 工作台端点与前端

| 端点 | 方法 | 作用 |
| --- | --- | --- |
| `/api/resolve` | POST | `{keyword}` → 候选公司列表（联网，只读） |
| `/api/plan` | POST | `{target, peers[]}` → 起 `job plan` 子进程，立刻返回 job_id |
| `/api/plan/draft` | GET | 读当前公司的假设草稿（值 + 来源 + 状态） |
| `/api/ingest/run` | POST | `{company, assumptions}` → 起 `job run` 子进程 |
| `/api/ingest` | GET | 保持今天的只读语义不变（有测试盯着，不去改它的含义） |
| `/api/status` | GET | 复用；多一个 `job` 字段说明当前跑的是 plan 还是 run |

前端改动集中在 `web/js/pages2.js` 的取数页与 `web/js/api.js`；`RunState` 增加
`job_id` 与 `phase`（`plan` / `run`），`/api/status` 照旧渲染 trace。

---

## 七、接口契约

### `company.json`

```json
{
  "version": 1,
  "target": {
    "code": "000661", "name": "长春高新", "org_id": "gssz0000661",
    "market": "CN", "cninfo_column": "szse", "years": [2026, 2025],
    "resolved_from": "长春高新", "resolved_at": "2026-10-14T22:00:00+08:00"
  },
  "peers": [
    {"code": "300009", "name": "安科生物", "org_id": "9900008310", "market": "CN",
     "cninfo_column": "szse", "years": [2026]}
  ],
  "as_of": "2026-09-30"
}
```

`years` 写**披露年份**（2025 年年报在 2026 年披露，所以写 2026），沿用现有约定。
目标公司默认带两年，可比公司默认带最近一年（可比只用到利润类指标）。

### `targets.csv`（生成物）

列与 `finai_web/targets.py` 的 `COLUMNS` 完全一致，文件头写「本文件由 company.json 生成，不要手改」。
生成好后用现有的 `FINAI_TARGETS_CSV` 开关指过去 —— 取数层一行都不用改。

### `assumptions.draft.json`

```json
{
  "company": {"name": "...", "code": "000661", "source": "cninfo/2025年年度报告"},
  "normalized": {"applied": true, "year": 2024, "reason": "最近一年归母净利同比 -94%，低于阈值 -40%"},
  "fields": {
    "drivers.ar_pct_revenue": {"value": 0.1680739523, "status": "derived",
      "source": "应收账款 20.31 亿 ÷ 营业总收入 120.83 亿（年报第 81 页）"},
    "drivers.revenue_growth": {"value": [0.03, 0.03, 0.03, 0.03, 0.03], "status": "default",
      "source": "规则默认：取近三年 CAGR 与 3% 的较小值，逐年回落"}
  }
}
```

---

## 八、假设层规则

确认屏上每个值都带状态标签，三类：

| 状态 | 来源 | 具体字段 |
| --- | --- | --- |
| `derived` 自动推导 | 从抽取结果或行情算出来，附算法与出处 | 基期资产负债表 11 项、基期收入与年份、营运资本比例（应收/存货/应付 ÷ 收入）、债务成本（利息支出 ÷ 有息负债）、beta（60 个月月线对沪深300 回归）、股权与债务权重、分红比例、归一口径 |
| `default` 规则默认 | 保守规则，**标黄提示复核** | 收入增速（近三年 CAGR 与 3% 取小、逐年回落）、资本开支（维持性 = 折旧摊销率）、永续增长 2.5%、无风险利率与 ERP（查一次缓存，记查询日期）、`mid_year=false`、敏感性浮动幅度沿用现有约定 |
| `human` 人来定 | 没有默认值可用，必须过目 | 上面两类里人认为不对的值，就地改 |

**归一不做成静默**。规则：

- 默认**不归一**；
- 只有当「最近一年归母净利同比跌幅 > 40%」或「最近一年 EBIT 利润率 < 上一年度的 1/3」时，
  才**建议**归一，并在确认屏顶部用一句话写清建议理由与拟用哪个年份；
- 归一数据取**上一份年报的当年列**（所以默认下两份年报），不从「上年同期」列解析 ——
  两份年报各自的当年列，比跨列解析可靠。

**缺口要显式留白**：分红比例、利息支出等科目抽不到时，字段留空并在确认屏标「未取到，需手填」，
不填一个默认值蒙混过去。

---

## 九、界面（取数页）

取数页从「只读一屏」变成「现状 + 向导」两块，**现状那块原样保留**
（已下 PDF、抽取记录、出处表、行情表校验都还在），向导是新加的，四步：

1. **输入名字** → 候选列表（多于一家就点选；每行显示代码、简称、市场、是否退市）
2. **可比公司** → 同一个搜索框，勾 2–4 家，也可直接填代码
3. **确认屏** → 三列状态标签 + 每项出处，可就地改；顶部显示归一建议（如果有）
4. **确认并估值** → 进度沿用现有九阶段视图（取数六步也进同一个视图），跑完自动跳到估值页

现状那块另加两个按钮：**重新下载**、**重新抽取**（默认不勾，命中缓存就不联网）。

---

## 十、错误处理与边界

每个失败都要给出「照着就能做的下一步」（取数层已经定下的风格，沿用）：

| 情况 | 程序的行为 |
| --- | --- |
| 名称查不到 / 已退市 / 未上市（例如子公司金赛药业） | 明说「这家没有上市，建议用它的上市母公司」或列出相似候选，不返回空结果了事 |
| 巨潮限流 / 网络不可达 | 重试后明确失败；已下的 PDF 与抽取记录不丢，下次接着跑 |
| 扫描件（无文字层） | 记「疑似扫描件，需要 OCR」，**不猜数字** |
| 缺必填科目 | 列出缺哪个科目就停下，不拿别的数凑 |
| 交叉校验不过 | 标出来给人看（现有 `cross_check`） |
| 两个行情源对不上 | 报错并同时显示两个值，不静默取其一 |
| 确认屏的非法输入 | 永续增长 ≥ WACC、比率不在 0–1 → 界面提前提示，不等流水线抛错 |
| 覆盖已有产物 | 默认不覆盖；重下 / 重抽要显式勾选；`assumptions.toml` 覆盖前必备份 |

---

## 十一、测试策略

- **新模块单测**（不联网，网络调用全走假 transport）：`resolve`（多候选、退市、港股、北交所）、
  `quotes`（双源一致 / 不一致 / 备源兜底）、`assumptions_gen`（给定抽取结果 → 期望值；
  `[evidence]` 保留；备份生成）、`job`（步骤与 trace 写入）、三个新端点（复用
  `pipeline/tests/test_web.py` 的 harness）。
- **测试绝不写进 `data/companies/`**：用临时目录 + `FINAI_COMPANY_DIR` 环境变量。
  10-12 号那次「演示公司把长春高新的输入覆盖掉」的教训，写成一条测试钉住。
- **离线端到端**：用 `finai_web/examples/offline_demo.py` 那条人造年报路径，
  把一个完整的「输入名称 → 出报告」跑通且不联网。
- **真跑验证**：换一家不是长春高新的公司（建议我武生物 300357 或贵州茅台 600519）走完整流程出报告。
- **回归**：现有 563 个测试全绿；长春高新重跑结论不变（每股 76.30 元）。

---

## 十二、验收清单

- [ ] `FINAI_COMPANY_DIR` 未设置时，两个 `config.py` 的行为与今天一致，563 个测试全绿
- [ ] 工作台输入「长春高新」→ 候选 → 确认 → 出报告，全程只在界面上操作
- [ ] 输入「长春」这类模糊词，返回多家候选且可点选
- [ ] 输入「金赛药业」这类未上市公司，给出可执行的提示
- [ ] 换一家新公司（非 000661）完整跑出报告
- [ ] 断网复跑已取数公司能出报告（命中本地文件即不联网）
- [ ] `assumptions.toml` 被覆盖前有 `.bak` 备份，人写的 `[evidence]` 条目一字不少
- [ ] 每家公司一个目录，跑新公司不会改动 `data/companies/000661/`
- [ ] `data/assumptions.toml`、`data/market.csv`、`data/peers.csv` 三个夹具未被改动

---

## 十三、风险与已知边界

1. **抽取质量因公司而异**。长春高新上已经修过四处真实报表的解析问题（`94df721`），
   换公司可能还要再修一次 —— 这是这类系统的固有成本。所以流程设计成「抽不出就明确报错」，
   而不是「猜一个数让它跑通」。
2. **股价来自公开行情接口，不是 Wind/Choice**。报告里要写清来源（现有做法就是双源交叉核对
   + 注明查询日期）。提交版仍建议用终端导出核对一次。
3. **无风险利率与 ERP 要联网查一次**，属于「查一次缓存起来」的外生参数，缓存文件要记查询日期。
4. **归一阈值（40% / 三分之一）是启发式**。它只决定「要不要提示你」，不自动改口径，
   所以猜错方向的代价有限，但报告里必须写明这条规则。
5. **北交所板块映射未实测**（见 6.1）。

---

## 十四、后续（不在本次范围）

- 自动挑可比公司（需要行业分类数据源，且要能解释依据）
- 港股 / 北交所的完整估值口径（汇率、披露差异）
- 从 `origin/Framework` 摘取活公式 Excel（`pipeline/workbook.py`）与配置化的敏感性 / 反向 / 相对估值
- 仓库整理：真实数据集补 `targets.csv`、`pyproject` 加 `dev` extra、归档 `PRD.md` 与 `tools/ui-smoke.py`
