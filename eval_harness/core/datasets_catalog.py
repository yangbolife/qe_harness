"""eval_harness · 评测数据集目录（catalog）加载与解析

为评测引擎提供「可预置开源评测数据集」的统一注册表：
- catalog.json 定义每个数据集的元信息（分级 / 来源 / 许可 / 决策字段：用例数、
  估算耗时 / token / 适用场景 / 推荐评分器 / 是否需执行环境）
- 本模块负责：加载 catalog、按分级分组、解析数据集文件路径（datasets/ 优先，
  examples/ 兜底）、为 CLI 与 Web 提供统一 API。

设计原则（Phase 0）：
- catalog 是「数据集注册表」，不是数据本身——可跑集带 suite_file 指向真实 JSONL，
  环境类（专家层）仅元数据占位（requires_env=true, suite_file=null）。
- 元信息里的耗时/token 为「模型相关估算值」，真实跑后由引擎实测回写校准。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent            # eval_harness/core
PKG = HERE.parent                                 # eval_harness
DATASETS_DIR = PKG / "datasets"
EXAMPLES_DIR = PKG / "examples"
CATALOG_PATH = DATASETS_DIR / "catalog.json"

# 粗→细分级顺序，供 UI 与 CLI 排序
TIER_ORDER = ["basic", "advanced", "deep", "expert"]
TIER_LABELS = {
    "basic": "基础",
    "advanced": "进阶",
    "deep": "深度",
    "expert": "专家(环境)",
}

# 数据集状态：ready=文件已就位可跑；env=需执行环境（占位）；planned=已登记待拉取/构建
STATUS_LABELS = {
    "ready": "可运行",
    "env": "需环境(占位)",
    "planned": "已登记待拉取",
}


def _load_catalog_raw() -> dict:
    if not CATALOG_PATH.exists():
        return {"datasets": []}
    with open(CATALOG_PATH, encoding="utf-8") as f:
        return json.load(f)


_CATALOG_CACHE: Optional[dict] = None


def load_catalog() -> dict:
    global _CATALOG_CACHE
    if _CATALOG_CACHE is None:
        _CATALOG_CACHE = _load_catalog_raw()
    return _CATALOG_CACHE


def invalidate_cache() -> None:
    global _CATALOG_CACHE
    _CATALOG_CACHE = None


def get_entry(id_or_name: str) -> Optional[dict]:
    for d in load_catalog().get("datasets", []):
        if d.get("id") == id_or_name or d.get("name") == id_or_name:
            return d
    return None


def resolve_dataset_path(suite: str) -> str:
    """把 suite（catalog id / 文件名 / 绝对路径）解析为可加载的 JSONL 路径。

    解析顺序：绝对路径 → catalog 条目 suite_file（datasets/ 与 examples/ 都查）
    → 裸文件名（datasets/ 与 examples/ 都查）。保证历史 examples/*.jsonl 仍可直跑。
    """
    p = Path(suite)
    if p.is_absolute() and p.exists():
        return str(p.resolve())

    entry = get_entry(suite)
    names: list[str] = []
    if entry and entry.get("suite_file"):
        names.append(entry["suite_file"])
    names.append(suite)  # 也允许直接传文件名

    for base in (DATASETS_DIR, EXAMPLES_DIR):
        for nm in names:
            cand = base / nm
            if cand.exists():
                return str(cand.resolve())
    raise FileNotFoundError(f"找不到用例集：{suite}（不在 catalog/datasets/examples 下）")


def _count_cases(path: Path) -> int:
    n = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    n += 1
    except Exception:
        return 0
    return n


def list_datasets() -> list[dict]:
    """返回富集后的数据集列表（带实际用例数、是否可跑、状态）。

    可跑集的 suite_file 可能位于 datasets/ 或 examples/（历史集成风险面探针在 examples/），
    两处都识别。找不到文件则标记为 planned（待拉取/构建）。
    """
    out: list[dict] = []
    for d in load_catalog().get("datasets", []):
        e = dict(d)
        tier = e.get("tier", "other")
        e.setdefault("tier_label", TIER_LABELS.get(tier, tier))

        requires_env = bool(d.get("requires_env"))
        if requires_env or not d.get("suite_file"):
            e["runnable"] = False
            e["status"] = "env" if requires_env else "planned"
            e["actual_cases"] = int(d.get("cases_count") or 0)
        else:
            fp = _locate_suite_file(d["suite_file"])
            if fp is not None:
                e["runnable"] = True
                e["status"] = "ready"
                e["actual_cases"] = _count_cases(fp)
                e["file_path"] = str(fp)
            else:
                e["runnable"] = False
                e["status"] = "planned"
                e["actual_cases"] = int(d.get("cases_count") or 0)
        e.setdefault("status_label", STATUS_LABELS.get(e["status"], e["status"]))
        out.append(e)
    return out


def _locate_suite_file(name: str) -> Optional[Path]:
    """在 datasets/ 与 examples/ 两处查找 suite 文件，返回首个存在的路径。"""
    for base in (DATASETS_DIR, EXAMPLES_DIR):
        cand = base / name
        if cand.exists():
            return cand
    return None


def group_by_tier() -> dict:
    groups: dict[str, list] = {t: [] for t in TIER_ORDER}
    groups["other"] = []
    for e in list_datasets():
        groups.get(e.get("tier"), "other").append(e)
    return {k: v for k, v in groups.items() if v or k in TIER_ORDER}


def list_fixtures() -> list[dict]:
    """examples/ 下、未被 catalog 收录的开发/演示用 fixtures（smoke/trajectory 等）。"""
    cat_names = {d.get("suite_file") for d in load_catalog().get("datasets", [])}
    cat_names.discard(None)
    out = []
    if not EXAMPLES_DIR.exists():
        return out
    for f in sorted(EXAMPLES_DIR.glob("*.jsonl")):
        if f.name in cat_names:
            continue
        out.append({
            "name": f.name,
            "path": str(f),
            "cases": _count_cases(f),
            "note": "开发/演示 fixtures（非基准数据集）",
        })
    return out


def summary() -> dict:
    ds = list_datasets()
    return {
        "total": len(ds),
        "ready": sum(1 for d in ds if d["status"] == "ready"),
        "env": sum(1 for d in ds if d["status"] == "env"),
        "planned": sum(1 for d in ds if d["status"] == "planned"),
        "tiers": {t: len(v) for t, v in group_by_tier().items()},
    }
