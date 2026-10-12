# 复现指南

竞赛通知要求「确保评审人员能够复现主要任务流程和输出结果」。
这份文档是给评审看的：从一台干净机器开始，到什么程度能看到什么结果。

---

## 一、最低要求：不装任何第三方包

```bash
git clone <仓库地址> finai && cd finai
make test          # 488 个单元测试，约 20 秒
make run           # 完整跑一遍，约 20 秒（不含大模型）
```

这时你会得到 `runs/<运行编号>/` 下的 **十个数据文件** —— 三表预测、
勾稽校验、DCF 估值、敏感性、反向求解、相对估值、估值区间，外加带 sha256
的运行清单。**报告也会生成**（Markdown 与 HTML），只是没有图、没有大模型的
文字、也没有 PDF：报告开头会有红色横幅写明缺什么。

核心链路只用 Python 标准库，所以这一步在任何有 Python 3.11+ 的机器上都成立。

## 二、完整效果：装上可选依赖

```bash
python3 -m pip install matplotlib reportlab pdfplumber
# PDF 首选 LaTeX 排版，需要 TeX Live 或 MacTeX（提供 xelatex）
# macOS: brew install --cask mactex-no-gui   或装 BasicTeX

export DEEPSEEK_API_KEY=sk-xxxxx    # 或者把 key 写进 apikey.txt
make verify
```

`make verify` 按顺序做五件事，任何一步失败都会停下并说明原因：

```
lint      静态检查（mypy + pyflakes + pyright 严格模式）
test      546 个单元测试（算法层 412 + 框架层 92 + 取数层 30 + MCP 12）
config    读一遍配置并自检
run       跑完整流水线（含大模型撰写报告，约 2 分钟）
check     验收产物：文件齐不齐、报告有没有引用全部图
```

跑完看 `runs/<运行编号>/`：

```
01_projections.json        三表联动预测的逐年明细
02_validation_report.json  勾稽校验（逐年逐条）
03_valuation.json          DCF 估值结果与诊断
04_tornado.csv             单因素敏感性排序
05_sensitivity.csv         双因素网格
06_reverse_dcf.json        市场隐含假设
07_comps.csv               可比公司倍数表
08_comps_implied.csv       各倍数的隐含每股价值
09_football_field.json     各方法的估值区间
10_run_manifest.json       运行清单（含全部产物的 sha256）
17_report.md               估值报告（Markdown，文字源）
18_report.html             估值报告（自包含 HTML，图内嵌）
19_report.pdf              估值报告（A4，LaTeX 排版；没装 LaTeX 时退回 reportlab）
report.tex                 19 号 PDF 的 LaTeX 源
charts/                    六张分析图（11-16 号）
```

另外 `logs/<运行编号>/` 下有 `trace.jsonl`（逐步执行轨迹）与
`narrative.json`（大模型的原始问答）。

### 可选依赖：LaTeX（决定报告 PDF 的排版）

装了 LaTeX 就出「LaTeX 排版」那版报告书（表格用 booktabs 三线表、图表浮动体、
页眉页脚），没装就自动退回 reportlab 渲染 —— 两条路内容一致，只是版式不同。
流水线在任何一种情况下都能跑完，报告 PDF 也一定会有。

```bash
# Windows：用户级安装，不需要管理员，装完把 bin 目录写进用户 PATH
winget install MiKTeX.MiKTeX

# macOS
brew install --cask mactex-no-gui      # 或完整版 mactex

# Linux（Debian/Ubuntu）
sudo apt install texlive-xetex texlive-lang-chinese texlive-latex-extra
```

两个坑，代码里都已经处理（`algorithm/output/latex.py`）：

1. **字体集不能写死**。ctex 的 fontset 选项不会自动换：写死 macnew 在 Windows 上
   直接报「字体找不到」。现在按平台选（Windows 用 windows、macOS 用 macnew、
   其它用 fandol）。
2. **PATH 不一定马上生效**。MiKTeX 装完只把 bin 目录写进用户 PATH，已经开着的
   终端读的还是旧环境。所以引擎查找除了 PATH，还会认几个常见安装位置 ——
   否则报告会悄悄退回 reportlab，版式换了没人知道。

## 三、现场演示（决赛用）

```bash
make demo
```

这个目标做三件事：跑一遍完整流水线、打印产物清单、把报告 PDF 打开。
**从零到一份完整报告大约两分钟**，其中大部分是大模型在推理与撰写。

如果现场网络不稳或没有 API key，先执行 `export DEEPSEEK_API_KEY=`（清空）
再 `make demo` —— 会在 20 秒内产出「数据版本报告」：数字、表格、图、
PDF 都在，只是叙述段落换成一句说明，报告开头有横幅标明。

想在演示中展示「换一家公司会怎样」：改 `data/assumptions.toml` 里的
`[company]` 与 `[drivers]`，重跑 `make run` 即可 —— 所有计算都是
从那几个假设推导出来的，没有写死的数字。

## 四、复现性的边界（哪些能复现，哪些不能）

这条必须说清楚，否则「可复现」是一句空话。

| 产物 | 可复现性 |
| --- | --- |
| 十个数据文件、六张图 | **逐字节相同**。同一份配置跑两次，内容完全一致（有测试盯着） |
| 报告正文（Markdown） | **可复现**，除了抬头里的运行编号与出具时间 |
| 报告 HTML / PDF | **不是逐字节相同**：HTML 含运行编号与时间，PDF 还含 reportlab / LaTeX 写入的生成时间戳 |
| 大模型写的九节文字 | **不可复现** —— 同样的输入两次跑出来措辞不同。这是大模型的固有性质 |

对于最后一类，本项目采取的办法是**把能固定下来的全部固定**：
模型名、接口地址、温度、推理强度、提示词版本、提示词与响应的 sha256、
token 用量，全部记进运行清单；原始问答单独落盘到日志目录。
所以报告正文本身不可复现，但「这份报告是哪一版提示词、哪个模型写出来的」
完全可查 —— 改动提示词会升版本号，历史运行不会混淆。

## 五、验证复现性的具体做法

```bash
# 同一份配置跑两次，比较数据产物
python3 -m unittest pipeline.tests.test_pipeline.TestEndToEnd -v

# 看某次运行读的是哪份数据（含哈希）
cat runs/<运行编号>/10_run_manifest.json | python3 -m json.tool | head -40

# 看某次运行的每一步做了什么
cat logs/<运行编号>/trace.jsonl

# 验收某次运行的产物是否齐全
make check RUN=<运行编号>    # 省略 RUN 就是检查最近一次
```

## 六、常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 报告开头有「不含大模型」横幅 | 没配 API key。设 `DEEPSEEK_API_KEY` 或写 `apikey.txt` |
| 报告开头有「不含图表」横幅 | 当前 Python 没装 matplotlib：`python3 -m pip install matplotlib` |
| 证书验证失败（SSL） | 本机 CA 配置问题，不是接口问题。设 `SSL_CERT_FILE` 指向含正确根证书的包，或给该 Python 跑一次 Install Certificates.command |
| LaTeX 编译失败 | 看 `runs/<编号>/` 下同名的 `.log`；也可以直接把 `report.tex` 拖进 Overleaf 编（中文字体由 ctex 处理） |
| 相对估值倍数全是空的 | `data/peers.csv` 里缺这家公司 |
| 流水线报「基期资产负债表不平」 | 改 `[opening_balance]`：资产必须等于负债加权益 |
| 勾稽校验有失败项 | 看 `02_validation_report.json` 里失败项的 `difference` 与 `evidence` |

**不要为了让流水线跑通而删掉检查或放宽约束** —— 那会把问题藏进报告里。
所有报错信息都写明了是哪个文件的哪个字段。
