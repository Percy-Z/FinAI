# 输出层

把算法层的结果落成文件。**一个文件负责一张图，编号写在文件名最前面。**

命名提醒：代码目录叫 `output/`，产物目录叫 `algorithm/outputs/`，只差一个 s。

## 目录结构

```text
output/
  __init__.py          对外接口
  reporting.py         写数据文件（json / csv）与运行清单
  charts/
    __init__.py        图表注册表 + render_all()
    base.py            公共样式：配色、中文字体、保存
    football_field.py  图 11  估值区间
    tornado.py         图 12  单因素敏感性
    sensitivity_heatmap.py  图 13  双因素敏感性
    peer_multiples.py  图 14  可比公司倍数
    forecast_trend.py  图 15  三表预测趋势
    validation_summary.py   图 16  勾稽校验总览
```

## 编号规则

数据文件占 01 到 10，图表接在后面占 11 到 16，**全目录编号唯一**。
放在文件名最前面而不是最后，是为了让目录按文件名字典序排出来就是流水线顺序，
打开文件夹从上往下看就是数据的生成顺序。

## 数据文件

| 编号 | 文件 | 生成的代码 | 内容 |
| --- | --- | --- | --- |
| 01 | `01_projections.json` | `reporting.export_run` ← `forecast.build_forecast` | 三表联动预测的逐年明细 |
| 02 | `02_validation_report.json` | ← `validate.run_checks` | 勾稽校验报告，可能含多年 |
| 03 | `03_valuation.json` | ← `dcf.value_dcf` | DCF 估值结果与诊断 |
| 04 | `04_tornado.csv` | ← `sensitivity.tornado` | 单因素敏感性排序 |
| 05 | `05_sensitivity.csv` | ← `sensitivity.two_way_grid` | 双因素网格 |
| 06 | `06_reverse_dcf.json` | ← `reverse_dcf.reverse_solve_many` | 反解出的市场隐含假设 |
| 07 | `07_comps.csv` | ← `comps.comps_valuation` | 每家公司一行的倍数表 |
| 08 | `08_comps_implied.csv` | ← `comps.comps_valuation` | 每种倍数的隐含每股价值 |
| 09 | `09_football_field.json` | ← `comps.football_field` | 估值区间的数据 |
| 10 | `10_run_manifest.json` | `reporting.export_run` | 运行清单，含每个产物的 sha256 |

## 图表

每张图一个文件，文件名就是编号加图名。所有图共用 `charts/base.py` 里的配色和字体。

| 编号 | 文件 | 程序文件 | 数据来源 | 回答什么问题 |
| --- | --- | --- | --- | --- |
| 11 | `11_football_field.png` | `charts/football_field.py` | `09_football_field.json` | 各方法的估值区间差多少，现价落在区间内还是外 |
| 12 | `12_tornado.png` | `charts/tornado.py` | `04_tornado.csv` + 现价 | 哪个假设对估值影响最大，精力该花在哪 |
| 13 | `13_sensitivity_heatmap.png` | `charts/sensitivity_heatmap.py` | `05_sensitivity.csv` | 哪些假设组合下估值会低于现价 |
| 14 | `14_peer_multiples.png` | `charts/peer_multiples.py` | `07_comps.csv` | 目标公司相对可比公司是贵还是便宜 |
| 15 | `15_forecast_trend.png` | `charts/forecast_trend.py` | `01_projections.json` | 未来五年的盈利、现金流与偿债能力怎么走 |
| 16 | `16_validation_summary.png` | `charts/validation_summary.py` | `02_validation_report.json` | 哪一年哪一项勾稽检查出了问题 |

## 用法

正常跑一遍端到端，图表会自动生成：

```bash
cd ~/Desktop/FinAI
python3 algorithm/examples/pipeline_demo.py
```

只要数据不要图：

```python
from algorithm.output import export_run, RunOutputs

export_run(outputs, render_charts=False)
```

单独渲染某一张图：

```python
from algorithm.output import charts

charts.render_tornado(rows, price=10.0, path="out/tornado.png")
```

## 设计约定

**缺什么就不生成什么。** `RunOutputs` 里哪个字段是 `None`，对应的数据文件和图表都不会生成。
只跑 DCF 也能导出，不必凑齐所有模块。

**编号固定不随运行变化。** 报告里可以直接写"见 11_football_field.png"，不用担心文件名漂移。

**图表不依赖 matplotlib 之外的东西。** 没装 matplotlib 时本包仍可导入，
`export_run` 会跳过错图并在运行清单的 `warnings` 里记一笔，不会报错中断。

**一个文件一张图。** 想改哪张图就改哪个文件，不用在一千行的画图脚本里翻。
