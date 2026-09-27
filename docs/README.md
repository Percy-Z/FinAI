# 系统流程图

两张图，分别讲系统的整体结构和三表联动的机制。

| 文件 | 内容 |
| --- | --- |
| `architecture.mmd` / `architecture.png` | 系统架构与数据流：输入、数据层、校验层、算法层、输出层 |
| `three-statement.mmd` / `three-statement.png` | 三表联动机制：假设如何变成三张表，现金为什么最后算 |
| `mermaid-config.json` | 渲染配置，放开默认的文字换行宽度并指定中文字体 |

## 怎么看第一张图

从左到右五层。蓝色是输入（本地冻结文件，只读），虚线橙色是**还没做的部分**
（文档抽取和报告生成），绿色是已经实现的算法模块，紫色是各模块产出的文件。

三条主干值得记住。一条是数据进来先过 `validate.py` 做勾稽校验，输出
`validation_report.json`——这是通知里「财务勾稽关系」那一项的落地。一条是
`wacc.py` 和 `forecast.py` 一起喂给 `dcf.py`，算出企业价值和每股价值。最后一条是
`dcf.py` 和 `comps.py` 两条独立的估值线汇进足球场图，检查两个区间是否重叠，
不重叠说明至少有一边的假设有问题。

## 怎么看第二张图

关键词是**现金是模型的塞子**。除了现金，资产负债表上的每个科目都由假设驱动；
现金则由现金流量表推出来，最后回填进资产负债表。这样资产必然等于负债加所有者权益，
不需要事后手动调平。

图里还标了两条容易被忽略的联动：固定资产的滚存（期初 + 资本开支 − 折旧）会反过来
影响利润表的折旧摊销；营运资本的变动同时出现在资产负债表和现金流量表里。

平衡的充要条件是「其他非流动资产变动 = 其他非流动负债变动」。这两项在本模型里
保持期初水平不变，所以差额恒为零。以后如果让它们随收入增长，两边必须同幅增长，
否则表会不平。

## 重新渲染

改完 `.mmd` 之后，在仓库根目录执行：

```bash
cd docs
PUPPETEER_EXECUTABLE_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  npx -y @mermaid-js/mermaid-cli -c mermaid-config.json \
  -i architecture.mmd -o architecture.png -b white -s 2
```

`-c mermaid-config.json` 不能省：mermaid 默认会把节点文字按 200 像素折行，中文会被切得很难看。
把 `architecture` 换成 `three-statement` 就渲染第二张图。

也可以把 `.mmd` 的内容直接粘到 <https://mermaid.live> 上导出 SVG，那样放进文档里更清晰。
