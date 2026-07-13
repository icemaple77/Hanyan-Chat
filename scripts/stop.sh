#!/bin/bash
# 停止 bot（发 SIGTERM，bot 会优雅关闭；干净退出后 launchd 不会拉起）
launchctl stop org.chenyun.hanyan 2>/dev/null
echo "✅ 已发送停止信号（bot 会优雅关闭）"
