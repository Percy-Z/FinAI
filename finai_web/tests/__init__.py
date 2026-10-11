"""取数层的单元测试。

跑法（在仓库根目录）：

    python -m unittest discover -s finai_web/tests

设计约定：**不联网、不依赖第三方库**。网络调用集中在 cninfo / firecrawl /
downloader 三处，它们都接受 `transport` 参数，测试里换成假实现 ——
这样「解析与拼装逻辑」在没有网络、没装 pdfplumber 的机器上也能验。
"""
