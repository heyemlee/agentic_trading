#!/bin/zsh
set -euo pipefail

# 切换到项目根目录
cd "$(dirname "$0")"

PORT=8787
LABEL="com.local.agentic-trading.api"

echo "=== 正在检查并清理旧进程 ==="

# 1. 如果有 launchd 守护进程托管，先将其卸载（否则 kill 掉后 launchd 会因为 KeepAlive 自动拉起）
if launchctl list 2>/dev/null | grep -q "$LABEL"; then
    echo "发现 launchd 托管服务 $LABEL，正在停用..."
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
fi

# 2. 检查端口占用并杀死残留进程
OLD_PIDS=$(lsof -ti :"$PORT" 2>/dev/null || true)
if [[ -n "$OLD_PIDS" ]]; then
    echo "端口 $PORT 被以下进程占用: $OLD_PIDS，正在终止..."
    echo "$OLD_PIDS" | xargs kill -9 2>/dev/null || true
    sleep 1
fi

# 再次确认端口已释放
REMAINING=$(lsof -ti :"$PORT" 2>/dev/null || true)
if [[ -n "$REMAINING" ]]; then
    echo "警告: 端口 $PORT 仍被进程占用: $REMAINING，尝试强制清理..."
    echo "$REMAINING" | xargs kill -9 2>/dev/null || true
    sleep 1
fi

echo "=== 端口 $PORT 已就绪 ==="
echo "控制台地址: http://127.0.0.1:$PORT/ui"
echo "API 地址:   http://127.0.0.1:$PORT"
echo "正在前台启动 ct-api (按 Ctrl+C 可停止)..."
echo ""

exec uv run ct-api "$@"
