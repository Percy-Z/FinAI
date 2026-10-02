# FinAI 常用命令
#
# 在仓库根目录执行 make <目标>。不加目标时显示这份说明。
#
# 注意：Makefile 的缩进必须用 TAB，不能用空格。
# 这是 make 最著名的坑，如果报 "missing separator"，先检查缩进。

PYTHON ?= python3

.DEFAULT_GOAL := help
.PHONY: help config run report test test-algorithm test-pipeline verify clean


help:
	@echo "FinAI 常用命令："
	@echo ""
	@echo "  make config    只读配置并自检，不跑流水线"
	@echo "  make run       跑完整条流水线"
	@echo "  make report    只重新生成报告（用最近一次运行的产物）"
	@echo "  make test      跑全部单元测试"
	@echo "  make verify    提交前的全量检查（测试 + 配置 + 完整跑一遍）"
	@echo "  make clean     删掉运行产物（runs/ 和 logs/）"
	@echo ""
	@echo "  可以指定解释器：make run PYTHON=python3.12"


# 只读配置，构造出各个数据类对象并打印。配置有问题时会在这里暴露。
config:
	$(PYTHON) -m pipeline.config


# 跑完整条流水线：读配置 → 各阶段 → 落盘 → 写日志。
run:
	$(PYTHON) -m pipeline.run


# 只重新生成报告三件套：从最近一次运行的产物读，不重跑计算 —— 改措辞时用这个。
report:
	$(PYTHON) -m pipeline.report
	$(PYTHON) -m pipeline.workbook


# 全部单元测试。算法层与框架层分开，方便出问题时定位是哪一边。
test: test-algorithm test-pipeline


# 算法层：345 个单元测试（其中 22 个需要 matplotlib，没装则跳过）。
# -s 参数不能省，algorithm 是命名空间包，不指定会找不到测试。
test-algorithm:
	$(PYTHON) -m unittest discover -s algorithm/tests


# 框架层：82 个测试，验的是「装配」——配置读得对不对、串起来跑不跑得通、
# 出错时有没有拦住并留下记录。测试目录跟着框架代码走，所以是 pipeline/tests。
test-pipeline:
	@if [ -d pipeline/tests ]; then \
		$(PYTHON) -m unittest discover -s pipeline/tests; \
	else \
		echo "（框架层还没有 pipeline/tests/ 目录，跳过）"; \
	fi


# 提交前跑这个。三件事都通过才算没问题：
#   1. 测试全过     → 没有碰坏算法层
#   2. 配置读得出来  → 配置文件没写错
#   3. 流水线跑得通  → 端到端没有运行时错误
verify: test config run
	@echo ""
	@echo "✓ 全部检查通过"


# 删掉运行产物。日志和产物都是每次运行自动生成的，删掉不影响代码。
# 加了守卫：只在仓库根目录才执行，避免在别处误删。
clean:
	@test -d algorithm && test -f Makefile || (echo "请在仓库根目录执行 make clean"; exit 1)
	rm -rf runs/* logs/*
	@echo "已清空 runs/ 和 logs/（algorithm/runs/ 里的示例产物保留）"
