# 算法说明

本文件列出 `algorithm/finai/` 下每个算法的作用、输入和输出。

单位约定（全项目统一）：金额用元，股数用股，比率用小数（0.12 表示 12%）。
数据结构里所有金额字段都以「元」为单位，抽取层负责把年报的万元、千元统一换算过来。

## 模块总览

| 模块 | 作用 | 主要输出文件 |
| --- | --- | --- |
| `types.py` | 估值引擎的输入输出契约 | — |
| `statements.py` | 财务报表的数据结构 | — |
| `wacc.py` | 资本成本：CAPM、债务成本、WACC | — |
| `dcf.py` | 现金流折现：FCFF、终值、估值桥 | `valuation.json` |
| `forecast.py` | 三表联动预测：从假设推出完整三张表 | `projections.json` |
| `sensitivity.py` | 敏感性分析：龙卷风图、双因素网格、区间归因 | `tornado.csv`、`sensitivity.csv` |
| `reverse_dcf.py` | 反向求解：给定股价反解市场隐含假设 | `reverse_dcf.json` |
| `comps.py` | 相对估值：可比公司倍数、隐含价值、足球场图 | `comps.csv`、`comps_implied.csv` |
| `validate.py` | 勾稽校验：恒等式、现金口径、信号类检查 | `validation_report.json` |
| `output/reporting.py` | 输出层：把结果写进 `outputs/<run_id>/` | 01 到 10 号数据文件 + 运行清单 |
| `output/charts/` | 图表层：一个文件一张图 | 11 到 16 号 PNG |

---

## 1. `finai/types.py` — 估值引擎的数据契约

**用途**：定义估值引擎的输入和输出长什么样。所有假设类都是不可变的（frozen），
改假设要用 `dataclasses.replace()` 生成新对象，这是敏感性分析和反向求解能复用
同一个估值函数的前提。

| 数据结构 | 承载什么 | 关键字段 |
| --- | --- | --- |
| `ForecastAssumptions` | 经营假设 | `revenue_base`、`revenue_growth`、`ebit_margin`、`tax_rate`、`da_pct_revenue`、`capex_pct_revenue`、`nwc_pct_revenue`、`terminal_method`、`terminal_growth`、`mid_year` |
| `MarketData` | 估值桥所需的市场与负债表数据 | `price`、`diluted_shares`、`interest_bearing_debt`、`cash`、`minority_interest`、`preferred_stock`、`non_operating_assets` |
| `WaccInputs` | 资本成本参数 | `risk_free`、`beta`、`equity_risk_premium`、`cost_of_debt`、`tax_rate`、`equity_weight`、`debt_weight` |
| `DCFResult` | 估值结果 | `enterprise_value`、`equity_value`、`per_share`、`implied_return`、`terminal_share_of_ev`、`implied_exit_multiple`、`diagnostics` |

`MarketData` 有两个派生属性：`net_debt = 有息负债 - 现金`，`market_cap = 股价 x 摊薄股数`。

---

## 2. `finai/wacc.py` — 资本成本

**用途**：算出 DCF 里当折现率用的那个数。三条口径纪律：权重用市值不用账面价值；
债务用付息债务总额不用净债务；每个参数都要能说出天期、窗口和日期。

| 算法 | 做什么 | 输入 | 输出 |
| --- | --- | --- | --- |
| `cost_of_equity()` | CAPM 算股权成本 | 无风险利率、beta、股权风险溢价 | 股权成本（比率） |
| `after_tax_cost_of_debt()` | 税后债务成本 | 税前债务成本、税率 | 税后债务成本（比率） |
| `weights_from_market()` | 按市值算资本结构权重 | 权益市值、债务市值 | `(权益权重, 债务权重)` |
| `compute_wacc()` | 加权平均资本成本 | `WaccInputs` | WACC（比率） |
| `wacc_from_market()` | 便捷入口，内部先算权重 | CAPM 参数 + 权益市值 + 债务市值 | WACC（比率） |
| `check_wacc_sanity()` | 参数合理性检查，不阻断计算 | `WaccInputs` | 警告文本列表（正常为空） |

公式：

```text
Re   = Rf + beta x ERP
Rd_a = Rd x (1 - t)
WACC = E/(D+E) x Re + D/(D+E) x Rd_a
```

`check_wacc_sanity()` 会挑出这几类问题：权重之和不为 1、无风险利率超出 0 到 15%、
beta 超出 0.1 到 3、风险溢价超出 2% 到 12%、债务成本低于无风险利率、债务权重超过 70%。

---

## 3. `finai/dcf.py` — 现金流折现

**用途**：把经营假设展开成逐年现金流，折现后得到企业价值，再通过估值桥得到每股价值。

| 算法 | 做什么 | 输入 | 输出 |
| --- | --- | --- | --- |
| `project()` | 把假设展开成逐年明细 | `ForecastAssumptions`、可选 `base_year` | `Projection`（收入、EBIT、EBITDA、折旧、资本开支、营运资本、NOPAT、FCFF 各一条序列） |
| `discount_factors()` | 算折现因子 | WACC、年数、是否用中值惯例 | 折现因子列表 |
| `terminal_value_perpetuity()` | 永续增长法终值 | 末年 FCFF、WACC、永续增长率 | 终值 |
| `terminal_value_exit_multiple()` | 退出倍数法终值 | 末年 EBITDA、退出倍数 | 终值 |
| `value_dcf()` | **主入口**，走完全链路 | `ForecastAssumptions`、`MarketData`、WACC、可选 `base_year` | `DCFResult` |
| `enterprise_value()` | 轻量入口，只要企业价值 | `ForecastAssumptions`、WACC | 企业价值 |
| `_diagnose()` | 结果诊断，内部调用 | 终值占比、隐含回报、隐含倍数 | 诊断文本列表 |

计算链：

```text
收入     = 上期收入 x (1 + 增长率)
EBIT     = 收入 x EBIT 利润率
NOPAT    = EBIT x (1 - 税率)
FCFF     = NOPAT + 折旧摊销 - 资本开支 - 营运资本增加
折现因子 = 1 / (1 + WACC)^t        （中值惯例用 t - 0.5）
终值     = FCFF_末 x (1 + g) / (WACC - g)   或   EBITDA_末 x 退出倍数
企业价值 = Σ 现值(FCFF) + 现值(终值)
权益价值 = 企业价值 - 付息债务 + 现金 - 少数股东权益 - 优先股 + 非经营性资产
每股价值 = 权益价值 / 摊薄股数
```

`_diagnose()` 的三条判断：终值占企业价值超过 80% 提示预测期过短、低于 40% 提示假设
可能过保守；权益价值为负提示债务口径有问题；隐含回报不足 5% 提示安全边际有限。
另外永续增长率大于等于 WACC 会直接抛错，不会算出一个负的终值。

---

## 4. `finai/sensitivity.py` — 敏感性分析

**用途**：回答两个问题——哪个假设最要紧、两个关键假设组合下估值落在哪。
实现方式是替换假设后重算，估值引擎是纯函数，所以这里不需要任何特殊支持。

| 算法 | 做什么 | 输入 | 输出 |
| --- | --- | --- | --- |
| `Variable.bounds()` | 构造参数：直接给上下界 | 名称、字段名、下界、上界、可选基准值 | `Variable` |
| `Variable.absolute()` | 构造参数：上下浮动一个绝对量 | 名称、字段名、基准值、浮动量 | `Variable` |
| `Variable.relative()` | 构造参数：上下浮动一个百分比 | 名称、字段名、基准值、百分比 | `Variable` |
| `apply_variable()` | 把一个参数设成指定值 | `ForecastAssumptions`、WACC、`Variable`、新值 | `(新假设, 新 WACC)` |
| `tornado()` | 单因素敏感性，按影响幅度排序 | 假设、市场数据、WACC、`Variable` 列表 | `TornadoRow` 列表 |
| `attribute()` | 区间归因 | `TornadoRow` 列表 | `AttributionRow` 列表（含占比） |
| `two_way_grid()` | 双因素网格 | 假设、市场数据、WACC、行参数、列参数、点数 | `GridResult`（含二维估值矩阵） |
| `write_tornado_csv()` | 写文件 | `TornadoRow` 列表、路径 | `tornado.csv` |
| `write_grid_csv()` | 写文件 | `GridResult`、路径 | `sensitivity.csv` |

`Variable` 里的 `field` 可以是 `"wacc"`，也可以是 `ForecastAssumptions` 上的任意字段名。
`revenue_growth` 和 `ebit_margin` 这两个逐年字段传单个值时会广播到每一年。

注意：龙卷风图的排序不只取决于模型，也取决于你们给每个参数定了多宽的上下界，
所以上下界的理由要写进报告。

---

## 5. `finai/reverse_dcf.py` — 反向求解

**用途**：正向估值只能说明贵还是便宜，反向求解回答「要让每股价值正好等于现价，
增长率或折现率得是多少」。得到的数字可以和公司自身历史对照，构成可证伪的判断。
求解用二分法，不依赖 scipy。

| 算法 | 做什么 | 输入 | 输出 |
| --- | --- | --- | --- |
| `effective_bounds()` | 确定搜索区间并裁剪到合法范围 | `Variable`、WACC、可选 `bounds`、可选 `growth_floor` | `(下界, 上界)` |
| `reverse_solve()` | **主入口**，反解单个参数 | 假设、市场数据、WACC、`Variable`、可选目标价、可选区间 | `ReverseResult`（隐含值、迭代次数、方向、残差） |
| `reverse_solve_many()` | 批量反解多个参数，各自独立求解 | 同上 x N | `ReverseResult` 列表 |

两处必须在搜索前裁剪：永续增长率必须小于 WACC，否则终值不收敛；反解 WACC 时下界
必须高于永续增长率，否则一求值就报错。目标价省略时默认用当前股价。

`ReverseResult.direction` 会直接告诉你市场隐含的假设比你们基准更高还是更低。

---

## 6. `finai/comps.py` — 相对估值

**用途**：用可比公司的交易倍数推算目标公司值多少钱，并把各方法的区间合成足球场图。

| 算法 | 做什么 | 输入 | 输出 |
| --- | --- | --- | --- |
| `CompanyMetrics` | 一家公司的估值输入容器 | 代码、名称、股价、股数、归母净利润、归母净资产、收入、EBIT、EBITDA、负债、现金、少数股东权益、优先股、非经营性资产 | 对象本身；派生 `market_cap`、`net_debt`、`enterprise_value`、`eps`、`book_value_per_share` |
| `compute_multiples()` | 算一家公司的全部倍数 | `CompanyMetrics` | 字典：`pe`、`pb`、`ps`、`ev_ebitda`、`ev_ebit`、`peg`（无意义时为 `None`） |
| `percentile()` | 分位数，线性插值 | 数值序列、分位点 p | 分位数值 |
| `build_stats()` | 统计倍数分布，可剔除异常值 | 公司列表、倍数名、是否剔除 | `dict[倍数名, MultipleStats]` |
| `implied_from_multiple()` | 用可比倍数推隐含每股价值 | 目标公司、倍数名、可比倍数、统计口径 | `ImpliedValuation` |
| `comps_valuation()` | **主入口** | 目标公司、可比公司列表、倍数、统计口径 | `CompsResult`（含样本统计、隐含估值、警告） |
| `comps_bands()` | 每个倍数生成一条横条 | `CompsResult` | `FootballFieldBand` 列表 |
| `dcf_band()` | 把 DCF 结果包成一条横条 | `DCFResult`、可选上下界 | `FootballFieldBand` |
| `football_field()` | 汇总各方法区间并检查重叠 | 横条列表、现价、需要检查重叠的配对 | `FootballField`（含警告） |
| `write_comps_csv()` | 写文件 | `CompsResult`、路径 | `comps.csv`（每家公司一行 + 统计块） |
| `write_implied_csv()` | 写文件 | `CompsResult`、路径 | `comps_implied.csv` |

隐含价值的两条路径，**绝不能混**：

```text
权益类倍数（PE、PB、PS）
    隐含股价 = 可比倍数 x 目标公司的每股指标

企业价值类倍数（EV/EBITDA、EV/EBIT）
    隐含企业价值 = 可比倍数 x 目标公司指标
    隐含权益价值 = 隐含企业价值 - 净债务 - 少数股东权益 - 优先股 + 非经营性资产
    隐含股价     = 隐含权益价值 / 摊薄股数
```

把 EV/EBITDA 直接乘到每股 EBITDA 上是最常见的错误。基准样本里正确结果是 9.50，
错误做法会得到 12.50，测试里专门写了断言把这两个数区分开。

三条设计判断：分母为负时倍数没有意义（亏损公司的 PE 不是便宜），这类样本会被排除
并单独列出原因；异常值剔除默认关闭，因为可比公司通常只有三到五家，按四分位距自动
剔除一家就会显著改变中位数；分位数用线性插值，与 Excel 的 `QUARTILE.INC` 一致。

---

## 7. `finai/statements.py` — 财务报表数据结构

**用途**：按年报披露的原始结构建模，给勾稽校验提供输入。

| 数据结构 | 承载什么 | 关键字段 |
| --- | --- | --- |
| `BalanceSheet` | 资产负债表的摘要科目 | `total_assets`、`total_liabilities`、`total_equity`、`equity_attributable`、`minority_equity`、`retained_earnings`、`cash_and_equivalents`、`accounts_receivable`、`inventory` |
| `IncomeStatement` | 利润表摘要 | `revenue`、`net_profit`、`net_profit_attributable`、`non_recurring_gains`、`dividends_declared`、`reserve_appropriation` |
| `CashFlowStatement` | 现金流量表摘要 | `operating`、`investing`、`financing`、`cash_begin`、`cash_end`、`fx_effect` |
| `ReportSnapshot` | 一份年报披露的当期 + 上期数据 | 上面三张表 + `balance_begin`（上期期末）+ `income_prior`（上期金额）+ `source` |

设计要点：上期数不是塞进某个字段，而是由 `balance_begin` 和 `income_prior` 单独承载，
对应年报里「期末余额 / 年初余额」和「本期金额 / 上期金额」两列的原始结构。
缺失的数据用 `None` 表示，检查函数遇到 `None` 会返回 skipped 而不是猜。

---

## 8. `finai/validate.py` — 勾稽校验

**用途**：验证抽出来或预测出来的数字彼此是否自洽。这是竞赛通知里「财务勾稽关系」
那一项的落地实现。

四类结论的含义完全不同：

| 结论 | 含义 |
| --- | --- |
| `pass` | 该平的平了，或者差异在容差内 |
| `warning` | 有差异但存在合理解释空间，或信号类检查被触发 |
| `fail` | 该平的没平，属于数据错误，必须查 |
| `skipped` | 输入缺失，无法检查（不等同于通过） |

| 算法 | 编号 | 做什么 | 输入 | 输出 |
| --- | --- | --- | --- | --- |
| `check_data_presence()` | DQ-01 | 核心数字是否全为零或缺失、资产总额是否为正 | `ReportSnapshot` | `CheckResult` |
| `check_balance_sheet_tie()` | BS-01 | 资产 = 负债 + 所有者权益 | `ReportSnapshot`、`Tolerance` | `CheckResult` |
| `check_equity_composition()` | BS-02 | 所有者权益 = 归母权益 + 少数股东权益 | 同上 | `CheckResult` |
| `check_retained_earnings_rollforward()` | BS-03 | 期末未分配利润 = 期初 + 归母净利润 - 提取盈余公积 - 宣告股利 | 同上 | `CheckResult` |
| `check_assets_floor()` | BS-04 | 货币资金、应收账款、存货不超过资产总计 | 同上 | `CheckResult` |
| `check_cash_rollforward()` | CF-01 | 期末现金 = 期初 + 经营 + 投资 + 筹资 + 汇率影响 | 同上 | `CheckResult` |
| `check_cash_definition()` | CF-02 | 现金流量表期末现金与资产负债表货币资金的口径差异 | 同上 | `CheckResult` |
| `check_revenue_receivable_divergence()` | X-01 | 应收账款增速是否显著快于收入增速 | 快照、触发阈值 | `CheckResult` |
| `check_profit_cash_conversion()` | X-02 | 经营现金流与净利润的转化比率 | 快照、触发阈值 | `CheckResult` |
| `check_non_recurring_share()` | X-03 | 非经常性损益占归母净利润的比重 | 快照、触发阈值 | `CheckResult` |
| `check_scale_sanity()` | X-04 | 收入与总资产的量级关系（抓单位混用） | 快照 | `CheckResult` |
| `check_restatement()` | X-05 | 上年报本期数 = 本年报上期数 | 科目名、两个数、年份、`Tolerance` | `CheckResult` |
| `run_checks()` | — | **主入口**，一次跑完前十一条 | 快照、可选容差与阈值 | `ValidationReport` |

`CheckResult` 里两个数值字段的单位是分开的，不要混用：

| 字段 | 单位 | 用在哪些检查 |
| --- | --- | --- |
| `difference` | 金额（元） | 恒等式类检查（BS-01 到 BS-04、CF-01、CF-02、X-05） |
| `metric` | 指标值（比率、占比、增速差） | 信号类检查（X-01 到 X-04），此时 `tolerance` 是触发阈值 |

几条判断逻辑值得单独记住：

BS-03 在没提供分红与提取盈余公积数据时，差额可能由这两项解释，所以结论降级为
warning 而不是 fail。CF-02 的货币资金本来就包含受限资金和保证金，与现金及现金等价物
不相等是常态，所以差异占 10% 以内判 warning，超过才判 fail。X-04 用收入除以总资产
落在 0.01 到 100 之外来抓单位混用。

`ValidationReport` 提供 `summary`（各状态计数）、`failures`、`warnings`、`by_id()`、
`to_dict()`，可以直接序列化成 `validation_report.json`。

---

## 9. `finai/forecast.py` — 三表联动预测

**用途**：把估值从「假设直接跳到现金流」升级成「假设推出完整的三张表」。区别在于
预测出来的利润表、资产负债表、现金流量表互相咬合，可以用 `validate.py` 反过来检查
预测结果，形成闭环。

| 算法 | 做什么 | 输入 | 输出 |
| --- | --- | --- | --- |
| `BalanceSheetState` | 科目级资产负债表 | 现金、应收、存货、固定资产、其他非流动资产、应付、有息负债、其他非流动负债、股本、留存收益、少数股东权益 | 对象本身；派生 `total_assets`、`total_liabilities`、`total_equity`、`net_debt`、`working_capital`、`balance_gap` |
| `IncomeStatementState` | 科目级利润表 | 收入、成本、毛利、期间费用、折旧摊销、EBITDA、EBIT、利息、税前利润、所得税、净利润、少数股东损益、归母净利润 | 对象本身 |
| `CashFlowState` | 现金流量表 | 净利润、折旧摊销、应收/存货/应付变动、经营、资本开支、投资、债务变动、分红、筹资、现金净增加、期初与期末现金 | 对象本身；派生 `delta_nwc` |
| `ForecastDrivers` | 驱动假设 | 基期收入、基期年份、预测年数、收入增速、毛利率、费用率、折旧摊销率、资本开支率、应收/存货/应付占收入比、债务利率、税率、分红比例、还款计划、少数股东损益占比 | 对象本身；标量自动广播，序列长度必须等于年数 |
| `build_forecast()` | **主入口**，逐年后推 | 期初 `BalanceSheetState`、`ForecastDrivers`、来源标记 | `ThreeStatementForecast`（逐年 `ForecastYear`） |
| `ForecastYear.fcff` | 从三表算自由现金流 | 该年的利润表与现金流量表 | FCFF（元） |
| `ThreeStatementForecast.to_records()` | 展开成逐年记录 | 对象本身 | 字典列表（供 `projections.json`） |
| `to_snapshots()` | 转成校验能吃的快照 | `ThreeStatementForecast` | `ReportSnapshot` 列表 |
| `to_summary_balance()` / `to_summary_income()` / `to_summary_cashflow()` | 科目级转摘要级 | 对应的状态对象 | `statements.py` 里的摘要结构 |

联动顺序（每年重复一次）：

```text
1.  收入      = 上期收入 x (1 + 增长率)
2.  毛利      = 收入 x 毛利率
3.  期间费用  = 收入 x 费用率（不含折旧摊销）
4.  折旧摊销  = 收入 x 折旧摊销率
5.  EBIT      = 毛利 - 期间费用 - 折旧摊销
6.  利息费用  = 期初有息负债 x 利率
7.  税前利润  = EBIT - 利息费用
8.  所得税    = 税前利润 x 税率
9.  净利润    = 税前利润 - 所得税，再拆成归母与少数股东
10. 资产负债表：应收、存货、应付按收入比例；固定资产按「期初 + 资本开支 - 折旧」滚动；
                有息负债按还款计划滚动；留存收益按「期初 + 归母净利润 - 分红」滚动
11. 现金流量表：经营现金流用间接法（净利润 + 折旧 - 营运资本增加）；
                投资现金流 = -资本开支；筹资现金流 = 债务变动 - 分红
12. 现金      = 期初现金 + 经营 + 投资 + 筹资，回填到资产负债表
```

**为什么现金要最后算**：现金是模型的塞子。其他科目都由假设驱动，现金由现金流量表
推出来，这样资产负债表自动平衡，不需要事后调平。

**平衡的充要条件**：资产减负债减权益的变动量，等于「其他非流动资产变动」减去
「其他非流动负债变动」。这两项在本模型里保持期初水平不变，所以差额恒为零。
如果以后要让它们随收入增长，两边必须同幅增长，否则表会不平。

**基期校验**：`build_forecast()` 会先检查期初资产负债表是否自平，不平就直接报错，
不允许在错误的基础上做预测。

---

## 10. `output/reporting.py` — 输出层

这个模块**不在 `finai/` 包里**，它单独放在 `algorithm/output/` 下，
是唯一一个不放在算法层里的模块。这样分层的依赖是单向的：
`output` 依赖 `finai`，`finai` 完全不知道 `output` 的存在。

**用途**：把算法层的结果落成文件与图表。每次运行产出一个带时间戳的目录
`outputs/<run_id>/`，不覆盖上一次；同时写一份 `10_run_manifest.json`，
记录每个产物的 sha256 与字节数。

输出层分成两部分：`reporting.py` 负责数据文件与运行清单，`charts/` 子包负责图表。
**一个文件负责一张图**，编号写在文件名最前面，全目录编号唯一（数据 01 到 10，图表 11 到 16）。
详细的对应关系见 `algorithm/output/README.md`。

| 算法 | 做什么 | 输入 | 输出 |
| --- | --- | --- | --- |
| `RunOutputs` | 一次运行的产出容器 | 各算法的结果对象，缺省为 None | 对象本身 |
| `export_run()` | **主入口**，写全部数据文件与图表 | `RunOutputs`、可选根目录、可选 run_id、可选元信息、是否覆盖、是否出图 | `RunArtifacts`（含目录、文件字典、清单） |
| `data_filename()` | 按编号拼数据文件名 | 产物名，如 `valuation` | 文件名，如 `03_valuation.json` |
| `make_run_id()` | 生成时间戳运行编号 | 可选时间、可选前缀 | 字符串，如 `20260927T165932` |
| `write_json()` | 写 JSON，中文不转义 | 路径、任意可序列化对象 | 文件路径 |
| `sha256_of()` | 算文件哈希 | 文件路径 | 十六进制哈希字符串 |
| `summarize()` | 把产物列成一段文本 | `RunArtifacts` | 多行文本，终端打印用 |
| `charts.render_all()` | 按有哪些结果批量出图 | `RunOutputs`、目标目录 | 图名到路径的字典 |
| `charts.chart_filename()` | 按编号拼图表文件名 | 图名，如 `tornado` | 文件名，如 `12_tornado.png` |

每个文件来自哪个算法的结果：

| 编号 | 文件 | 来自 |
| --- | --- | --- |
| 01 | `01_projections.json` | `forecast.build_forecast()` |
| 02 | `02_validation_report.json` | `validate.run_checks()` |
| 03 | `03_valuation.json` | `dcf.value_dcf()` |
| 04 | `04_tornado.csv` | `sensitivity.tornado()` |
| 05 | `05_sensitivity.csv` | `sensitivity.two_way_grid()` |
| 06 | `06_reverse_dcf.json` | `reverse_dcf.reverse_solve_many()` |
| 07 | `07_comps.csv` | `comps.comps_valuation()` |
| 08 | `08_comps_implied.csv` | `comps.comps_valuation()` |
| 09 | `09_football_field.json` | `comps.football_field()` |
| 10 | `10_run_manifest.json` | `export_run()` 自己生成 |

### 10.2 图表子包 `charts/`

公共样式放在 `base.py`，所有图共用一套配色、中文字体和保存参数。
每个图表模块只暴露一个 `render()` 函数。

| 编号 | 文件 | 程序 | 数据来源 | 回答什么问题 |
| --- | --- | --- | --- | --- |
| 11 | `11_football_field.png` | `charts/football_field.py` | 09 | 各方法区间差多少，现价在区间内还是外 |
| 12 | `12_tornado.png` | `charts/tornado.py` | 04 | 哪个假设影响最大 |
| 13 | `13_sensitivity_heatmap.png` | `charts/sensitivity_heatmap.py` | 05 | 哪些组合下估值低于现价 |
| 14 | `14_peer_multiples.png` | `charts/peer_multiples.py` | 07 | 相对可比公司贵还是便宜 |
| 15 | `15_forecast_trend.png` | `charts/forecast_trend.py` | 01 | 未来五年的盈利与偿债能力 |
| 16 | `16_validation_summary.png` | `charts/validation_summary.py` | 02 | 哪一年哪一项勾稽检查出问题 |

三条设计约定。**哪些字段是 None，对应文件就不会生成**，所以可以只导出部分结果。
**同名运行目录已存在且有内容时直接报错**，要覆盖得显式传 `overwrite=True`，
避免把上一次的结果悄悄冲掉。**清单里不列它自己**：`run_manifest.json` 是在写完
所有产物之后才生成的，没法包含自己的哈希。

`run_manifest.json` 是复现性的凭证，结构如下：

```json
{
  "run_id": "20260927T165932",
  "created_at": "2026-09-27T16:59:32",
  "python": "3.14.5",
  "platform": "macOS-...",
  "inputs": {"target": "T", "price": 10.0, "wacc": 0.0947},
  "outputs": [
    {"name": "valuation", "file": "03_valuation.json", "bytes": 2217, "sha256": "..."},
    {"name": "chart_tornado", "file": "charts/12_tornado.png", "bytes": 92297, "sha256": "..."}
  ],
  "warnings": []
}
```

---

## 11. 整体数据流

```text
① 数据准备与校验
    年报 PDF（尚未接入） → 文档抽取 → financials.json
                                          |
                                          v
                             validate.py 勾稽校验 → validation_report.json

② 估值计算
    wacc.py 资本成本 ─┐
    forecast.py 三表预测 ─┴→ dcf.py 估值 ─┬→ sensitivity.py 敏感性分析
                                            ├→ reverse_dcf.py 反向求解
                                            └→ 足球场图 ← comps.py 相对估值

③ 落盘（reporting.py）
    上面所有结果 → export_run(RunOutputs(...)) → outputs/<run_id>/
        valuation.json                ← dcf
        football_field.json / .png    ← 足球场图
        comps.csv / comps_implied.csv ← comps
        sensitivity.csv / tornado.csv ← sensitivity
        reverse_dcf.json              ← reverse_dcf
        validation_report.json        ← validate
        projections.json              ← forecast
        run_manifest.json             ← 每个产物的 sha256
```

两条独立的估值线（DCF 与相对估值）算出区间后，`football_field()` 会检查它们是否重叠。
不重叠说明至少有一边的假设有问题，这个提示比「估值偏高还是偏低」更要紧。

---

## 12. 还没做的部分

| 待做 | 说明 |
| --- | --- |
| 数据层 | 读 YAML 假设文件、写运行日志（`trace.jsonl`）。JSON 落盘与运行清单已由 `reporting.py` 完成 |
| 文档抽取 | 年报 PDF 到结构化财报，每个数字绑定页码与原文片段 |
| DCF 接三表 | 目前 `dcf.py` 走的是简化路径，`forecast.py` 算出的 FCFF 序列还没有接进估值函数 |
| 报告生成 | 从各类 json 与 csv 生成估值报告（md / pdf / 活公式 xlsx） |

当前测试规模：`algorithm/tests/` 下 271 个单元测试，全部用手算数字做断言。
