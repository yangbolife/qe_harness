"""eval_harness · 动态 curated 产题 + 版本钉（创新④）

痛点（MMLU 已饱和、decodethefuture 2026：单次跑虚高 5–15pp、训练污染不可见）：
- 静态基准会饱和、被污染，分数失去区分度。
- 单次运行不可复现，无法追溯「这次用的哪版数据集 / 哪版模型」。

本模块：
- generate_curated(seed, n)：基于模板库 + 种子**确定性**生成数据集（同 seed 同结果，可复现）。
- version_of(cases)：对数据集内容做哈希，作为「数据集版本钉」，落 PG case_sets，
  保证任何一次 run 都能追溯到确切的数据集版本（去污染/防漂移）。
- pin_dataset(...)：把版本钉写入持久化层（配合 persistence.Database.save_case_set）。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
from typing import Optional

# 模板库（中文事实类，避免与训练集高度重叠——演示用，生产可替换为业务私有题）
_TEMPLATES = [
    ("中国的首都是哪里？", "北京"),
    ("水的化学式是什么？", "H2O"),
    ("光速约是多少？", "30万公里/秒"),
    ("人体最大的器官是？", "皮肤"),
    ("珠穆朗玛峰大约多高？", "8848米"),
    ("中国的国宝动物是？", "熊猫"),
    ("太阳系最大的行星是？", "木星"),
    ("1+1 等于几？", "2"),
    ("《红楼梦》的作者是？", "曹雪芹"),
    ("一年有多少个月？", "12"),
    ("二进制中 1+1 等于几？", "10"),
    ("氧气的化学式是？", "O2"),
    ("黄金的化学符号是？", "Au"),
    ("铁的化学符号是？", "Fe"),
    ("DNA 的中文全称是？", "脱氧核糖核酸"),
    ("光合作用主要发生在植物哪个结构？", "叶绿体"),
    ("中国的身份证号码通常有多少位？", "18位"),
    ("一周有几天？", "7天"),
    ("长江是中国第几长河？", "第一长河"),
    ("圆周率 π 约等于几？", "3.14"),
]


def generate_curated(seed: int, n: int, path: Optional[str] = None,
                     suite: str = "regression", category: str = "curated") -> list[dict]:
    """确定性生成 n 条 curated 用例。同 seed → 同结果（可复现）。"""
    rng = random.Random(seed)
    out: list[dict] = []
    for i in range(n):
        q, a = rng.choice(_TEMPLATES)
        out.append({
            "id": i, "question": q, "gold": a,
            "suite": suite, "category": category, "grader": "code",
        })
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for o in out:
                f.write(json.dumps(o, ensure_ascii=False) + "\n")
    return out


def version_of(cases: list[dict]) -> str:
    """对数据集内容做哈希 → 数据集版本钉（短哈希）。"""
    h = hashlib.sha256()
    for c in cases:
        h.update(json.dumps(c, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()[:16]


async def pin_dataset(db, name: str, cases: list[dict], source_path: str = "") -> str:
    """把数据集版本钉写入持久化层（PG case_sets）。返回 case_set id。"""
    return await db.save_case_set(
        name=name, version=version_of(cases), source_path=source_path,
        hashv=version_of(cases), meta={"n": len(cases)},
    )
