"""`python -m eval_harness.web` 的入口（app.py 文档里写的就是这条命令）。

startup.sh / 一键启动脚本与 README 都按 `python -m eval_harness.web` 调用，
缺了本文件该命令会报 `No module named eval_harness.web.__main__`。
"""
from __future__ import annotations

from .app import main

if __name__ == "__main__":
    main()
