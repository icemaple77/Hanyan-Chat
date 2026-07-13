#!/bin/bash
# launchd 实际执行的前台入口。也可以手动跑：bash scripts/run.sh
# launchd 环境的 PATH 极简（没有 homebrew），ffmpeg / python3 都可能找不到，
# 这里显式补全。
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

exec python3 main.py
