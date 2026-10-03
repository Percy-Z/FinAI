# 第三方依赖与参考来源

竞赛通知要求：「使用第三方开源项目、模型、数据或代码的，应在项目说明中列明
名称、版本、来源、许可证及具体使用范围，不得将第三方成果冒充为自主开发成果。」

本文件就是那份说明。分三类：**运行依赖**、**设计与方法参考**、**数据来源**。

---

## 一、运行依赖（可选，缺了就降级）

本项目核心（算法层、框架层、输出层的数据落盘）**只用 Python 标准库**，
不装任何第三方包也能跑完流水线并产出 Markdown 报告。下面四个是可选增强：

| 名称 | 版本 | 用途 | 缺了会怎样 | 许可证 |
| --- | --- | --- | --- | --- |
| matplotlib | 3.10.x | 六张分析图（`output/charts/`） | 报告没有插图，验收层会拦下 | PSF/BSD 兼容 |
| LaTeX（TeX Live / MacTeX，提供 `xelatex`） | TeX Live 2026 | 报告书 PDF 排版（`output/latex.py`） | 退回 reportlab 渲染 | LPPL / 各宏包各自许可 |
| reportlab | 4.x / 5.x | PDF 后备渲染（`output/pdf.py`） | 没有 PDF，只出 Markdown 与 HTML | BSD |
| pdfplumber | 0.11.x | 年报 PDF 抽取（`finai/extract.py`） | 抽取不可用，其余不受影响 | MIT |

LaTeX 用到的宏包：ctex、geometry、booktabs、tabularx、graphicx、xcolor、
tcolorbox、enumitem、titlesec、fancyhdr、hyperref（均为 TeX Live 发行版自带）。

安装：

```bash
python3 -m pip install matplotlib reportlab pdfplumber
# LaTeX 按操作系统装 TeX Live 或 MacTeX 即可，不需要额外 Python 包
```

---

## 二、设计与方法参考

### FinRobot（AI4Finance-Foundation）

| 项目 | 内容 |
| --- | --- |
| 来源 | https://github.com/AI4Finance-Foundation/FinRobot |
| 分支与提交 | `master`，参考时最新提交见仓库历史 |
| 版本 | V0（AutoGen）与 V1（`finrobot_equity`，OpenAI Agents SDK） |
| 许可证 | Apache-2.0（仓库内含 NOTICE，要求保留归属声明） |

**具体参考了什么**（都是设计，不是代码搬运）：

1. `finrobot_equity/core/src/modules/report_structure.py` 的**报告章节结构**
   （执行摘要 / 公司概览 / 财务分析 / 估值分析 / 风险因素 / 附录）与
   `.ai-badge`、`.data-source` 的**内容性质标注思路** —— 对应本项目的
   【事实】【推论】【观点】【结论】四档标注与「AI 生成」徽章。
2. `finrobot_desktop/finrobot/engine/agents/instructions/report_agent.md` 的
   **写作纪律** —— 特别是「不确定性必须保留到报告里，绝不能抹平」那条，
   改写成中文后构成 `output/narrative.py` 里 `SYSTEM_PROMPT` 的第 3 条。
3. `finrobot_equity/core/src/modules/pdf_generator.py` 用 reportlab 排 A4 报告的
   **技术路线** —— 本项目的 `output/pdf.py` 是同一条路线上的独立实现。

**没有使用的部分**：`html_renderer.py`（66 KB，深度耦合其自有数据键）、
`create_equity_report.py`（需要 7 个预先写好的文本文件）、
以及其美股数据链路（SEC / yfinance / FMP）。

本项目的报告章节结构、提示词、渲染器代码均为自主实现；上列参考仅限
设计思路与写作规范，未复制其源代码。

### 报告排版思路的来源说明

`output/pdf.py`（reportlab 路线）与 `output/latex.py`（LaTeX 路线）由本项目
实现；其中 reportlab 路由的字体注册、Markdown 子集到 flowables 的转换，
参考了本仓库另一条分支上 `pipeline/report.py` 的实现（队内成员完成，
同一仓库，非第三方）。

---

## 三、调用的大模型服务

| 项目 | 内容 |
| --- | --- |
| 服务 | DeepSeek API（https://api.deepseek.com） |
| 模型 | `deepseek-v4-pro`（默认）；调试可用 `deepseek-flash` |
| 调用方式 | HTTP `POST /chat/completions`，标准库 `urllib` 调用，未使用官方 SDK |
| 使用范围 | 仅用于**撰写报告正文**（十节中的九节）与**假设合理性审查** |
| 不用于 | 所有计算、数据校验、勾稽检查、图表绘制、文件读写 |

每次调用的模型名、提示词版本、温度、提示词与响应的 sha256、token 用量
都记录在运行清单 `10_run_manifest.json` 的 `narrative` 字段里；
原始问答存于 `logs/<运行编号>/narrative.json`。**报告正文不可复现，
但「用哪个模型、哪一版提示词写的」可查。**

模型权重与商业软件源码不需要提交；上表列明了名称、版本、调用方式与使用范围。

---

## 四、数据来源

见 [`data/SOURCES.md`](data/SOURCES.md)。所有输入数据均为本地文件，
运行时不访问网络（除大模型接口外），符合「封闭或半封闭数据环境」的要求。
