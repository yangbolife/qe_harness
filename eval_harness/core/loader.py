"""eval_harness · 用例加载器（声明式）

离线四件套：smoke / regression / behavior / safety。
v1 支持 JSONL；YAML 在 LOADERS 注册即可扩展（不破练习3 手感）。
"""
from __future__ import annotations

import json
import os

from .models import Case
from .registry import LOADERS


@LOADERS.register("jsonl")
def load_jsonl(path: str) -> list[Case]:
    cases: list[Case] = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            cases.append(Case.from_dict(json.loads(line), idx=i))
    return cases


@LOADERS.register("yaml")
def load_yaml(path: str) -> list[Case]:
    import yaml  # 可选依赖，用到才 import
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    items = data if isinstance(data, list) else data.get("cases", [])
    return [Case.from_dict(d, idx=i) for i, d in enumerate(items)]


def load_suite(path: str) -> list[Case]:
    """按扩展名自动选择加载器。"""
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    if ext not in LOADERS.available():
        # 默认按 jsonl 尝试
        ext = "jsonl"
    return LOADERS.get(ext)(path)
