"""FinAI 项目代码包。

这个文件的存在本身比它的内容重要：没有它，`algorithm` 就是命名空间包，
而 `finai` 又自带 `__init__.py`，类型检查器会把同一个文件同时当成
`finai.comps` 和 `algorithm.finai.comps` 两个模块，于是满屏「导入无法解析」
和 `Source file found twice under different module names`。

子包：
    finai   算法层
    output  输出层与图表层
    examples 演示脚本
    tests    单元测试
"""

__all__ = ["finai", "output"]
