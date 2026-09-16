#!/data/data/com.termux/files/usr/bin/bash
# 本地语音引擎 —— 后台启动 / 重启
#
# 用法（注意 /sdcard 是 noexec，必须用 bash 调用）：
#     bash start.sh          # 在本目录下执行
#
# 它会：停掉旧实例 -> 拿 wakelock -> 脱离终端启动 -> 等健康检查通过 -> 打印状态

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${PORT:-8790}"
LOG="${TMPDIR:-/tmp}/tts_server.log"

cd "$DIR" || { echo "进不去目录: $DIR"; exit 1; }

echo "== 本地语音引擎 =="
echo "目录: $DIR"

# ---- 1. 停掉旧实例 ----
# 用独立脚本做匹配：只认「python 解释器 + tts_server.py」，并排除整条祖先链。
# （不能直接用 pkill -f，它会把调用者自己的 shell 也杀掉）
python3 "$DIR/stopsrv.py"

# ---- 2. 拿 wakelock，防 Android 清理 ----
if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock 2>/dev/null && echo "  wakelock 已获取"
fi

# ---- 3. 脱离终端启动 ----
setsid nohup python3 -u "$DIR/tts_server.py" --port "$PORT" > "$LOG" 2>&1 < /dev/null &
disown 2>/dev/null || true
echo "  已后台启动，日志: $LOG"

# ---- 4. 等健康检查通过 ----
printf "  等待就绪"
ok=0
for _ in $(seq 1 40); do
    sleep 1
    printf "."
    H="$(curl -s -m 3 "http://127.0.0.1:$PORT/health" 2>/dev/null)"
    if echo "$H" | grep -q '"worker": *true'; then
        ok=1
        break
    fi
done
echo

if [ "$ok" = "1" ]; then
    echo
    echo "✅ 已就绪: http://127.0.0.1:$PORT"
    echo "$H"; echo
    echo "   音色列表: curl -s http://127.0.0.1:$PORT/voices"
    echo "   自检:     curl -o $TMPDIR/t.mp3 'http://127.0.0.1:$PORT/tts?text=你好'"
    echo "   日志:     tail -f $LOG"
    echo "   新增音色: 在 voices/ 下建文件夹放 ref.wav + ref.txt，不用重启"
else
    echo
    echo "❌ 启动失败，最后 20 行日志:"
    tail -20 "$LOG" 2>/dev/null
    exit 1
fi
