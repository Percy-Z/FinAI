#!/bin/bash
# 双击我就能打开 FinAI 工作台。
#
# macOS 的 Finder 会在终端里执行 .command 文件 —— 这就是「双击可运行」的入口，
# 不需要记任何命令。关掉这个终端窗口，服务就停了。
#
# 想放到桌面或 Dock：在 Finder 里按住 ⌥⌘ 拖拽做别名，或者把整个文件拖到 Dock 右侧。

cd "$(dirname "$0")" || exit 1

echo "=========================================="
echo "  FinAI 自动化估值工作台"
echo "=========================================="
echo
echo "工作目录：$(pwd)"
echo

# 已经在跑就直接打开浏览器 —— 双击两次不该报「端口被占用」那种看不懂的错。
if curl -s -m 2 -o /dev/null "http://127.0.0.1:8770/api/status"; then
  echo "工作台已经在运行，直接打开浏览器。"
  open "http://127.0.0.1:8770/"
  echo "（如果要用新端口，在终端里跑：python3 -m pipeline.web --port 9000）"
  sleep 1
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "找不到 python3。请先安装 Python 3.11 或更高版本："
  echo "    https://www.python.org/downloads/"
  echo
  read -n 1 -s -r -p "按任意键关闭…"
  exit 1
fi

python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    print(f"需要 Python 3.11 或更高版本，当前是 {sys.version.split()[0]}")
    print("（3.11 之前没有 tomllib，配置层读不了 TOML）")
    raise SystemExit(1)
PY
if [ $? -ne 0 ]; then
  read -n 1 -s -r -p "按任意键关闭…"
  exit 1
fi

# 缺可选依赖只影响效果（图表/PDF），不影响能否启动 —— 只说一句，不拦人。
python3 - <<'PY'
import importlib.util, shutil
missing = [n for n in ("matplotlib", "reportlab", "pdfplumber")
           if importlib.util.find_spec(n) is None]
if not (shutil.which("xelatex") or shutil.which("lualatex")):
    missing.append("LaTeX")
if missing:
    print("提示：这些可选依赖没装，报告会缺对应部分 →", "、".join(missing))
    print("      装上更完整：python3 -m pip install matplotlib reportlab pdfplumber")
    print()
PY

echo "浏览器会自动打开；没打开就手动访问 http://127.0.0.1:8770/"
echo "按 Control-C 停止服务。"
echo

python3 -m pipeline.web

echo
read -n 1 -s -r -p "服务已停止，按任意键关闭…"
