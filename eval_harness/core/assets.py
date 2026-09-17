"""eval_harness · 版本化资产（O4）：Prompt / Function 一级对象 + push/pull

Braintrust 的「Prompt/Function versioning + push/pull」对应物。坚持「资产即代码、可回滚、可分发」：
- Asset：prompt（文本）/ function（可注册进 ToolRegistry 托管执行的源码表达式），按 name+version 管理。
- 持久化：asset_versions 表（content_hash 幂等判定）。
- push/pull：把全部资产导出为 .harnessassets JSON 包；在另一套库 import 即还原（跨团队/跨环境分发）。
- function 资产可一键注册为工具（register_function_asset_as_tool），与 O10 工具托管打通。

版本解析：未指定 version 时自动用内容 SHA-256 前 8 位；同名资产多次改动即产生多个版本，可回滚。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Asset:
    kind: str               # prompt | function
    name: str
    version: str
    content: str
    content_hash: str = ""
    created_at: float = 0.0
    meta: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Asset":
        return cls(kind=d.get("kind", "prompt"), name=d["name"], version=d.get("version", ""),
                   content=d.get("content", ""), content_hash=d.get("content_hash", ""),
                   created_at=d.get("created_at", 0.0), meta=d.get("meta", {}))


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------------
# CRUD（面向 Database 实例；复用 persistence 的 asset 方法）
# ----------------------------------------------------------------------------
async def save_asset(db, kind: str, name: str, content: str, version: Optional[str] = None,
                     meta: Optional[dict] = None) -> int:
    v = version or _hash(content)[:8]
    return await db.save_asset(kind, name, v, content, content_hash=_hash(content), meta=meta)


async def get_asset(db, name: str, version: Optional[str] = None) -> Optional[Asset]:
    d = await db.get_asset(name, version)
    return Asset.from_dict(d) if d else None


async def list_assets(db) -> list:
    return [Asset.from_dict(d) for d in await db.list_assets()]


async def asset_versions(db, name: str) -> list:
    return await db.asset_versions(name)


async def delete_asset(db, name: str, version: Optional[str] = None) -> int:
    return await db.delete_asset(name, version)


# ----------------------------------------------------------------------------
# push / pull：资产包分发
# ----------------------------------------------------------------------------
async def export_bundle(db, path: str) -> dict:
    """把全部资产导出为 .harnessassets JSON 包。"""
    assets = await list_assets(db)
    bundle = {
        "format": "harness-assets/1",
        "exported_at": time.time(),
        "assets": [a.__dict__ if hasattr(a, "__dict__") else a for a in
                   [{"kind": a.kind, "name": a.name, "version": a.version,
                     "content": a.content, "content_hash": a.content_hash,
                     "created_at": a.created_at, "meta": a.meta} for a in assets]],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)
    return {"path": path, "count": len(assets)}


async def import_bundle(db, path: str, overwrite: bool = False) -> dict:
    """从 .harnessassets 包导入资产（按 name+version 幂等；同 hash 跳过）。"""
    with open(path, encoding="utf-8") as f:
        bundle = json.load(f)
    existing = {(a.name, a.version) for a in await list_assets(db)}
    added, skipped = 0, 0
    for item in bundle.get("assets", []):
        key = (item["name"], item.get("version"))
        if key in existing and not overwrite:
            skipped += 1
            continue
        await db.save_asset(item["kind"], item["name"], item.get("version") or _hash(item["content"])[:8],
                           item["content"], content_hash=item.get("content_hash") or _hash(item["content"]),
                           meta=item.get("meta", {}))
        added += 1
    return {"added": added, "skipped": skipped, "total": len(bundle.get("assets", []))}


# ----------------------------------------------------------------------------
# function 资产 → 注册进工具托管（打通 O10）
# ----------------------------------------------------------------------------
def compile_function(content: str):
    """把 function 资产正文（一个 lambda/可调用表达式）编译为 callable。

    受限命名空间执行，仅暴露少数安全内置；正文应形如 ``lambda x: x.strip().lower()``。
    """
    safe = {"__builtins__": {}, "len": len, "str": str, "int": int, "float": float,
            "list": list, "dict": dict, "json": json}
    fn = eval(content, safe)  # noqa: S307 - 资产由用户显式管理，非不受信输入
    if not callable(fn):
        raise ValueError("function 资产正文必须编译为可调用对象")
    return fn


async def register_function_asset_as_tool(db, name: str, version: Optional[str] = None,
                                          registry=None) -> str:
    """拉取 function 资产并注册进 ToolRegistry（与 O10 托管执行打通）。"""
    from .tools import get_registry
    asset = await get_asset(db, name, version)
    if asset is None:
        raise KeyError(f"找不到 function 资产：{name}")
    if asset.kind != "function":
        raise ValueError(f"资产 {name} 不是 function 类型（实际 {asset.kind}）")
    fn = compile_function(asset.content)
    reg = registry or get_registry()
    reg.register(name, f"function 资产 v{asset.version}", fn=fn)
    return name
