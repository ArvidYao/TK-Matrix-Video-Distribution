#!/bin/bash
# TK 视频分发工具 - Web 版
# 双击启动 Web 服务，自动打开浏览器

cd "$(dirname "$0")"

# ★ 确保 Homebrew 工具在 PATH 中
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
clear
echo "=================================="
echo "  TK 视频分发工具 v1.0.9"
echo "=================================="
echo ""
echo "正在启动 Web 服务..."

# 优先使用 miniconda Python（有 yaml/flask 等依赖）
if [ -f "/Users/mac/miniconda3/bin/python3.12" ]; then
    PYTHON="/Users/mac/miniconda3/bin/python3.12"
elif command -v python3 &>/dev/null; then
    PYTHON="python3"
else
    echo "❌ 未找到 Python3，请安装 miniconda3"
    read -p "按任意键退出..." 
    exit 1
fi

# 启动 Flask Web 服务（后台运行）
$PYTHON web_app.py &
sleep 2

echo "正在打开浏览器..."
open http://localhost:5800

echo ""
echo "Web 服务已启动 ✅"
echo "浏览器已打开，可直接操作"
echo ""
echo "关闭方法：按 Ctrl+C 停止服务"
echo "或直接关闭此窗口"

# 等待后台进程
wait
