#!/bin/bash
# 启动 bot（launchd 已安装的前提下；没装先跑 scripts/install-launchd.sh）
launchctl kickstart "gui/$(id -u)/org.chenyun.hanyan" && echo "✅ 已启动" || {
    echo "⚠️ 启动失败——可能还没安装 launchd 服务，先执行: bash scripts/install-launchd.sh"
    exit 1
}
