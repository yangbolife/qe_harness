"""eval_harness · 数据集目录的写回（增 / 改 / 删 / 导入用例）

catalog.json 是元数据的单一事实来源，引擎与 Web 控制台都读它。本模块负责**安全地改写它**，
把「数据集管理」从只读视图升级为可增删改：

安全底线（每一条都对应一个真实会踩的坑）：
1. **原子写**：先写 `<name>.tmp` 再 `os.replace`。直接就地 `open(w)` 一旦中途异常，
   整个目录表就废了——38 条元数据比样本文件更难重建。
2. **写前备份**：`catalog.json.bak.<ts>`，保留最近若干份。删除 / 批量改动后能回滚。
3. **删除不真删文件**：样本文件移入 `datasets/.trash/<ts>/`，不 unlink。数据集是
   项目资产，误删 10 条人工构造的样本是无法从上游拉回来的。
4. **受控字段**：只允许改白名单字段；`id` 建后不可改；`file_sha256`/`fetched_at`
   归 `fetch_datasets.py --pin` 管，不接受手改（否则版本钉失去意义）。
5. **受控枚举**：`tier` 必须落在 TIER_ORDER，`domain`/`capabilities` 必须落在
   taxonomy 的受控枚举内——否则分类又会退回碎片化。
6. **写后失效缓存**：`datasets_catalog` 有进程内缓存，改完必须 invalidate，否则
   界面刷新还是旧数据（这个坑很隐蔽：写成功、返回 200，但列表纹丝不动）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from . import datasets_catalog as dc
from . import datasets_taxonomy as tax

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")

# 允许通过管理界面编辑的字段（id 不在此列——建后不可改，避免破坏历史 run 的引用）
EDITABLE_FIELDS = {
    "name", "tier", "domain", "capabilities", "category",
    "description", "scenario", "access", "source", "official_url", "aligned_to",
    "license", "commercial_use", "version", "updated", "data_format",
    "recommended_grader", "recommends_judge", "full_scale",
    "est_duration_min", "est_tokens", "languages",
    "requires_env", "env_notes", "suite_file", "cases_count",
    "source_kind", "sample_note", "sample_built", "object_axes", "dimensions",
}

# 建新条目时必须给的字段
REQUIRED_ON_CREATE = ("id", "name", "tier")

# 各字段的期望类型（用于把界面传来的字符串归一化；None 表示随业务判断）
_STR_FIELDS = {
    "name", "tier", "domain", "category", "description", "scenario", "access",
    "source", "official_url", "aligned_to", "license", "version", "updated",
    "data_format", "recommended_grader", "full_scale", "env_notes", "suite_file",
    "source_kind", "sample_note", "sample_built",
}
_INT_FIELDS = {"est_duration_min", "est_tokens", "cases_count"}
_BOOL_FIELDS = {"commercial_use", "recommends_judge", "requires_env"}
_LIST_FIELDS = {"capabilities", "languages", "object_axes", "dimensions"}

MAX_BACKUPS = 20


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def _now_tag() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _backup() -> Optional[str]:
    """写前备份 catalog.json，返回备份路径（未变更过则返回 None）。"""
    p = dc.CATALOG_PATH
    if not p.exists():
        return None
    dst = p.with_name(f"{p.name}.bak.{_now_tag()}")
    shutil.copy2(p, dst)
    # 只保留最近 MAX_BACKUPS 份，避免备份目录无限膨胀
    backups = sorted(p.parent.glob(f"{p.name}.bak.*"))
    for old in backups[:-MAX_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass
    return str(dst)


def _atomic_write(cat: dict, *, backup: bool = True) -> Optional[str]:
    p = dc.CATALOG_PATH
    bak = _backup() if backup else None
    tmp = p.with_name(f"{p.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cat, f, ensure_ascii=False, indent=1)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    dc.invalidate_cache()
    return bak


def _sort(cat: dict) -> None:
    """按档位 + id 稳定排序，保证文件 diff 可读。"""
    order = {t: i for i, t in enumerate(dc.TIER_ORDER)}
    cat["datasets"].sort(key=lambda d: (order.get(d.get("tier", ""), 99), d.get("id", "")))


def _load() -> dict:
    return dc._load_catalog_raw()


# ---------------------------------------------------------------------------
# 归一化与校验
# ---------------------------------------------------------------------------
def _coerce(field: str, value: Any) -> Any:
    """把界面传来的值归一化成该字段的期望类型。空串一律视为「清空」。"""
    if field in _LIST_FIELDS:
        if value is None:
            return []
        if isinstance(value, str):
            return [s.strip() for s in re.split(r"[,，\s]+", value) if s.strip()]
        if isinstance(value, list):
            return [str(x).strip() for x in value if str(x).strip()]
        return []
    if value is None:
        return None
    if field in _INT_FIELDS:
        s = str(value).strip()
        if s == "":
            return None
        try:
            return int(float(s))
        except (TypeError, ValueError):
            raise ValueError(f"字段 {field} 需要整数，收到：{value!r}")
    if field in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "是")
    s = str(value).strip()
    return s or None


def normalize(patch: dict) -> dict:
    """只保留可编辑字段并归一化类型。"""
    out: dict = {}
    for k, v in (patch or {}).items():
        if k not in EDITABLE_FIELDS:
            continue
        out[k] = _coerce(k, v)
    return out


def validate(entry: dict, *, creating: bool) -> list[str]:
    """校验一个条目是否可入库，返回错误信息列表（空表示通过）。"""
    errs: list[str] = []
    if creating:
        did = str(entry.get("id") or "")
        if not ID_RE.match(did):
            errs.append("id 只能用小写字母/数字开头，含 . _ - ，长度 2–64（如 my-dataset-v1）")
    if not str(entry.get("name") or "").strip():
        errs.append("name 不能为空")
    tier = entry.get("tier")
    if tier not in dc.TIER_ORDER:
        errs.append(f"tier 必须是 {dc.TIER_ORDER} 之一，收到：{tier!r}")
    dom = entry.get("domain")
    if dom and not tax.is_domain(dom):
        errs.append(f"domain 不在受控枚举内：{dom!r}")
    caps = entry.get("capabilities") or []
    bad = [c for c in caps if not tax.is_capability(c)]
    if bad:
        errs.append(f"capabilities 含未登记的标签：{bad}")
    if len(caps) > 6:
        errs.append("capabilities 最多 6 个（界面按 4 个以内展示更清晰）")
    for f in ("est_duration_min", "est_tokens"):
        v = entry.get(f)
        if isinstance(v, int) and v < 0:
            errs.append(f"{f} 不能为负")
    langs = entry.get("languages") or []
    if langs and not all(isinstance(x, str) for x in langs):
        errs.append("languages 必须是字符串列表")
    return errs


# ---------------------------------------------------------------------------
# 增
# ---------------------------------------------------------------------------
def create(fields: dict, cases: Optional[list[str]] = None,
           *, suite_file: Optional[str] = None) -> dict:
    """新建数据集条目；可选同时写入用例文件。

    cases 给的是 JSONL 行文本列表（每行一个 JSON 对象字符串）；给了就写文件并把
    cases_count 同步为实际条数，避免「登记 10 条、实际 0 条」这种不一致。
    """
    fields = dict(fields or {})
    did = str(fields.get("id") or "").strip()
    entry = normalize(fields)
    entry["id"] = did

    errs = validate(entry, creating=True)
    if errs:
        raise ValueError("；".join(errs))

    cat = _load()
    if any(d.get("id") == did for d in cat.get("datasets", [])):
        raise ValueError(f"数据集 id 已存在：{did}（如需修改请用编辑）")

    if not entry.get("domain"):
        entry["domain"] = tax.domain_of(entry)
    if not entry.get("capabilities"):
        entry["capabilities"] = tax.capabilities_of(entry)
    entry.setdefault("source_kind", "curated_sample")

    if cases:
        n = _write_cases_file(suite_file or f"{did}.jsonl", cases)
        entry["suite_file"] = suite_file or f"{did}.jsonl"
        entry["cases_count"] = n
    else:
        entry.setdefault("cases_count", 0)
        entry.setdefault("suite_file", suite_file or None)

    cat.setdefault("datasets", []).append(entry)
    _sort(cat)
    bak = _atomic_write(cat)
    return {"id": did, "backup": bak, "entry": entry}


# ---------------------------------------------------------------------------
# 改
# ---------------------------------------------------------------------------
def patch(dsid: str, fields: dict) -> dict:
    """改元数据。id 不可改；file_sha256 / fetched_at 不接受手改。"""
    cat = _load()
    idx = next((i for i, d in enumerate(cat.get("datasets", [])) if d.get("id") == dsid), None)
    if idx is None:
        raise KeyError(f"数据集不存在：{dsid}")

    incoming = dict(fields or {})
    if "id" in incoming and str(incoming["id"]).strip() != dsid:
        raise ValueError("id 不可修改（历史评测记录按 id 引用）；需要改名请新建后删除旧的")

    upd = normalize(incoming)
    if not upd:
        raise ValueError("没有可更新的字段（或字段都不在可编辑白名单内）")

    merged = {**cat["datasets"][idx], **upd}
    # tier 改了要重排，所以整体过一遍校验
    errs = validate(merged, creating=False)
    if errs:
        raise ValueError("；".join(errs))

    changed = {k: v for k, v in upd.items() if cat["datasets"][idx].get(k) != v}
    if not changed:
        return {"id": dsid, "changed": {}, "backup": None, "entry": cat["datasets"][idx]}

    cat["datasets"][idx] = merged
    _sort(cat)
    bak = _atomic_write(cat)
    return {"id": dsid, "changed": changed, "backup": bak, "entry": merged}


# ---------------------------------------------------------------------------
# 删
# ---------------------------------------------------------------------------
def delete(dsid: str, *, purge_file: bool = False,
           trash_dir: Optional[Path] = None) -> dict:
    """删除条目。purge_file=True 时把样本文件移入回收目录（不 unlink）。

    回收而非真删：样本文件多为人工构造，删掉不可从上游恢复；同时保留一条 .jsonl
    也能让误删后的回滚成本从「重写 10 道题」降到「mv 回来」。
    """
    cat = _load()
    before = len(cat.get("datasets", []))
    hit = next((d for d in cat.get("datasets", []) if d.get("id") == dsid), None)
    if hit is None:
        raise KeyError(f"数据集不存在：{dsid}")

    moved: Optional[str] = None
    sf = hit.get("suite_file")
    if purge_file and sf:
        src = dc._locate_suite_file(str(sf))
        if src and src.exists():
            base = trash_dir or (dc.DATASETS_DIR / ".trash")
            dst_dir = base / _now_tag()
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / src.name
            shutil.move(str(src), str(dst))
            moved = str(dst)

    cat["datasets"] = [d for d in cat["datasets"] if d.get("id") != dsid]
    bak = _atomic_write(cat)
    return {"id": dsid, "backup": bak, "removed": before - len(cat["datasets"]),
            "file_moved_to": moved, "entry": hit}


# ---------------------------------------------------------------------------
# 导入用例（JSONL）
# ---------------------------------------------------------------------------
def _validate_case_lines(lines: list[str]) -> list[str]:
    """逐行校验 JSONL 用例：可解析 + 有 id + 有 input。返回错误列表。"""
    errs: list[str] = []
    seen: set[str] = set()
    for i, line in enumerate(lines, 1):
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except Exception as ex:  # noqa: BLE001
            errs.append(f"第 {i} 行不是合法 JSON：{ex}")
            continue
        if not isinstance(obj, dict):
            errs.append(f"第 {i} 行不是 JSON 对象")
            continue
        cid = str(obj.get("id") or "")
        if not cid:
            errs.append(f"第 {i} 行缺少 id")
        elif cid in seen:
            errs.append(f"第 {i} 行 id 重复：{cid}")
        else:
            seen.add(cid)
        if obj.get("input") in (None, ""):
            errs.append(f"第 {i} 行 input 为空")
    return errs


def _write_cases_file(name: str, lines: list[str]) -> int:
    """写用例文件（覆盖）。返回写入的有效行数。"""
    safe = Path(str(name)).name  # 防目录穿越：只取文件名
    if not safe.endswith(".jsonl"):
        safe += ".jsonl"
    dst = dc.DATASETS_DIR / safe
    dst.parent.mkdir(parents=True, exist_ok=True)
    valid = [ln.strip() for ln in lines if ln.strip() and json.loads(ln.strip())]
    tmp = dst.with_name(dst.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for ln in valid:
            f.write(ln + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dst)
    return len(valid)


def import_cases(dsid: str, text: str, *, mode: str = "replace") -> dict:
    """导入用例。mode=replace 覆盖；append 追加。

    导入后自动同步 catalog 的 cases_count——否则「登记 10 条、磁盘 12 条」会一直
    在体检里报不一致（这正是 validate 端点会抓的事）。
    """
    cat = _load()
    idx = next((i for i, d in enumerate(cat.get("datasets", [])) if d.get("id") == dsid), None)
    if idx is None:
        raise KeyError(f"数据集不存在：{dsid}")

    entry = cat["datasets"][idx]
    lines = str(text or "").splitlines()
    errs = _validate_case_lines(lines)
    if errs:
        raise ValueError("用例校验失败：" + "；".join(errs[:8]))

    sf = str(entry.get("suite_file") or f"{dsid}.jsonl")
    if mode == "append":
        existing: list[str] = []
        cur = dc._locate_suite_file(sf)
        if cur and cur.exists():
            raw_lines = cur.read_text(encoding="utf-8").splitlines()
            existing = [ln.strip() for ln in raw_lines if ln.strip()]
            # 现有文件里的坏行必须单独报：混进「追加后校验失败（id 冲突？）」里，用户会去改
            # 刚粘贴的内容——而那段是对的，问题在磁盘上的文件里（预览页会把坏行标出来）。
            bad = _validate_case_lines(raw_lines)   # 传未过滤行，报的行号才和文件一致
            if bad:
                raise ValueError(
                    f"现有用例文件（{Path(sf).name}）有 {len(bad)} 处问题，无法追加："
                    + "；".join(bad[:5])
                    + "　→ 请先修复该文件，或改用「覆盖导入」整体替换。")
        ctx_errs = _validate_case_lines(existing + [ln.strip() for ln in lines if ln.strip()])
        if ctx_errs:
            raise ValueError("追加后校验失败（id 冲突？）：" + "；".join(ctx_errs[:8]))
        n = _write_cases_file(sf, existing + [ln.strip() for ln in lines if ln.strip()])
    else:
        n = _write_cases_file(sf, lines)

    entry["suite_file"] = Path(sf).name
    entry["cases_count"] = n
    cat["datasets"][idx] = entry
    bak = _atomic_write(cat)
    return {"id": dsid, "cases": n, "mode": mode, "suite_file": entry["suite_file"], "backup": bak}


# ---------------------------------------------------------------------------
# 自检：分类覆盖率
# ---------------------------------------------------------------------------
def taxonomy_report() -> dict:
    """回填/体检用：统计每条是否都有受控 domain 与 capabilities。"""
    ds = _load().get("datasets", [])
    missing_domain = [d["id"] for d in ds if not tax.is_domain(str(d.get("domain") or ""))]
    missing_caps = [d["id"] for d in ds if not (d.get("capabilities") or [])]
    return {
        "total": len(ds),
        "missing_domain": missing_domain,
        "missing_capabilities": missing_caps,
        "domains_used": sorted({tax.domain_of(d) for d in ds}),
    }
