"""允许 `python -m eval_harness` 直接启动 CLI（含 --web 控制台）。"""
from .cli import main

if __name__ == "__main__":
    main()
