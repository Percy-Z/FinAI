# FinAI 常用命令
#
# 在仓库根目录执行 make <目标>。不加目标时显示这份说明。
#
# 注意：Makefile 的缩进必须用 TAB，不能用空格。
# 这是 make 最著名的坑，如果报 "missing separator"，先检查缩进。

PYTHON ?= python3

.DEFAULT_GOAL := help
.PHONY: help config run test test-algorithm test-pipeline test-mcp lint check verify clean


help:
	@echo "FinAI 常用命令："
	@echo ""
	@echo "  make config    只读配置并自检，不跑流水线"
	@echo "  make run       跑完整条流水线"
	@echo "  make test      跑全部单元测试"
	@echo "  make lint      静态检查：mypy + pyflakes + pyright（严格模式）"
	@echo "  make check     验收最近一次运行的产物（文件齐不齐、报告有没有图）"
	@echo "  make verify    提交前的全量检查（测试 + 配置 + 完整跑一遍 + 验收产物）"
	@echo "  make clean     删掉运行产物（runs/ 和 logs/）"
	@echo ""
	@echo "  可以指定解释器：make run PYTHON=python3.12"


# 只读配置，构造出各个数据类对象并打印。配置有问题时会在这里暴露。
config:
	$(PYTHON) -m pipeline.config


# 跑完整条流水线：读配置 → 各阶段 → 落盘 → 写日志。
run:
	$(PYTHON) -m pipeline.run


# 全部单元测试。算法层与框架层分开，方便出问题时定位是哪一边。
test: test-algorithm test-pipeline test-mcp


# 算法层：345 个单元测试（其中 22 个需要 matplotlib，没装则跳过）。
# -s 参数不能省，algorithm 是命名空间包，不指定会找不到测试。
test-algorithm:
	$(PYTHON) -m unittest discover -s algorithm/tests


# 框架层：42 个测试，验的是「装配」——配置读得对不对、串起来跑不跑得通、
# 出错时有没有拦住并留下记录。测试目录跟着框架代码走，所以是 pipeline/tests。
test-pipeline:
	@if [ -d pipeline/tests ]; then \
		$(PYTHON) -m unittest discover -s pipeline/tests; \
	else \
		echo "（框架层还没有 pipeline/tests/ 目录，跳过）"; \
	fi


# MCP 层：验协议实现 —— 握手、工具清单、调用与错误处理。
test-mcp:
	@if [ -d mcp_server/tests ]; then \
		$(PYTHON) -m unittest discover -s mcp_server/tests; \
	else \
		echo "（还没有 mcp_server/tests/，跳过）"; \
	fi


# 静态检查。项目一直保持三个检查器全绿，所以把它做成一条命令，
# 而不是靠某个人记得手动敲 —— 忘了跑就会慢慢积起来。
# pyright 走 npx（需要网络拉一次），没有 npx 时跳过并提示，不算失败。
lint:
	$(PYTHON) -m mypy algorithm pipeline mcp_server
	$(PYTHON) -m pyflakes algorithm pipeline mcp_server
	@if command -v npx >/dev/null 2>&1; then \
		npx --yes pyright; \
	else \
		echo "（没有 npx，跳过 pyright；装了 node 就能跑）"; \
	fi


# 提交前跑这个。三件事都通过才算没问题：
#   1. 测试全过     → 没有碰坏算法层
#   2. 配置读得出来  → 配置文件没写错
#   3. 流水线跑得通  → 端到端没有运行时错误
# 验收最近一次运行的产物。
# 为什么单独有这一步：图表依赖 matplotlib，缺了会被「跳过并记一条警告」。
# 这个降级本身合理，但它会悄悄产出一份没有图的报告 —— 那就必须有人拦住。
check:
	$(PYTHON) -m pipeline.check_run


# 提交前跑这个。四件事都通过才算没问题：
#   1. 测试全过      → 没有碰坏算法层与框架层
#   2. 配置读得出来   → 配置文件没写错
#   3. 流水线跑得通   → 端到端没有运行时错误
#   4. 产物验得出来   → 文件齐、报告里有图、清单里的警告都摆到台面上
# 五件事：
#   1. 静态检查干净   → 没有类型错误与未定义名字
#   2. 测试全过       → 没碰坏算法层与框架层
#   3. 配置读得出来   → 配置文件没写错
#   4. 流水线跑得通   → 端到端没有运行时错误
#   5. 产物验得出来   → 文件齐、报告里有图、清单里的警告都摆到台面上
verify: lint test config run check
	@echo ""
	@echo "✓ 全部检查通过"


# 删掉运行产物。日志和产物都是每次运行自动生成的，删掉不影响代码。
# 加了守卫：只在仓库根目录才执行，避免在别处误删。
clean:
	@test -d algorithm && test -f Makefile || (echo "请在仓库根目录执行 make clean"; exit 1)
	rm -rf runs/* logs/*
	@echo "已清空 runs/ 和 logs/（algorithm/runs/ 里的示例产物保留）"
