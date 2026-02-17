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

# 2. 检查端口占用并结束残留进程
# 只找正在监听这个端口的进程。不加 -sTCP:LISTEN 时，lsof 会把任何与 8787 有过连接（包括已关闭的
# 旧连接）的进程都列出来，例如浏览器或其他应用的辅助进程，脚本随后会对它们发 kill -9。
listeners() { lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true; }

OLD_PIDS=$(listeners)
if [[ -n "$OLD_PIDS" ]]; then
    echo "端口 $PORT 正被以下进程监听: $OLD_PIDS，正在请求退出..."
    # 先 TERM：API 持有券商会话，launchd 也给它 30 秒关闭；直接 kill -9 会跳过这些清理。
    echo "$OLD_PIDS" | xargs kill 2>/dev/null || true
    for _ in {1..30}; do
        [[ -z "$(listeners)" ]] && break
        sleep 1
    done
fi

# 再次确认端口已释放
REMAINING=$(listeners)
if [[ -n "$REMAINING" ]]; then
    echo "警告: 端口 $PORT 在 30 秒后仍被监听: $REMAINING，强制结束..."
    echo "$REMAINING" | xargs kill -9 2>/dev/null || true
    sleep 1
fi

echo "=== 端口 $PORT 已就绪 ==="
echo "控制台地址: http://127.0.0.1:$PORT/ui"
echo "API 地址:   http://127.0.0.1:$PORT"
echo "正在前台启动 ct-api (按 Ctrl+C 可停止)..."
echo ""

exec uv run ct-api "$@"
