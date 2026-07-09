#!/usr/bin/env python3
"""
Hanyan Chat WebUI — 入口脚本

    python webui.py

需要额外安装 Flask（见 requirements.txt）。WebUI 和 bot 主进程是两个独立进程，
可以只在需要改配置/看记忆的时候临时启动，不需要一直跟 bot 一起跑。
"""

from hanyan.webui import main

if __name__ == "__main__":
    main()
