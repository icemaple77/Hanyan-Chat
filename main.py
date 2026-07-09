#!/usr/bin/env python3
"""
Hanyan Chat — 入口脚本

    python main.py

具体逻辑在 hanyan/ 包里，这个文件只是一个薄薄的启动壳，方便直接
`python main.py` 或用 systemd/supervisor 指向这个文件启动。
"""

from hanyan.bot import main

if __name__ == "__main__":
    main()
