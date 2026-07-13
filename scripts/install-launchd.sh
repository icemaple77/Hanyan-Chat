#!/bin/bash
# 安装 launchd 服务：开机自启 + 崩溃自动拉起。只需要跑一次。
# 之后用 scripts/start.sh / stop.sh / status.sh 控制。
set -euo pipefail

LABEL="org.chenyun.hanyan"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/data"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$PROJECT_DIR/scripts/run.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <!-- 崩溃（非 0 退出）自动拉起；正常 stop（干净退出）不拉起 -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <!-- 崩溃重启间隔，防止启动即崩时疯狂循环 -->
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$PROJECT_DIR/data/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$PROJECT_DIR/data/launchd.err.log</string>
</dict>
</plist>
EOF

# 如果已加载过，先卸掉旧的再加载新的
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "✅ 已安装并启动。常用命令："
echo "   启动:   bash scripts/start.sh"
echo "   停止:   bash scripts/stop.sh"
echo "   状态:   bash scripts/status.sh"
echo "   看日志: bash scripts/logs.sh"
