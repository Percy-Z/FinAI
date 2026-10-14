# FinAI

2026 年北京市大学生金融人工智能竞赛「自动化估值建模」方向的实现仓库。

当前进度：算法层十个模块、输出层（数据文件 + 六张图 + 报告）、框架层十个阶段、
年报抽取、MCP 服务与 Skill 全部打通。`make run` 一条命令从配置跑到
**带图表与大模型推理的报告**，共 546 个单元测试覆盖（算法层 412 +
框架层 92 + 取数层 30 + MCP 12）。每个算法的输入输出见
[`algorithm/shuoming.md`](algorithm/shuoming.md)。

## 快速开始

**核心链路只用 Python 标准库**，不装任何第三方包也能跑完流水线并产出
Markdown 报告。四个可选依赖分别负责图表、PDF、年报抽取，装了就生效、
没装就降级（不报错）。完整清单与安装方式见 [`THIRD_PARTY.md`](THIRD_PARTY.md)，
复现步骤见 [`docs/reproduce.md`](docs/reproduce.md)。

```bash
python3 -m pip install -e ".[all]"   # 可选依赖全装；只想跑通可跳过
make verify                          # 静态检查 + 测试 + 配置 + 跑一遍 + 验收产物
make demo                            # 现场演示：跑一遍并打开报告 PDF
```

装完之后多一个 `finai` 命令，在任何目录都能用：`finai run`、`finai check`、
`finai extract <年报.pdf>`、`finai mcp`、`finai version`。

**不想敲命令**：在 Finder 里双击仓库根目录的 `启动工作台.command`，
会自动开一个终端并打开浏览器里的工作台（`http://127.0.0.1:8770/`）。
界面上点一下按钮就能跑完整流水线，能看到九个阶段逐个完成、关键指标、
六张图和产物清单。跑完之后页面顶部会出现**打开报告**的快捷入口，
下面还有一组**复现凭证**按钮：报告 PDF、报告网页、运行清单（全部产物的
sha256）、执行轨迹、大模型原始问答，以及三个输入文件 —— 点开就是原始记录，
不是二次加工的展示页。这三个目录（`runs/`、`logs/`、`data/`）是只读开放的，
且做了路径校验，只能读仓库内的这些文件。关掉那个终端窗口就是停止服务。

> 首次双击若被 macOS 拦下（提示「无法验证开发者」）：右键 →「打开」，
> 或到「系统设置 → 隐私与安全性」里点「仍要打开」。这是下载文件的门禁，
> 与本项目无关。也可以先执行一次 `chmod +x 启动工作台.command`。

下面的命令都在仓库根目录 `FinAI/` 下执行：

```bash
python3 -m unittest discover -s algorithm/tests -v   # 算法层测试
python3 algorithm/examples/dcf_demo.py               # 各模块的演示脚本
python3 algorithm/examples/pipeline_demo.py          # 端到端：假设进，文件出
```

注意 `discover` 必须指定 `-s algorithm/tests`：`algorithm` 是命名空间包，
不带参数直接跑 `python3 -m unittest discover` 会找不到测试。

### 让大模型写报告正文（可选）

报告的数字全部由代码算好，大模型只负责把数字讲成人话。**不配 key 也能跑** ——
这时会出一份「数据版本报告」：表格、图、数字都在，只是叙述段落换成一句说明。
注意报告开头会出现红色横幅，明确写着这份报告不满足提交要求。

```bash
export DEEPSEEK_API_KEY=sk-xxxxx     # 方式一：环境变量
echo 'sk-xxxxx' > apikey.txt         # 方式二：本地文件（已在 .gitignore 里）
make run
```

模型与参数在 `data/assumptions.toml` 的 `[report]` 小节（默认 `deepseek-v4-pro`，
调试时换成 `deepseek-flash`、把 `effort` 调成 `low`，能快一倍）。**key 不写进配置文件。**

一次完整运行约 3 分钟，其中 2 分半是大模型在推理与写报告。

## 系统架构

![系统架构与数据流](docs/architecture.png)

![三表联动机制](docs/three-statement.png)

两张图的源码在 [`docs/`](docs/) 下，是可编辑的 Mermaid 文件，改完按
[`docs/README.md`](docs/README.md) 里的命令重新渲染即可。

## 目录结构

```text
algorithm/
  finai/
    types.py        估值引擎的输入输出契约（经营假设、市场数据、估值结果）
    wacc.py         资本成本：CAPM、债务成本、WACC
    dcf.py          估值引擎：FCFF 预测、折现、终值、估值桥
    sensitivity.py  敏感性分析：龙卷风图、双因素网格、区间归因、CSV 输出
    reverse_dcf.py  反向求解：给定股价反解市场隐含的假设
    statements.py   财务报表数据结构（三张表 + 上期可比数）
    validate.py     勾稽校验：恒等式、现金口径、信号类检查
    comps.py        相对估值：可比公司倍数、隐含价值、足球场图
    forecast.py     三表联动预测：从假设推出完整三张表
    extract.py      年报抽取：从年报 PDF 取关键科目，每个数字带页码与原文
  output/           输出层（只依赖 finai，不反向依赖）
    reporting.py    把结果写进 algorithm/runs/<run_id>/
    charts/         一个文件一张图，11 到 16 号
    narrative.py    叙述层：把结果交给大模型写成报告正文（可降级）
    report.py       报告层：组装成 17 报告 md 与 18 报告 html
    pdf.py          把报告 Markdown 渲染成 PDF（19；需要 reportlab）
  tests/            单元测试，用手算数字做断言
  examples/         可直接运行的示例脚本
  runs/             运行产物。产物目录统一叫 runs/，避免和包名 output 只差一个 s
  mcp_server/       MCP 服务：把流水线能力暴露成标准协议接口
  skills/           Agent Skill：描述什么时候、怎么用这套估值流程
```

## 端到端跑一遍

`pipeline_demo.py` 把八个算法模块串起来，一次跑完并落盘：

```text
资本成本 → 三表联动预测 → 勾稽校验 → DCF 估值
→ 敏感性分析 → 反向求解 → 相对估值 → 足球场图 → 写文件
```

每次运行新建一个带时间戳的目录，不覆盖上一次。**文件名前面的编号是固定的**，
数据文件 01 到 10、图表 11 到 16，全目录编号唯一，按文件名字典序排出来就是流水线顺序：

```text
algorithm/runs/20260927T165932/
  01_projections.json        三表联动预测
  02_validation_report.json  勾稽校验报告（可能含多年）
  03_valuation.json          DCF 估值结果
  04_tornado.csv             单因素敏感性排序
  05_sensitivity.csv         双因素敏感性网格
  06_reverse_dcf.json        反向求解出的市场隐含假设
  07_comps.csv               每家公司一行的倍数表
  08_comps_implied.csv       每种倍数的隐含每股价值
  09_football_field.json     估值区间的数据
  10_run_manifest.json       运行清单，含每个产物的 sha256
  17_report.md               估值报告（Markdown）
  18_report.html             估值报告（自包含 HTML，可直接打印成 PDF）
  19_report.pdf              估值报告（A4 PDF，需要 reportlab）
  charts/
    11_football_field.png      估值区间
    12_tornado.png             单因素敏感性
    13_sensitivity_heatmap.png 双因素敏感性
    14_peer_multiples.png      可比公司倍数
    15_forecast_trend.png      三表预测趋势
    16_validation_summary.png  勾稽校验总览
```

图表层的组织方式和完整对应表见 [`algorithm/output/README.md`](algorithm/output/README.md)。

报告的八个章节是：估值摘要、公司与预测概览、财务勾稽校验、估值分析、
敏感性与区间归因、反向估值、风险与适用边界、附录。每个章节标题标出内容性质
（【事实】/【推论】/【观点】），大模型写的段落单独打「AI 生成」徽章；
附录里有大模型使用记录（模型、提示词版本、哈希、token 用量）和数字校验结论 ——
程序会把正文里出现的每一个数字与事实表比对，对不上的全部列出来。

`run_manifest.json` 是可复现性的凭证：记录每个输出文件的 sha256、字节数，
以及调用方传入的输入说明。两次运行结果不一样时，用它就能判断是输入变了、
代码变了还是假设变了。

## 相对估值的两条路径

| 倍数类型 | 倍数 | 隐含价值怎么算 |
| --- | --- | --- |
| 权益类 | PE、PB、PS | 倍数 x 目标公司的每股指标，直接得到隐含股价 |
| 企业价值类 | EV/EBITDA、EV/EBIT | 倍数 x 目标公司指标 = 隐含企业价值，再减净债务、少数股东权益、优先股，加回非经营性资产，最后除以摊薄股数 |

把 EV/EBITDA 直接乘到每股 EBITDA 上是最常见的错误，测试里专门锁了这条：
基准样本下正确结果是 9.50，错误做法会得到 12.50。

分母为负时倍数没有意义——亏损公司的 PE 不是「便宜」——这类样本会被排除并单独列出，
不参与统计。可比公司通常只有三到五家，按四分位距自动剔除会显著改变结论，
所以异常值剔除默认关闭，只用中位数抗偏。

## 勾稽校验的四类结论

| 结论 | 含义 |
| --- | --- |
| pass | 该平的平了，或差异在容差内 |
| warning | 有差异但存在合理解释空间，或信号类检查被触发 |
| fail | 该平的没平，属于数据错误，必须查 |
| skipped | 输入缺失，无法检查（不等同于通过） |

当前共十一项检查：DQ-01 数据完整性、BS-01 资产负债配平、BS-02 权益构成、
BS-03 未分配利润滚动、BS-04 明细不超总额、CF-01 现金滚动、CF-02 现金口径、
X-01 收入与应收背离、X-02 利润现金转化、X-03 非经常性损益占比、X-04 量级合理性。
另有 X-05 跨期一致性需要两份年报，用 `check_restatement` 单独调用。

DQ-01 排在最前面，专门拦"数据其实没抽到"这种情况：资产、收入、净利润、
经营现金流全为零时，恒等式 0 = 0 + 0 恒成立，会报出一条假通过。

## 单位约定

全项目统一：金额用元，股数用股，比率用小数（0.12 表示 12%）。
中途不要换单位，这是最容易翻车的地方。

## 计算链

```text
收入     = 上期收入 x (1 + 增长率)
EBIT     = 收入 x EBIT 利润率
NOPAT    = EBIT x (1 - 税率)
FCFF     = NOPAT + 折旧摊销 - 资本开支 - 营运资本增加
折现因子 = 1 / (1 + WACC)^t
终值     = FCFF_末 x (1 + g) / (WACC - g)   或   EBITDA_末 x 退出倍数
企业价值 = Σ 现值(FCFF) + 现值(终值)
权益价值 = 企业价值 - 付息债务 + 现金 - 少数股东权益 - 优先股 + 非经营性资产
每股价值 = 权益价值 / 摊薄股数
```

## 下一步

按依赖顺序，接下来要做的是：

1. 文档抽取：年报 PDF 到结构化财报。这是数据入口最后一段手工环节 ——
   现在 `[opening_balance]` 和 `data/peers.csv` 都是手填的过渡数据
2. 把 `forecast.py` 算出的 FCFF 序列接进 `dcf.py` 的估值函数
   （目前估值走的是简化路径，两条路径的 FCFF 由回归测试盯着，数值一致）
3. 初赛交付物：项目计划书与 5 分钟演示视频（截止 10 月 18 日）

报告出三份：Markdown（内容源）、自包含 HTML（图内嵌，浏览器直接看）、
A4 PDF（LaTeX 排版）。PDF 走「LaTeX 优先、reportlab 后备」的降级链，
两条路共用同一个内容源，不会走偏。

## 打包与分发

三种形态，按需要选。

**一、源码 + 可编辑安装（提交作品与现场演示用这个）**

```bash
python3 -m pip install -e ".[all]"
finai run
```

装的是 `pyproject.toml` 里 `[project.scripts]` 声明的入口（`pipeline/cli.py`），
用标准库 argparse 写的，不依赖 click 之类的框架。
**这一种最符合通知要求**：源码、依赖声明、运行说明齐全，
「附依赖文件及操作说明，确保评审人员能够复现」说的就是它。

**二、Docker（换机器复现用）**

环境差异（Python 版本、中文字体、LaTeX）是复现失败的主要原因，
Docker 把这层一次性消掉。需要写一个 Dockerfile：Python 3.11+、可选依赖、
LaTeX 与中文字体。**要做的话我可以写。**

**三、单文件可执行程序（PyInstaller）**

```bash
python3 -m pip install pyinstaller
pyinstaller --name finai --onefile --add-data "data:data" pipeline/cli.py
```

产物是 `dist/finai`。两个坑要知道：matplotlib 与 reportlab 的资源文件要额外
`--add-data`；**LaTeX 是系统级程序，打不进单文件**，所以单文件版的 PDF
只能走 reportlab 路线。好处是评委不用配环境。

**建议**：提交与演示都用第一种。单文件版是加分项，不是必需品。
### 输入公司名，自动取数并估值

工作台的**取数页**现在能自己跑完整条链路：输入公司名 → 检索候选 → 选可比公司 →
下载年报 → 抽取科目 → 取股价 → 生成假设草稿 → 你复核确认 → 出报告。

```text
① 要估哪家公司   输入「长春高新」或六位代码，支持模糊（输入「长春」会列出几家让你挑）
② 可比公司       同一个搜索框，勾 2–4 家
③ 取数并生成假设  六步：解析 → 下载 → 抽取 → 汇总 → 取价 → 假设草稿
④ 复核假设       每个值都带状态：自动推导 / 规则默认 / 沿用上一版 / 需人工填
⑤ 确认并估值     写 assumptions.toml（覆盖前自动备份）→ 跑流水线 → 报告
```

每一家公司一个目录（`data/companies/<代码>/`），互不覆盖。已经取过数的公司，
断网也能复跑到报告（命中本地年报与抽取记录就不联网）。

命令行等价做法（工作台起的就是这两条命令）：

```bash
python3 -m pipeline.job plan --company-dir data/companies/600519 --target-code 600519 \
        --target 贵州茅台 --peer 000858 --peer 000568
# 复核 data/companies/600519/assumptions.draft.json，填上标「需人工填」的字段
python3 -m pipeline.job run  --company-dir data/companies/600519
```

老数据集（例如原先放在 `data/real-000661/` 的那套）用迁移命令收进公司目录：

```bash
python3 -m finai_web company --migrate 000661     # 先打印将要移动的清单
python3 -m finai_web company --migrate 000661 --yes
```

### 让大模型写报告正文（可选）
