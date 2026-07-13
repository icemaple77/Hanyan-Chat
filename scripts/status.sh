#!/bin/bash
# 查看 bot 运行状态
LABEL="org.chenyun.hanyan"
INFO=$(launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null) || {
    echo "❌ launchd 服务未安装（先执行: bash scripts/install-launchd.sh）"
    exit 1
}
PID=$(echo "$INFO" | awk '/^\s*pid = /{print $3}')
if [ -n "$PID" ]; then
    echo "✅ 运行中 (PID $PID)"
    ps -o pid,etime,rss,command -p "$PID" | sed 's/^/   /'
else
    echo "⏸  已安装但未在运行"
    echo "$INFO" | grep -E "last exit (code|reason)" | sed 's/^/   /'
fi
