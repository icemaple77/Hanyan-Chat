#!/bin/bash
# 实时看 bot 日志（Ctrl+C 退出）
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tail -f "$PROJECT_DIR/data/hanyan.log"
