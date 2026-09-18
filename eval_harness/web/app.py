"""eval_harness · Apple 风格可视化控制台（创新⑦）

面向内部评测人员的 Web 控制台：
- FastAPI 后端：读取持久化层(PostgreSQL/SQLite)数据，提供 REST + 静态托管
- 单页 SPA（web/static）：概览 / 运行列表 / 运行详情 / 生产轨迹 / 数据集（管理）/ 新建评测
- 直接复用 engine.run_suite_async（含 6 创新点落库）启动评测

启动：
    python -m eval_harness.web            # 默认 sqlite
    EVAL_DB_URL=postgresql+asyncpg://... python -m eval_harness.web
    python -m eval_harness.web --port 8848 --db-url postgresql+asyncpg://...
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Body, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..core.persistence import Database
from ..core.sql import PREVIEW_QUERIES
from ..core.version import HARNESS_VERSION
from ..core.rbac import get_engine, Cap, caller_from_headers
from ..core.sso import OIDCClient, map_claims_to_group, apply_sso_principal, claims_to_capabilities
from ..core.alerting import AlertRule, AlertManager
from ..core.online import OnlineEvaluator
from ..core import assets as asset_mod
from ..core import datasets_catalog as dc
from ..core import datasets_admin as dsa
from ..core import datasets_taxonomy as tax

HERE = Path(__file__).resolve().parent
EXAMPLES_DIR = HERE.parent / "examples"
STATIC_DIR = HERE / "static"

db: Optional[Database] = None
# 在途评测任务（内存态，仅用于「运行中」提示）
_jobs: dict[str, dict] = {}

# ---- 批量评测：内存态协调（批→子 run 映射 + 并发闸） ----
# 每个数据集 = 一个独立 run（各自保留版本钉 / 评分器 / 报告），此处只做编排与汇总。
_batches: dict[str, dict] = {}
# 并发闸：避免 N 个数据集同时打模型 API 触发限流；mock 场景也避免一次性起过多任务。
_batch_sem = asyncio.Semaphore(3)

# ---- O3：在线流评测器 + 共享告警管理器（进程内内存态） ----
_alert_mgr = AlertManager()
_online = OnlineEvaluator(default_grader="code", alert_manager=_alert_mgr)

# ---------------------------------------------------------------------------
class NewEvalRequest(BaseModel):
    suite: str = "practice3_smoke.jsonl"   # 示例名或绝对路径
    provider: str = "mock"
    provider_mode: str = "full"
    judge_provider: str = ""               # 留空=不启用 judge 通道
    grader: str = "code,judge"
    trials: int = 3
    trial_policy: str = "any"
    three_way: bool = True
    model_version: str = "deepseek-chat"
    dataset_version: str = "manual"
    run_name: str = ""
    judge_budget: float = 1.0
    # —— O14：工作台回链（可选） —— #
    wb_callback_url: str = ""   # 发起时携带工作台验收单 URL，运行详情页可一键返回工作台
    wb_contract_id: str = ""    # 关联的工作台合同/验收单 id（随运行记录，供回链与归集）
    # —— 批量评测：透传给引擎 params（落 config_json，用于汇总矩阵回查 dataset_id） —— #
    params: Optional[dict] = None


class NewBatchRequest(BaseModel):
    """批量评测：多选数据集 → 拆成 N 个独立 run（各自保留版本钉 / 评分器 / 报告）。

    不做「合并成一个 run」——那样会写坏 run.dataset_version（单值）、稀释聚合通过率、
    错配评分器，并让不可运行集触发整批熔断。详情见控制台说明。
    """
    datasets: list[str]                                  # 数据集 id 列表（可多选）
    provider: str = "mock"
    provider_mode: str = "full"
    judge_provider: str = ""
    grader: str = ""                                     # 留空 = 每个数据集用各自推荐评分器
    trials: int = 3
    trial_policy: str = "any"
    three_way: bool = True
    model_version: str = "deepseek-chat"
    dataset_version: str = ""                            # 留空 = 每个数据集用各自版本钉
    run_name_prefix: str = "批量"
    max_concurrency: int = 3                             # ≤ batch 并发闸上限
    # —— O14：工作台回链（可选） —— #
    wb_callback_url: str = ""
    wb_contract_id: str = ""


class SummaryRequest(BaseModel):
    """R2：多运行统一汇总报告请求（支持任意 run_ids 或整批 batch_id）。"""
    run_ids: Optional[list[str]] = None
    batch_id: Optional[str] = None


def _resolve_suite(suite: str) -> str:
    """把 suite（catalog id / 文件名 / 绝对路径）解析为可加载的 JSONL 路径。

    优先级：绝对路径 → catalog 条目 suite_file（datasets/ 与 examples/ 都查）
    → 裸文件名（datasets/ 与 examples/ 都查）。
    """
    try:
        return dc.resolve_dataset_path(suite)
    except FileNotFoundError:
        raise HTTPException(400, f"找不到用例集：{suite}（不在 catalog/datasets/examples 下）")


# ---- R5 / R6：执行提供方（被测对象）/ Judge 通道 解析与 CRUD 辅助 ----
# 内置提供方（被测对象）的展示名；自定义配置可覆盖。
_PROVIDER_DISPLAY = {
    "mock": "内置 Mock（确定性样本）",
    "deepseek": "DeepSeek",
    "local_agent": "本地智能体（HTTP 靶机）",
    "qwen": "通义千问",
    "zhipu": "智谱 GLM",
    "domestic_gateway": "国产聚合网关",
}
# 可作为 Judge 通道（评测用 LLM）的内置项。
_JUDGE_DISPLAY = {
    "deepseek": "DeepSeek（判官）",
    "qwen": "通义千问（判官）",
    "zhipu": "智谱 GLM（判官）",
    "domestic_gateway": "国产聚合网关（判官）",
}
# 被测对象可用注册表 key（过滤掉纯评分类 sandbox_script / remote）。
_SUT_KINDS = ("mock", "deepseek", "local_agent", "qwen", "zhipu", "domestic_gateway")
# Judge 通道可用注册表 key（评测 LLM）。
_JUDGE_KINDS = ("deepseek", "qwen", "zhipu", "domestic_gateway")


def _env_key_for(kind: str) -> str:
    """按 provider 类型取环境变量 API key（与历史 launch_eval 行为一致）。"""
    m = {"deepseek": "DEEPSEEK_API_KEY", "qwen": "QWEN_API_KEY",
         "zhipu": "ZHIPU_API_KEY", "domestic_gateway": "DOMESTIC_GATEWAY_KEY"}
    env = m.get(kind)
    return os.environ.get(env, "") if env else ""


def _cfg_to_kwargs(cfg: dict) -> dict:
    """把 ProviderConfig / JudgeConfig 转成引擎构造参数（base_url/api_key/model/extra）。"""
    kwargs: dict = {}
    ak = cfg.get("api_key") or _env_key_for(cfg.get("kind", ""))
    if ak:
        kwargs["api_key"] = ak
    if cfg.get("base_url"):
        kwargs["base_url"] = cfg["base_url"]
    if cfg.get("model"):
        kwargs["model"] = cfg["model"]
    kwargs.update(cfg.get("extra") or {})
    return kwargs


async def _resolve_provider(name: str, mode: str = "full"):
    """解析执行提供方 → (engine_provider_name, kwargs)。

    优先查 DB 自定义配置（R5）；无配置则回退注册表内置（含 deepseek 用环境变量 key）。
    """
    if name == "mock":
        return "mock", {"mode": mode}
    if db is not None:
        cfg = await db.get_provider_config(name)
        if cfg:
            return cfg["kind"], _cfg_to_kwargs(cfg)
    if name == "deepseek":
        key = os.environ.get("DEEPSEEK_API_KEY")
        return name, ({"api_key": key} if key else {})
    return name, {}


async def _resolve_judge(name: str):
    """解析 Judge 通道 → (engine_judge_name, kwargs)。空名返回 (None, None) 表示不启用。"""
    if not name:
        return None, None
    if db is not None:
        cfg = await db.get_judge_config(name)
        if cfg:
            return cfg["kind"], _cfg_to_kwargs(cfg)
    if name == "deepseek":
        key = os.environ.get("DEEPSEEK_API_KEY")
        return name, ({"api_key": key} if key else {})
    return name, {}


def _new_run_id() -> str:
    import uuid as _uuid
    return str(_uuid.uuid4())


# ---- R5 / R6：提供方 / Judge 通道 配置请求模型 ----
class ConfigRequest(BaseModel):
    name: str                                   # 下拉框使用的 key（必填、唯一）
    display_name: str = ""                       # 展示名（留空=name）
    kind: str                                    # 注册表 key（如 deepseek / local_agent）
    base_url: str = ""
    api_key: str = ""                            # 留空=不修改已存 key（更新时）
    model: str = ""
    extra: Optional[dict] = None


# ---- N12：RBAC 行级/能力守卫（仅当 EVAL_RBAC=1 时强制；默认宽松） ----
_RBAC_ENABLED = os.environ.get("EVAL_RBAC") == "1"


def _enforce(cap: Cap, request: Request):
    """受保护写操作的能力守卫；未开启 RBAC 时放行。"""
    if not _RBAC_ENABLED:
        return
    principal = caller_from_headers(dict(request.headers))
    if not get_engine().can(principal, cap):
        raise HTTPException(403, f"缺少能力 {cap.value}（主体 {principal}）")


def _caller(request: Request) -> str:
    return caller_from_headers(dict(request.headers))


def _build_oidc(provider: str, redirect_uri: str = ""):
    """按 provider 名读 EVAL_SSO_* 环境变量构造 OIDC 客户端（生产用）。"""
    p = provider.upper()
    issuer = os.environ.get(f"EVAL_SSO_{p}_ISSUER") or os.environ.get("EVAL_SSO_ISSUER", "")
    return OIDCClient(
        issuer=issuer,
        client_id=os.environ.get(f"EVAL_SSO_{p}_CLIENT_ID") or os.environ.get("EVAL_SSO_CLIENT_ID", ""),
        client_secret=os.environ.get(f"EVAL_SSO_{p}_CLIENT_SECRET") or os.environ.get("EVAL_SSO_CLIENT_SECRET", ""),
        redirect_uri=redirect_uri or os.environ.get("EVAL_SSO_REDIRECT", ""),
    )


# ---------------------------------------------------------------------------
# 数据集目录：管理视图辅助（列表富集 / 详情 / 校验 / 导出）
# ---------------------------------------------------------------------------
# 结构性评分器的界面文案（名称一律取自插件注册表，本表只补「说人话」的描述）
_GRADER_LABELS = {
    "code": "确定性比对（exact / contains / f1）",
    "judge": "模型判官（需 judge 通道）",
    "human": "人工评审（盲评队列）",
    "sandbox": "受限子进程真执行（代码生成 / 结构化断言）",
    "contract": "输出契约（JSON schema / 工具调用格式）",
    "permission": "权限边界（禁用工具 + 合规拒绝语）",
    "fallback": "失效兜底（故障时降级为「不可判」）",
    "attribution": "责任归属（结论来源可追溯）",
    "trajectory": "轨迹归因（步骤覆盖率 + 首错步骤）",
    "trajectory_eval": "轨迹质量评估（判官版）",
    "classifier": "分类器评分（Span 级）",
}


def _grader_choices() -> list[dict]:
    """给 UI 用的评分器候选：结构性评分器 + AutoEval 目录。

    名单全部从注册表与 AUTOEVALS 派生，不在 Web 层硬编——避免「界面能选、引擎没注册」
    或反过来「引擎支持、界面选不到」（如 sandbox / permission 此前就选不到）。
    """
    from ..core.registry import GRADERS
    from ..core.scorers import AUTOEVALS
    names = GRADERS.available()
    out: list[dict] = []
    for n in names:  # 结构性评分器
        if n in AUTOEVALS:
            continue
        out.append({"name": n, "group": "core", "label": _GRADER_LABELS.get(n, "")})
    for n, desc in AUTOEVALS.items():  # 自动评测器
        if n in names:
            out.append({"name": n, "group": "autoeval", "label": desc})
    return out


def _known_graders() -> list[str]:
    """已注册评分器名单（唯一事实来源 = 插件注册表）。

    评分器靠 import 时的装饰器注册，所以读注册表前必须先 import 承载它们的模块，
    否则 `scorers` 里的 AutoEval 尚未注册，名单会随调用顺序变化。
    """
    from ..core import scorers  # noqa: F401  —— 触发注册（副作用）
    from ..core.registry import GRADERS
    return GRADERS.available()


def _normalize_graders(text: str, known: list[str]) -> list[str]:
    """从 catalog 的描述性文案提取「顶层评分器名」。

    catalog 的 recommended_grader 是人类可读描述（如 `code(exact/contains)`、
    `permission,attribution`、`sandbox`），不能直接当 CLI 参数。做法：先剥掉括号里的
    模式说明（那是给同一评分器的子模式，不是另一个评分器），再按逗号切分，只保留
    注册表中真实存在的名字。

    注意不要按标识符全量扫描——注册表里还有 `contains`/`exact_match` 这类子评分器，
    全量扫描会把 `code(exact/contains)` 误判成 `code,contains`。
    """
    stripped = re.sub(r"[（(][^）)]*[）)]", "", str(text or ""))
    out: list[str] = []
    for seg in stripped.split(","):
        tok = seg.strip()
        if tok in known and tok not in out:
            out.append(tok)
    return out


def _clip(s, n: int) -> str:
    txt = re.sub(r"\s+", " ", str(s if s is not None else "")).strip()
    return txt if len(txt) <= n else txt[:n] + "…"


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ds_brief(e: dict, known: list[str]) -> dict:
    """把 catalog 富集条目收敛为管理视图需要的字段（列表与详情共用）。"""
    graders = _normalize_graders(e.get("recommended_grader", ""), known)
    caps = tax.capabilities_of(e)
    return {
        "id": e["id"], "name": e["name"],
        "tier": e.get("tier"), "tier_label": e.get("tier_label", e.get("tier")),
        # 受控分类：domain 是一级分组依据，capabilities 是二级标签
        "domain": tax.domain_of(e), "domain_label": tax.domain_label_of(e),
        "capabilities": caps, "capability_labels": tax.capability_labels(caps),
        "status": e.get("status"), "status_label": e.get("status_label", e.get("status")),
        "runnable": bool(e.get("runnable")), "cases": e.get("actual_cases", 0),
        "category": e.get("category", ""),
        "description": e.get("description", ""),
        "recommended_grader": e.get("recommended_grader", ""),
        "graders": graders,
        "grader_available": bool(graders) and all(g in known for g in graders),
        "recommends_judge": bool(e.get("recommends_judge")),
        "est_duration_min": e.get("est_duration_min"),
        "est_tokens": e.get("est_tokens"),
        "license": e.get("license", ""),
        "commercial_use": e.get("commercial_use"),
        "requires_env": bool(e.get("requires_env")),
        "env_notes": e.get("env_notes", ""),
        "scenario": e.get("scenario", ""),
        "access": e.get("access", ""),
        "source": e.get("source", ""), "official_url": e.get("official_url", ""),
        "full_scale": e.get("full_scale", ""), "languages": e.get("languages", []),
        "version": e.get("version", ""), "updated": e.get("updated", ""),
        "data_format": e.get("data_format", ""),
        "suite_file": e.get("suite_file"),
        "source_kind": e.get("source_kind", ""),
        "sample_note": e.get("sample_note", ""), "sample_built": e.get("sample_built", ""),
        "aligned_to": e.get("aligned_to", ""),
        # 兼容字段：编辑表单要能回显与修改，否则用户在界面上看到的是空白默认值
        "object_axes": e.get("object_axes", []),
        "dimensions": e.get("dimensions", []),
        "cases_count": int(e.get("cases_count") or 0),
        "pinned": bool(e.get("file_sha256")),
    }


def _preview_cases(path: Path, n: int) -> list[dict]:
    """读用例文件前 n 条，做脱敏截断，供详情页「长得什么样」预览。"""
    out: list[dict] = []
    if n <= 0:
        return out
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if len(out) >= n:
                break
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception as ex:  # noqa: BLE001
                out.append({"line": i, "error": f"JSON 解析失败：{ex}"})
                continue
            meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
            out.append({
                "line": i,
                "id": obj.get("id", ""),
                "input": _clip(obj.get("input", ""), 400),
                "gold": _clip(obj.get("gold", ""), 200),
                "suite": obj.get("suite", ""),
                "grader": obj.get("grader", ""),
                "meta_keys": sorted(meta.keys()),
                "meta_brief": {k: _clip(v, 120) for k, v in list(meta.items())[:5]},
            })
    return out


# ---------------------------------------------------------------------------
# 用例在线预览：行级索引缓存 + 分页读取
# ---------------------------------------------------------------------------
# 为什么要有索引：预览器要支持「搜索 + 翻页」，每次都重扫文件既慢、页码也难稳定。
# 建一次索引（每行字节偏移 + 轻量检索字段 + 分布统计），翻页只 seek 读目标那几十行。
# 缓存键里带 mtime/size：文件被替换或重新导入用例后自动失效，不会读到旧题。
_CASES_INDEX: "OrderedDict[str, dict]" = OrderedDict()
_CASES_INDEX_MAX = 8          # 最多缓存 8 个数据集的行索引
_SEARCH_CHARS = 4000          # 每行参与检索的文本上限（防超长 input 把内存吃满）
_VALUE_LIMIT = 20000          # 单条用例里单个字符串的展示上限（超出截断并如实标注）
_PREVIEW_MAX_BYTES = 64 << 20  # 超过 64 MB 的用例文件不做在线预览（引导走导出）
_FACET_KEYS = ("difficulty", "grader", "suite", "category")


def _norm_text(s) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip()


def _path_in_scope(p: Path) -> bool:
    """只放行 datasets/ 与 examples/ 下的文件。

    catalog 是本地可编辑文件，理论上可被写入越界路径；预览接口按路径读文件，
    所以这里做一次归属校验，不让它变成任意文件读取的口子。
    """
    try:
        rp = p.resolve()
    except OSError:
        return False
    for base in (dc.DATASETS_DIR, dc.EXAMPLES_DIR):
        try:
            rp.relative_to(base.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _build_cases_index(path: Path) -> dict:
    """扫一遍文件建行索引：偏移 + 检索文本 + 分布统计（不缓存题目正文，翻页时再读）。"""
    st = path.stat()
    offsets: list[int] = []
    items: list[dict] = []
    fields: list[str] = []
    seen: set[str] = set()
    facets: dict[str, dict[str, int]] = {k: {} for k in _FACET_KEYS}
    bad = 0
    off = 0
    with open(path, "rb") as f:
        for line_no, raw in enumerate(f, 1):
            n = len(raw)
            if not raw.strip():
                off += n
                continue
            offsets.append(off)
            off += n
            try:
                obj = json.loads(raw.decode("utf-8"))
                if not isinstance(obj, dict):
                    raise ValueError("顶层不是 JSON 对象")
            except Exception as ex:  # noqa: BLE001
                bad += 1
                items.append({"line": line_no, "error": f"JSON 解析失败：{ex}",
                              "raw": raw.decode("utf-8", "replace")[:2000]})
                continue
            for k in obj:
                if k not in seen:
                    seen.add(k)
                    fields.append(k)
            item = {
                "line": line_no,
                "id": str(obj.get("id") or ""),
                "input": _clip(obj.get("input", ""), 180),
                "search": _norm_text(json.dumps(obj, ensure_ascii=False)).lower()[:_SEARCH_CHARS],
            }
            for k in _FACET_KEYS:
                v = obj.get(k)
                if v not in (None, ""):
                    item[k] = str(v)
                    facets[k][str(v)] = facets[k].get(str(v), 0) + 1
            items.append(item)
    ordered = {k: dict(sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))) for k, v in facets.items()}
    return {"mtime": st.st_mtime, "size": st.st_size, "bytes": st.st_size,
            "offsets": offsets, "items": items, "fields": fields, "facets": ordered,
            "bad": bad, "valid": len(items) - bad}


def _get_cases_index(path: Path) -> dict:
    key = str(path)
    st = path.stat()
    hit = _CASES_INDEX.get(key)
    if hit and hit["mtime"] == st.st_mtime and hit["size"] == st.st_size:
        _CASES_INDEX.move_to_end(key)
        return hit
    idx = _build_cases_index(path)
    _CASES_INDEX[key] = idx
    _CASES_INDEX.move_to_end(key)
    while len(_CASES_INDEX) > _CASES_INDEX_MAX:
        _CASES_INDEX.popitem(last=False)
    return idx


def _invalidate_cases_index() -> None:
    """写操作用例文件后必须调用。

    否则预览器仍读旧索引——表现为「刚导入的用例在预览里看不到」，而列表里条数已经变了。
    """
    _CASES_INDEX.clear()


def _truncate_values(obj, limit: int = _VALUE_LIMIT) -> tuple[object, list[str]]:
    """递归截断超长字符串，并回报被截断的字段路径（界面上要如实说「这里没显示全」）。"""
    cut: list[str] = []

    def walk(v, p: str):
        if isinstance(v, str):
            if len(v) > limit:
                cut.append(p or "$")
                return v[:limit] + f"\n…（已截断，原文 {len(v)} 字符）"
            return v
        if isinstance(v, dict):
            return {k: walk(x, f"{p}.{k}" if p else str(k)) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x, f"{p}[{i}]") for i, x in enumerate(v)]
        return v

    return walk(obj, ""), cut


def _cases_page(path: Path, page: int, page_size: int, q: str = "",
                filters: Optional[dict[str, str]] = None) -> dict:
    """按「关键词 + 字段筛选」分页读用例。

    分页切出的是连续区间，所以只 seek 一次再顺序读若干行，不做逐行随机读。
    """
    idx = _get_cases_index(path)
    items: list[dict] = idx["items"]
    nq = (q or "").strip().lower()
    fl = {k: v for k, v in (filters or {}).items() if v}
    hit: list[int] = []
    for i, it in enumerate(items):
        if it.get("error"):
            # 坏行只在「完全不筛选」时露出，否则会干扰搜索结果的判断
            if not nq and not fl:
                hit.append(i)
            continue
        if nq and nq not in it.get("search", ""):
            continue
        if any(str(it.get(k, "")) != v for k, v in fl.items()):
            continue
        hit.append(i)
    total = len(hit)
    pages = max(1, math.ceil(total / page_size)) if total else 1
    page = min(max(1, page), pages)
    sel = hit[(page - 1) * page_size: page * page_size]
    cases: list[dict] = []
    if sel:
        with open(path, "rb") as f:
            f.seek(idx["offsets"][sel[0]])
            raws = [f.readline() for _ in sel]
        for pos, raw in zip(sel, raws):
            it = items[pos]
            if it.get("error"):
                cases.append({"line": it["line"], "error": it["error"],
                              "raw_text": it.get("raw", "")})
                continue
            try:
                obj = json.loads(raw.decode("utf-8"))
            except Exception as ex:  # noqa: BLE001
                cases.append({"line": it["line"], "error": f"读取失败：{ex}"})
                continue
            shown, cut = _truncate_values(obj)
            cases.append({"line": it["line"], "data": shown, "truncated": cut})
    return {
        "cases": cases, "total": total, "page": page, "pages": pages,
        "page_size": page_size, "facets": idx["facets"], "fields": idx["fields"],
        "file": {"bytes": idx["bytes"], "mtime": idx["mtime"], "lines": len(items),
                 "valid": idx["valid"], "bad": idx["bad"]},
        "filters": {"q": q, **fl},
    }


def _domains_with_counts(known: list[str]) -> list[dict]:
    """给 UI 的域清单：受控定义 + 每条域下的数据集条数与可运行条数。

    计数从真实数据算，不硬编——否则界面上会出现「域显示 10 条、点进去只有 8 条」。
    """
    briefs = [_ds_brief(e, known) for e in dc.list_datasets()]
    out: list[dict] = []
    for meta in tax.domains_meta():
        mine = [b for b in briefs if b["domain"] == meta["key"]]
        out.append({**meta, "count": len(mine),
                    "runnable": sum(1 for b in mine if b["runnable"])})
    other = [b for b in briefs if b["domain"] == "other"]
    if other:
        out.append({"key": "other", "label": "未分类",
                    "desc": "尚未归入受控域的条目（应在编辑里补上 domain）。",
                    "count": len(other),
                    "runnable": sum(1 for b in other if b["runnable"])})
    return out


def create_app(db_url: Optional[str] = None) -> FastAPI:
    global db
    db = Database(db_url or os.environ.get("EVAL_DB_URL") or "sqlite+aiosqlite:///./eval_harness.db")

    app = FastAPI(title="智化融合评测 Harness 控制台", version=HARNESS_VERSION)

    @app.on_event("startup")
    async def _startup():
        await db.init()

    @app.get("/api/health")
    async def health():
        return {
            "ok": True,
            "harness_version": HARNESS_VERSION,
            "backend": "postgresql" if db.url.startswith("postgresql") else "sqlite",
            "db_url": db.url,
        }

    @app.get("/api/dashboard")
    async def dashboard():
        runs = await db.list_runs(limit=200)
        trace_count = await db.count_traces()
        # KPI
        total_runs = len(runs)
        total_cases = sum(r["total"] for r in runs)
        avg_pass = (sum(r["pass_rate"] for r in runs) / total_runs) if total_runs else 0.0
        # 趋势（按时间升序）
        trend = sorted(
            [{"name": r["name"], "pass_rate": round(r["pass_rate"], 4),
              "created_at": r["created_at"]} for r in runs],
            key=lambda x: x["created_at"],
        )
        # 三通道解耦（仅含 three_way 的运行）
        three_way = [
            {"name": r["name"], **(r["three_way_json"] or {})}
            for r in runs if r.get("three_way_json")
        ]
        # grader 计数 + judge 成本（逐运行聚合）
        grader_mix: dict[str, int] = {}
        judge_cost = 0.0
        for r in runs[-50:]:
            detail = await db.get_run(r["id"])
            for c in detail.get("cases", []):
                for g in c.get("graders", []):
                    grader_mix[g["grader"]] = grader_mix.get(g["grader"], 0) + 1
                    if g["grader"] == "judge":
                        judge_cost += float(g.get("cost_usd", 0.0) or 0.0)
        return {
            "kpis": {
                "runs": total_runs, "cases": total_cases,
                "avg_pass_rate": round(avg_pass, 4),
                "traces": trace_count,
                "judge_cost_usd": round(judge_cost, 6),
            },
            "trend": trend,
            "three_way": three_way,
            "grader_mix": grader_mix,
            "harness_version": HARNESS_VERSION,
            "backend": "postgresql" if db.url.startswith("postgresql") else "sqlite",
        }

    @app.get("/api/runs")
    async def list_runs(limit: int = Query(50, le=500)):
        runs = await db.list_runs(limit=limit)
        return {"runs": runs, "count": len(runs)}

    @app.get("/api/runs/{rid}")
    async def get_run(rid: str):
        r = await db.get_run(rid)
        if r is None:
            raise HTTPException(404, "run not found")
        return r

    @app.delete("/api/runs/{rid}")
    async def delete_run_api(rid: str, request: Request):
        _enforce(Cap.DELETE, request)
        if db is None:
            raise HTTPException(500, "未连接数据库")
        ok = await db.delete_run(rid)
        if not ok:
            raise HTTPException(404, "运行不存在")
        await db.record_audit("run_delete", _caller(request), rid, ok=True)
        return {"ok": True}

    @app.get("/api/traces")
    async def list_traces(limit: int = Query(50, le=500)):
        traces = await db.list_traces(limit=limit)
        return {"traces": traces, "count": len(traces)}

    @app.get("/api/traces/{tid}")
    async def trace_detail(tid: int):
        from ..core.trace_store import evaluate_trace_payload, parse_trace_to_case
        traces = await db.list_traces(limit=10000)
        hit = next((t for t in traces if str(t["id"]) == str(tid)), None)
        if hit is None:
            raise HTTPException(404, "trace not found")
        res = evaluate_trace_payload(hit["payload"], grader_name="trajectory")
        case = parse_trace_to_case(hit["payload"], trace_id=f"trace:{tid}")
        return {
            "id": tid, "source": hit["source"], "created_at": hit["created_at"],
            "expected_steps": case.meta.get("expected_steps"),
            "trajectory_text": case.meta.get("trajectory_text"),
            "result": res,
        }

    @app.get("/api/case_sets")
    async def list_case_sets():
        return {"case_sets": await db.list_case_sets()}

    # ---- 只读 SQL 查询层（N9）：沙箱，四道闸由 core.sql 保障 ----
    @app.get("/api/sql/views")
    async def sql_views():
        from ..core.sql import describe_views
        try:
            views = await describe_views(db)
        except Exception as e:  # noqa: BLE001
            return {"views": [], "error": str(e)}
        return {"views": views, "preview_queries": PREVIEW_QUERIES}

    @app.get("/api/sql")
    async def sql_query(
        q: str = Query(..., description="只读 SELECT/WITH，仅可引用预置视图：runs/cases/graders/trials/traces"),
        limit: int = Query(200, le=5000),
        timeout: float = Query(15.0, le=60.0),
    ):
        from ..core.sql import run_query, SQLGuardError, PREVIEW_QUERIES
        try:
            result = await run_query(db, q, limit=limit, timeout=timeout)
        except SQLGuardError as e:
            raise HTTPException(400, f"只读查询被拒绝：{e}")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"查询执行失败：{e}")
        result["preview_queries"] = PREVIEW_QUERIES
        return result

    # ---- N10：日志检索 ----
    @app.get("/api/search")
    async def search(q: str = Query("", min_length=1)):
        return await db.search(q, limit=100)

    # ---- N10：数据出库（导出为 zip 下载）----
    @app.post("/api/export")
    async def export_data(
        fmt: str = Query("jsonl", pattern="^(jsonl|csv|parquet)$"),
        partition: str = Query("date", pattern="^(date|provider|model|run|none)$"),
    ):
        import os
        import tempfile
        import zipfile
        from ..core import export as export_mod
        tmp = tempfile.mkdtemp(prefix="eval_export_")
        out_dir = os.path.join(tmp, "export")
        info = await export_mod.export(db, out_dir, fmt=fmt, partition=partition)
        zip_path = os.path.join(tmp, "export.zip")
        export_mod.zip_dir(out_dir, zip_path)
        return FileResponse(zip_path, filename="eval_export.zip",
                            media_type="application/zip")

    # ---- N7：自定义看板 CRUD ----
    @app.get("/api/dashboards")
    async def list_dashboards():
        return {"dashboards": await db.list_dashboards()}

    @app.get("/api/dashboards/{did}")
    async def get_dashboard(did: str):
        d = await db.get_dashboard(did)
        if d is None:
            raise HTTPException(404, "dashboard not found")
        return d

    @app.post("/api/dashboards")
    async def create_dashboard(name: str = Body(...), config: dict = Body(...)):
        did = await db.save_dashboard(name, config)
        return {"id": did, "name": name}

    @app.delete("/api/dashboards/{did}")
    async def delete_dashboard(did: str):
        ok = await db.delete_dashboard(did)
        if not ok:
            raise HTTPException(404, "dashboard not found")
        return {"ok": True}

    @app.get("/api/dashboards/{did}/render")
    async def render_dashboard(did: str):
        from ..core.dashboards import compute_dashboard
        d = await db.get_dashboard(did)
        if d is None:
            raise HTTPException(404, "dashboard not found")
        widgets = await compute_dashboard(d["config"], db)
        return {"id": did, "name": d["name"], "widgets": widgets}

    # ---- N2：项目级 Rubric CRUD + 盲评 ----
    @app.get("/api/rubrics")
    async def list_rubrics():
        return {"rubrics": await db.list_rubrics()}

    @app.get("/api/rubrics/{rid}")
    async def get_rubric(rid: str):
        r = await db.get_rubric(rid)
        if r is None:
            raise HTTPException(404, "rubric not found")
        return r

    @app.post("/api/rubrics")
    async def create_rubric(request: Request, name: str = Body(...),
                            criteria: dict = Body(...), pass_threshold: float = Body(0.6)):
        _enforce(Cap.MANAGE, request)
        rid = await db.save_rubric(name, criteria, pass_threshold)
        return {"id": rid, "name": name}

    @app.delete("/api/rubrics/{rid}")
    async def delete_rubric(rid: str, request: Request):
        _enforce(Cap.MANAGE, request)
        ok = await db.delete_rubric(rid)
        if not ok:
            raise HTTPException(404, "rubric not found")
        return {"ok": True}

    @app.get("/api/runs/{rid}/blind")
    async def blind_run(rid: str):
        """盲评视图：匿名化 provider/model/git，供无偏见人工评审。"""
        from ..core.rubric import blind_anonymize
        r = await db.get_run(rid)
        if r is None:
            raise HTTPException(404, "run not found")
        return blind_anonymize(r)

    # ---- O12：Patterns 自动问题发现 ----
    @app.get("/api/runs/{rid}/patterns")
    async def run_patterns(rid: str):
        from ..core.patterns import discover_patterns
        return await discover_patterns(db, rid)

    # ---- O15：Trace 调试器（Span 级评分） ----
    @app.get("/api/traces/{tid}/debug")
    async def trace_debug(tid: int):
        from ..core.classifiers import score_spans
        from ..core.trace_store import evaluate_trace_payload, parse_trace_to_case
        traces = await db.list_traces(limit=10000)
        hit = next((t for t in traces if str(t["id"]) == str(tid)), None)
        if hit is None:
            raise HTTPException(404, "trace not found")
        res = evaluate_trace_payload(hit["payload"], grader_name="trajectory")
        case = parse_trace_to_case(hit["payload"], trace_id=f"trace:{tid}")
        spans = score_spans(hit["payload"])
        return {
            "id": tid, "source": hit["source"], "created_at": hit["created_at"],
            "steps": case.meta.get("expected_steps"),
            "trajectory_score": res["score"], "trajectory_passed": res["passed"],
            "span_scores": spans,
            "healthy_spans": sum(1 for s in spans if s["score"] >= 1.0),
            "total_spans": len(spans),
        }

    # ---- O16：多模态附件 ----
    @app.post("/api/runs/{rid}/attachments")
    async def add_attachment(rid: str, request: Request,
                             filename: str = Body(...), mime: str = Body("application/octet-stream"),
                             data: str = Body(...)):
        _enforce(Cap.MANAGE, request)
        r = await db.get_run(rid)
        if r is None:
            raise HTTPException(404, "run not found")
        atts = list(r.get("attachments", []))
        atts.append({"filename": filename, "mime": mime,
                     "data": data if len(data) < 5_000_000 else data[:5_000_000],
                     "size": len(data)})
        await db.attach_to_run(rid, atts)
        return {"ok": True, "count": len(atts)}

    # ---- O17：数据集快照 ----
    @app.get("/api/snapshots")
    async def list_snapshots():
        return {"snapshots": await db.list_snapshots()}

    @app.get("/api/snapshots/{sid}")
    async def get_snapshot(sid: str):
        s = await db.get_snapshot(sid)
        if s is None:
            raise HTTPException(404, "snapshot not found")
        return s

    # ---- O17：自定义视图（保存的 SQL 查询） ----
    @app.get("/api/views")
    async def list_views():
        return {"views": await db.list_views()}

    @app.post("/api/views")
    async def create_view(request: Request, name: str = Body(...), sql: str = Body(...)):
        _enforce(Cap.MANAGE, request)
        vid = await db.save_view(name, sql)
        return {"id": vid, "name": name}

    @app.delete("/api/views/{vid}")
    async def delete_view(vid: str, request: Request):
        _enforce(Cap.MANAGE, request)
        ok = await db.delete_view(vid)
        if not ok:
            raise HTTPException(404, "view not found")
        return {"ok": True}

    @app.get("/api/views/{vid}/run")
    async def run_view(vid: str, limit: int = Query(200, le=5000),
                       timeout: float = Query(15.0, le=60.0)):
        from ..core.sql import run_query, SQLGuardError
        v = await db.get_view(vid)
        if v is None:
            raise HTTPException(404, "view not found")
        try:
            result = await run_query(db, v["sql"], limit=limit, timeout=timeout)
        except SQLGuardError as e:
            raise HTTPException(400, f"只读查询被拒绝：{e}")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"查询执行失败：{e}")
        return result

    @app.get("/api/rbac/me")
    async def rbac_me(request: Request):
        principal = _caller(request)
        caps = [c.value for c in Cap if get_engine().can(principal, c)]
        return {"principal": principal, "capabilities": caps,
                "rbac_enabled": _RBAC_ENABLED}

    # ---- O2：企业合规 SSO（dev 模式允许直接传 claims 走登录；生产走真实 IdP） ----
    @app.post("/api/sso/login")
    async def sso_login(request: Request, provider: str = Body("default"),
                        redirect_uri: str = Body("")):
        if os.environ.get("EVAL_SSO_DEV") == "1":
            return {"provider": provider, "auth_url": f"dev://sso/{provider}?dev=1",
                    "dev_mode": True}
        client = _build_oidc(provider, redirect_uri)
        return {"provider": provider, "auth_url": client.authorization_url(), "dev_mode": False}

    @app.post("/api/sso/callback")
    async def sso_callback(request: Request, code: str = Body(""),
                           claims: Optional[dict] = Body(None)):
        if os.environ.get("EVAL_SSO_DEV") == "1" and claims:
            principal = claims.get("sub") or claims.get("email") or "anonymous"
            group = map_claims_to_group(claims)
            apply_sso_principal(get_engine(), principal, group)
            await db.record_audit("sso_login", principal, f"group={group}", ok=True)
            return {"principal": principal, "group": group,
                    "capabilities": [c.value for c in claims_to_capabilities(claims)]}
        client = _build_oidc("default", "")
        res = client.handle_callback(code, audit=None)
        principal, group = res["principal"], res["group"]
        apply_sso_principal(get_engine(), principal, group)
        await db.record_audit("sso_login", principal, f"group={group}", ok=True)
        return {"principal": principal, "group": group, "capabilities": res["capabilities"]}

    @app.get("/api/audit")
    async def list_audit(event_type: Optional[str] = Query(None),
                        limit: int = Query(200, le=500)):
        return {"audit": await db.list_audit(event_type=event_type, limit=limit)}

    # ---- O3：在线评分 + 告警 ----
    @app.get("/api/alert-rules")
    async def list_alert_rules():
        return {"rules": _alert_mgr.rules_dict()}

    @app.post("/api/alert-rules")
    async def add_alert_rule(request: Request, rule: dict = Body(...)):
        _enforce(Cap.MANAGE, request)
        r = _alert_mgr.add_rule_from_dict(rule)
        return {"name": r.name, "metric": r.metric, "op": r.op, "threshold": r.threshold}

    @app.delete("/api/alert-rules/{name}")
    async def del_alert_rule(name: str, request: Request):
        _enforce(Cap.MANAGE, request)
        return {"ok": _alert_mgr.remove_rule(name)}

    @app.post("/api/online/feed")
    async def online_feed(request: Request, item: dict = Body(...)):
        return _online.feed(
            item.get("input", ""), item.get("gold", ""), item.get("response", ""),
            case_id=item.get("case_id", ""), grader=item.get("grader"),
            latency_ms=float(item.get("latency_ms", 0.0)),
            cost_usd=float(item.get("cost_usd", 0.0)),
            category=item.get("category", "general"),
        )

    @app.get("/api/online/metrics")
    async def online_metrics():
        return _online.snapshot()

    # ---- O4：版本化资产（prompt / function） ----
    @app.get("/api/assets")
    async def list_assets():
        return {"assets": await asset_mod.list_assets(db)}

    @app.get("/api/assets/{name}")
    async def get_asset_ep(name: str, version: Optional[str] = Query(None)):
        a = await asset_mod.get_asset(db, name, version)
        if a is None:
            raise HTTPException(404, "asset not found")
        return a.__dict__

    @app.post("/api/assets")
    async def add_asset_ep(request: Request, kind: str = Body(...), name: str = Body(...),
                           content: str = Body(...), version: Optional[str] = Body(None),
                           meta: Optional[dict] = Body(None)):
        _enforce(Cap.MANAGE, request)
        aid = await asset_mod.save_asset(db, kind, name, content, version=version, meta=meta)
        return {"id": aid, "kind": kind, "name": name}

    @app.delete("/api/assets/{name}")
    async def del_asset_ep(name: str, version: Optional[str] = Query(None), request: Request = None):
        _enforce(Cap.MANAGE, request)
        return {"deleted": await asset_mod.delete_asset(db, name, version)}

    @app.post("/api/assets/{name}/register-tool")
    async def register_tool_ep(name: str, version: Optional[str] = Query(None), request: Request = None):
        _enforce(Cap.MANAGE, request)
        try:
            registered = await asset_mod.register_function_asset_as_tool(db, name, version)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        return {"registered": registered}

    @app.get("/api/datasets")
    async def list_datasets():
        """返回评测数据集目录（分级 + 受控分类 + 决策字段）。

        数据集按粗粒度档位返回：基础 / 进阶 / 深度 / 专家(环境)，并叠加受控两级分类：
        一级 `domain`（评测对象域，界面按其分组）、二级 `capabilities`（能力标签）。
        每条带评测人员决策所需的：分级、状态、实际用例数、估算耗时、估算 token、
        适用场景、推荐评分器（含是否已注册）、许可与是否可商用、是否需执行环境、版本钉状态。
        """
        # 先算 choices：它会 import scorers，触发全部评分器注册，之后 known 才是完整名单
        choices = _grader_choices()
        known = _known_graders()
        tiers = {}
        for tier, items in dc.group_by_tier().items():
            tiers[tier] = {
                "label": dc.TIER_LABELS.get(tier, tier),
                "datasets": [_ds_brief(e, known) for e in items],
            }
        return {
            "tiers": tiers,
            "tier_order": dc.TIER_ORDER,
            "tier_labels": dc.TIER_LABELS,
            "domains": _domains_with_counts(known),
            "domain_order": tax.DOMAIN_ORDER,
            "capabilities": tax.capabilities_meta(),
            "taxonomy_version": dc.load_catalog().get("taxonomy_version"),
            "fixtures": dc.list_fixtures(),
            "summary": dc.summary(),
            "graders": known,
            "grader_choices": choices,
            "editable_fields": sorted(dsa.EDITABLE_FIELDS),
        }

    @app.get("/api/datasets/{dsid}")
    async def dataset_detail(dsid: str, preview: int = Query(3, ge=0, le=20)):
        """数据集详情：catalog 原始条目 + 文件事实 + 版本钉核对 + 用例预览 + 阻断项。

        「文件事实」与「版本钉核对」是管理视图的核心价值——把 catalog 里登记的
        cases_count / file_sha256 与磁盘上的真实文件对一次，不一致就摆出来。
        """
        known = _known_graders()
        entry = dc.get_entry(dsid)
        if entry is None:
            raise HTTPException(404, f"数据集不存在：{dsid}")
        rich = next((e for e in dc.list_datasets() if e["id"] == dsid), dict(entry))
        brief = _ds_brief(rich, known)

        facts = {"exists": False, "file_path": None, "bytes": None, "sha256": None,
                 "mtime": None, "recorded_cases": int(entry.get("cases_count") or 0),
                 "actual_cases": brief["cases"]}
        fp = rich.get("file_path")
        if fp and Path(fp).exists():
            p = Path(fp)
            facts.update({
                "exists": True, "file_path": str(p), "bytes": p.stat().st_size,
                "sha256": _sha256_file(p), "mtime": p.stat().st_mtime,
            })

        recorded = entry.get("file_sha256")
        pin = {
            "version": entry.get("version"), "recorded_sha256": recorded,
            "fetched_at": entry.get("fetched_at"), "pinned": bool(recorded),
            "match": None,
        }
        if recorded and facts["sha256"]:
            pin["match"] = (recorded == facts["sha256"])

        blockers: list[dict] = []
        if entry.get("requires_env"):
            blockers.append({"level": "info", "field": "requires_env",
                             "message": "环境类数据集：仅元数据登记，本机不拉取、不可直接运行",
                             "detail": entry.get("env_notes", "")})
        elif not entry.get("suite_file"):
            blockers.append({"level": "error", "field": "suite_file",
                             "message": "未登记 suite_file，无法定位用例文件"})
        elif not facts["exists"]:
            blockers.append({"level": "error", "field": "file",
                             "message": f"文件未就位：{entry.get('suite_file')}（待拉取/构建）"})
        if not brief["grader_available"]:
            blockers.append({"level": "error", "field": "grader",
                             "message": f"推荐评分器未注册：{brief['recommended_grader']}"})
        if facts["exists"] and facts["actual_cases"] != facts["recorded_cases"]:
            blockers.append({"level": "warn", "field": "cases",
                             "message": f"条数不一致：catalog 记 {facts['recorded_cases']}，"
                                        f"磁盘实际 {facts['actual_cases']}"})
        if pin["pinned"] and pin["match"] is False:
            blockers.append({"level": "warn", "field": "pin",
                             "message": "文件内容与版本钉记录的 sha256 不一致（文件已被替换）"})
        if not pin["pinned"] and facts["exists"]:
            # 只在「有文件可钉」时才提示未钉；环境类/未就位集没有文件，提示没意义
            blockers.append({"level": "info", "field": "pin",
                             "message": "尚未版本钉：未记录 file_sha256 / fetched_at，"
                                        "无法证明「跑的是同一份题」"})
        if entry.get("commercial_use") is False:
            blockers.append({"level": "warn", "field": "license",
                             "message": f"许可非商用（{entry.get('license')}）：商用前须法务确认"})

        return {
            "dataset": brief, "entry": entry, "facts": facts, "pin": pin,
            "preview": _preview_cases(Path(facts["file_path"]), preview) if facts["exists"] else [],
            "blockers": blockers, "graders": known,
            "tier_order": dc.TIER_ORDER,
        }

    @app.get("/api/datasets/{dsid}/cases")
    async def preview_dataset_cases(
        dsid: str,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=200),
        q: str = Query(""),
        difficulty: str = Query(""),
        grader: str = Query(""),
        suite: str = Query(""),
        category: str = Query(""),
    ):
        """在线预览数据集用例内容（分页 + 关键词 / 字段筛选），只读。

        与「导出 JSONL」互补：导出是把整份文件拿走，这里是就地看内容——不用下载、
        可搜索、可翻页，并给出字段分布与坏行提示。路径限制在 datasets/ 与 examples/ 下。
        """
        known = _known_graders()
        entry = dc.get_entry(dsid)
        if entry is None:
            raise HTTPException(404, f"数据集不存在：{dsid}")
        rich = next((e for e in dc.list_datasets() if e["id"] == dsid), dict(entry))
        brief = _ds_brief(rich, known)
        fp = rich.get("file_path")
        if not fp or not Path(fp).exists():
            why = ("仅元数据登记，本机没有用例文件（需执行环境）" if entry.get("requires_env")
                   else "用例文件未就位（待拉取/构建）")
            raise HTTPException(400, f"该数据集暂无可预览内容：{why}")
        p = Path(fp)
        if not _path_in_scope(p):
            raise HTTPException(403, f"用例文件不在 datasets/ 或 examples/ 下，拒绝读取：{p}")
        if p.stat().st_size > _PREVIEW_MAX_BYTES:
            raise HTTPException(400, f"文件过大（{p.stat().st_size} 字节），请改用导出 JSONL 查看")
        return {
            "dataset": brief, "path": str(p), "suite_file": entry.get("suite_file"),
            **_cases_page(p, page, page_size, q,
                          {"difficulty": difficulty, "grader": grader,
                           "suite": suite, "category": category}),
        }

    @app.post("/api/datasets/validate")
    async def validate_datasets(request: Request, payload: dict = Body(default=None)):
        """批量体检数据集：文件就位 / JSONL 可解析 / 条数一致 / 评分器已注册 / 许可 / 版本钉。

        body: {"ids": ["..."] , "deep": true, "include_pin_status": false}
        ids 为空表示校验全部；deep=false 跳过逐行解析（快照模式）。
        """
        _enforce(Cap.REVIEW, request)
        known = _known_graders()
        payload = payload or {}
        ids = payload.get("ids") or []
        deep = bool(payload.get("deep", True))
        include_pin = bool(payload.get("include_pin_status", False))

        entries = dc.list_datasets()
        if ids:
            want = set(ids)
            entries = [e for e in entries if e["id"] in want]

        issues: list[dict] = []
        checked = ok_count = 0
        for e in entries:
            checked += 1
            did = e["id"]
            before = len(issues)
            if e.get("requires_env"):
                issues.append({"id": did, "level": "info", "field": "requires_env",
                               "message": "环境类数据集：仅元数据登记，本机不可直接运行"})
                continue
            fp = e.get("file_path")
            if not fp or not Path(fp).exists():
                issues.append({"id": did, "level": "error", "field": "file",
                               "message": f"文件未就位：{e.get('suite_file') or '（未登记）'}"})
                continue
            p = Path(fp)
            n_lines = 0
            parse_err: list[int] = []
            missing_id: list[int] = []
            missing_input: list[int] = []
            if deep:
                with open(p, encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if not line.strip():
                            continue
                        n_lines += 1
                        try:
                            obj = json.loads(line)
                        except Exception:  # noqa: BLE001
                            parse_err.append(i)
                            continue
                        if not obj.get("id"):
                            missing_id.append(i)
                        if obj.get("input") in (None, ""):
                            missing_input.append(i)
            else:
                n_lines = int(e.get("actual_cases") or 0)
            if parse_err:
                issues.append({"id": did, "level": "error", "field": "jsonl",
                               "message": f"{len(parse_err)} 行 JSON 解析失败（行号 {parse_err[:5]}）"})
            if missing_id:
                issues.append({"id": did, "level": "error", "field": "id",
                               "message": f"{len(missing_id)} 条缺少 id（行号 {missing_id[:5]}）"})
            if missing_input:
                issues.append({"id": did, "level": "warn", "field": "input",
                               "message": f"{len(missing_input)} 条 input 为空（行号 {missing_input[:5]}）"})
            recorded = int(e.get("cases_count") or 0)
            if deep and n_lines != recorded:
                issues.append({"id": did, "level": "warn", "field": "cases",
                               "message": f"条数不一致：catalog 记 {recorded}，磁盘实际 {n_lines}"})
            graders = _normalize_graders(e.get("recommended_grader", ""), known)
            if graders and not all(g in known for g in graders):
                issues.append({"id": did, "level": "error", "field": "grader",
                               "message": f"推荐评分器未注册：{e.get('recommended_grader')}"})
            if e.get("commercial_use") is False:
                issues.append({"id": did, "level": "warn", "field": "license",
                               "message": f"许可非商用：{e.get('license')}"})
            if e.get("file_sha256"):
                if _sha256_file(p) != e.get("file_sha256"):
                    issues.append({"id": did, "level": "warn", "field": "pin",
                                   "message": "文件内容与版本钉记录的 sha256 不一致（文件已被替换）"})
            elif include_pin:
                issues.append({"id": did, "level": "info", "field": "pin",
                               "message": "尚未版本钉（未记录 file_sha256）"})
            # 「健康」只认错误/警告；info（环境占位、尚未版本钉）是纯提示，不拉低健康度
            if not any(i["level"] in ("error", "warn") for i in issues[before:]):
                ok_count += 1

        counts = {lv: sum(1 for i in issues if i["level"] == lv)
                  for lv in ("error", "warn", "info")}
        return {
            "checked": checked, "clean": ok_count, "deep": deep,
            "pin_pending": sum(1 for e in entries
                               if not e.get("requires_env") and not e.get("file_sha256")),
            "issues": issues, "counts": counts,
            "ok": counts["error"] == 0,
        }

    @app.get("/api/datasets/{dsid}/download")
    async def download_dataset(dsid: str, request: Request):
        """导出该数据集的用例文件（JSONL）。仅可运行集可取。"""
        _enforce(Cap.EXPORT, request)
        entry = dc.get_entry(dsid)
        if entry is None:
            raise HTTPException(404, f"数据集不存在：{dsid}")
        rich = next((e for e in dc.list_datasets() if e["id"] == dsid), None)
        fp = (rich or {}).get("file_path")
        if not fp or not Path(fp).exists():
            raise HTTPException(400, "该数据集当前不可运行（文件未就位或需执行环境），无法导出")
        await db.record_audit("dataset_download", _caller(request),
                              f"{dsid} → {entry.get('suite_file')}", ok=True)
        return FileResponse(fp, filename=entry.get("suite_file") or f"{dsid}.jsonl",
                            media_type="application/x-ndjson")

    @app.post("/api/datasets")
    async def create_dataset(request: Request, payload: dict = Body(...)):
        """新建数据集条目；可同时写入用例文件。

        body: {"id","name","tier","domain","capabilities":[...], ...,
               "cases": ["{\\"id\\":\\"...\\",\\"input\\":...}", ...]   # 可选，JSONL 行
               "suite_file": "my_set.jsonl"}                        # 可选，默认 <id>.jsonl
        """
        _enforce(Cap.MANAGE, request)
        cases = payload.pop("cases", None) or None
        suite_file = payload.pop("suite_file", None)
        try:
            r = dsa.create(payload, cases, suite_file=suite_file)
        except ValueError as e:
            raise HTTPException(400, str(e))
        _invalidate_cases_index()
        await db.record_audit("dataset_create", _caller(request),
                              f"{r['id']} cases={len(cases) if cases else 0}", ok=True)
        return {"ok": True, "id": r["id"], "entry": r["entry"], "backup": r["backup"]}

    @app.patch("/api/datasets/{dsid}")
    async def patch_dataset(dsid: str, request: Request, payload: dict = Body(...)):
        """编辑元数据（白名单字段；id 不可改）。

        body: {"scenario": "...", "est_duration_min": 5, "capabilities": ["privacy"], ...}
        """
        _enforce(Cap.MANAGE, request)
        try:
            r = dsa.patch(dsid, payload)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if r["changed"]:
            await db.record_audit("dataset_patch", _caller(request),
                                  f"{dsid} fields={sorted(r['changed'])}", ok=True)
        return {"ok": True, "id": dsid, "changed": r["changed"],
                "entry": r["entry"], "backup": r["backup"]}

    @app.delete("/api/datasets/{dsid}")
    async def delete_dataset(dsid: str, request: Request, purge_file: bool = False):
        """删除数据集条目。purge_file=true 时样本文件移入 datasets/.trash/（不真删）。"""
        _enforce(Cap.DELETE, request)
        try:
            r = dsa.delete(dsid, purge_file=purge_file)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'"))
        _invalidate_cases_index()
        await db.record_audit("dataset_delete", _caller(request),
                              f"{dsid} purge_file={purge_file} moved={r['file_moved_to']}", ok=True)
        return {"ok": True, "id": dsid, "removed": r["removed"],
                "file_moved_to": r["file_moved_to"], "backup": r["backup"]}

    @app.post("/api/datasets/{dsid}/cases")
    async def import_dataset_cases(dsid: str, request: Request, payload: dict = Body(...)):
        """导入用例（JSONL 文本）。mode=replace 覆盖 / append 追加。导入后同步 cases_count。"""
        _enforce(Cap.MANAGE, request)
        text = str(payload.get("text") or "")
        mode = str(payload.get("mode") or "replace")
        if mode not in ("replace", "append"):
            raise HTTPException(400, "mode 只能是 replace 或 append")
        if not text.strip():
            raise HTTPException(400, "text 为空——请粘贴 JSONL 内容（一行一条用例）")
        try:
            r = dsa.import_cases(dsid, text, mode=mode)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        _invalidate_cases_index()   # 预览器必须看到新导入的题，而不是旧索引
        await db.record_audit("dataset_import_cases", _caller(request),
                              f"{dsid} {mode} cases={r['cases']}", ok=True)
        return {"ok": True, **r}

    @app.get("/api/jobs")
    async def jobs():
        return {"jobs": list(_jobs.values())}

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str):
        # R3：运行中任务实时进度（done/total/last_case/status）
        j = _jobs.get(job_id)
        if j is None:
            raise HTTPException(404, "任务不存在或已超出内存保留期")
        return j

    @app.post("/api/runs")
    async def launch_eval(req: NewEvalRequest, request: Request):
        _enforce(Cap.CREATE_RUN, request)
        from ..core.engine import run_suite_async

        try:
            suite_path = _resolve_suite(req.suite)
        except FileNotFoundError as e:
            raise HTTPException(400, str(e))

        # R5 / R6：解析提供方 / Judge（自定义配置优先；无配置回退注册表内置）
        provider_name, provider_kwargs = await _resolve_provider(req.provider, req.provider_mode)
        judge_name, judge_kwargs = await _resolve_judge(req.judge_provider)

        job_id = f"job_{int(asyncio.get_event_loop().time()*1000)}"
        run_id = _new_run_id()
        _jobs[job_id] = {
            "id": job_id, "suite": req.suite, "status": "running",
            "started_at": asyncio.get_event_loop().time(), "run_id": run_id,
            "provider": provider_name, "done": 0, "total": 0, "last_case": None,
        }
        run_name = req.run_name or f"{Path(req.suite).stem}@{req.provider}"

        def _progress(done: int, total: int, last_case: dict):
            # R3：实时进度回调（引擎每题完成调用一次；同步函数，引擎内部 await 兼容）
            j = _jobs.get(job_id)
            if j:
                j["done"] = done
                j["total"] = total
                j["last_case"] = last_case
                j["status"] = "running"

        async def _runner():
            try:
                # O14：工作台回链 meta（contract_id / callback_url）随运行落库，供双向回查
                wb_meta = None
                if req.wb_callback_url or req.wb_contract_id:
                    wb_meta = {}
                    if req.wb_callback_url:
                        wb_meta["callback_url"] = req.wb_callback_url
                    if req.wb_contract_id:
                        wb_meta["contract_id"] = req.wb_contract_id
                results = await run_suite_async(
                    suite_path, provider_name=provider_name,
                    provider_kwargs=provider_kwargs,
                    judge_provider_name=judge_name,
                    judge_provider_kwargs=judge_kwargs,
                    default_grader=req.grader, trials=req.trials,
                    trial_policy=req.trial_policy, three_way=req.three_way,
                    store=db, run_name=run_name,
                    model_version=req.model_version, dataset_version=req.dataset_version,
                    judge_budget=req.judge_budget, wb_meta=wb_meta,
                    params=req.params, run_id=run_id, progress_callback=_progress,
                )
                _jobs[job_id]["status"] = "done"
                _jobs[job_id]["cases"] = len(results)
            except Exception as e:  # noqa: BLE001
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = str(e)

        asyncio.create_task(_runner())
        # R4：非阻塞立即返回（run_id 立即可用于进度轮询），前端不卡顿
        return {"job_id": job_id, "run_id": run_id, "status": "launched", "suite": req.suite}

    # -----------------------------------------------------------------------
    # 批量评测：多选数据集 → 拆成 N 个独立 run（各自保留版本钉 / 评分器 / 报告）
    # -----------------------------------------------------------------------
    async def _batch_runner(batch_id: str, it: dict, req: NewBatchRequest, wb_meta,
                            run_id: str, job_id: str):
        from ..core.engine import run_suite_async
        dsid = it["dataset_id"]
        # R5 / R6：解析提供方 / Judge（自定义配置优先）
        provider_name, provider_kwargs = await _resolve_provider(req.provider, req.provider_mode)
        judge_name, judge_kwargs = await _resolve_judge(req.judge_provider)
        run_name = f"{req.run_name_prefix}::{dsid}"

        def _progress(done: int, total: int, last_case: dict):
            j = _jobs.get(job_id)
            if j:
                j["done"] = done
                j["total"] = total
                j["last_case"] = last_case
                j["status"] = "running"

        async with _batch_sem:
            try:
                await run_suite_async(
                    dc.resolve_dataset_path(dsid),
                    provider_name=provider_name, provider_kwargs=provider_kwargs,
                    judge_provider_name=judge_name,
                    judge_provider_kwargs=judge_kwargs,
                    default_grader=it["grader"], trials=req.trials,
                    trial_policy=req.trial_policy, three_way=req.three_way,
                    store=db, run_name=run_name, model_version=req.model_version,
                    dataset_version=it["dataset_version"], judge_budget=1.0,
                    wb_meta=wb_meta, params={"dataset_id": dsid, "batch_id": batch_id},
                    run_id=run_id, progress_callback=_progress,
                )
                it["status"] = "done"
            except Exception as ex:  # noqa: BLE001
                it["status"] = "error"
                it["error"] = str(ex)
            it["finished_at"] = asyncio.get_event_loop().time()

    @app.post("/api/runs/batch")
    async def launch_batch_eval(req: NewBatchRequest, request: Request):
        _enforce(Cap.CREATE_RUN, request)
        # 延迟导入，避免循环
        from ..core.engine import run_suite_async
        from ..core.providers import PROVIDERS

        if not req.datasets:
            raise HTTPException(400, "datasets 不能为空")
        ids = list(dict.fromkeys(req.datasets))  # 去重保序
        cat = {e["id"]: e for e in dc.list_datasets()}
        global_grader = None
        if req.grader.strip():
            known_g = _known_graders()
            global_grader = [g for g in _normalize_graders(req.grader, known_g) if g in known_g]
            if not global_grader:
                raise HTTPException(400, f"grader 解析后为空或全部未注册：{req.grader}")

        items, skipped = [], []
        for dsid in ids:
            e = cat.get(dsid)
            if not e:
                skipped.append({"dataset_id": dsid, "reason": "目录未收录"})
                continue
            if not e.get("runnable"):
                skipped.append({"dataset_id": dsid,
                                "reason": e.get("blockers") or (e.get("status") or "未就位")})
                continue
            if global_grader:
                grader = ",".join(global_grader)
            else:
                rec = e.get("graders") or []
                if not rec:
                    rec = _normalize_graders(e.get("recommended_grader", ""), _known_graders())
                grader = ",".join(rec) if rec else "code"
            dv = req.dataset_version.strip()
            # manual / 空 = 不覆盖，沿用各数据集自己的版本钉（避免把"未钉"当成覆盖值）
            version = dv if (dv and dv != "manual") else (e.get("version") or "manual")
            items.append({
                "dataset_id": dsid, "name": e.get("name"), "domain": e.get("domain"),
                "tier": e.get("tier"), "grader": grader, "dataset_version": version,
                "status": "running", "error": None,
            })

        if not items:
            return {"batch_id": None, "created": 0, "skipped": skipped, "jobs": [],
                    "message": "没有可运行的数据集（全部跳过）"}

        batch_id = f"batch_{int(asyncio.get_event_loop().time()*1000)}"
        _batches[batch_id] = {
            "batch_id": batch_id, "created_at": asyncio.get_event_loop().time(),
            "prefix": req.run_name_prefix, "items": items,
            "provider": req.provider, "trials": req.trials,
        }
        wb_meta = None
        if req.wb_callback_url or req.wb_contract_id:
            wb_meta = {}
            if req.wb_callback_url:
                wb_meta["callback_url"] = req.wb_callback_url
            if req.wb_contract_id:
                wb_meta["contract_id"] = req.wb_contract_id

        jobs = []
        for it in items:
            job_id = f"job_{int(asyncio.get_event_loop().time()*1000)}_{it['dataset_id']}"
            run_id = _new_run_id()
            _jobs[job_id] = {"id": job_id, "suite": it["dataset_id"], "status": "running",
                             "started_at": asyncio.get_event_loop().time(), "run_id": run_id,
                             "batch_id": batch_id, "done": 0, "total": 0, "last_case": None}
            it["run_id"] = run_id
            jobs.append(job_id)
            asyncio.create_task(_batch_runner(batch_id, it, req, wb_meta, run_id, job_id))
        await db.record_audit("batch_eval_launch", _caller(request),
                              f"{batch_id} {len(items)} 集 / skip {len(skipped)}", ok=True)
        return {"batch_id": batch_id, "created": len(items), "skipped": skipped,
                "jobs": jobs, "status": "launched"}

    @app.get("/api/runs/batch/{batch_id}")
    async def get_batch(batch_id: str):
        b = _batches.get(batch_id)
        if not b:
            # 内存态丢失（服务重启）：尝试从 DB 按 batch_id 恢复骨架
            runs0 = await db.list_runs(limit=2000)
            recovered = [{"dataset_id": r["params"]["dataset_id"], "status": "done"}
                         for r in runs0 if (r.get("params") or {}).get("batch_id") == batch_id]
            if not recovered:
                raise HTTPException(404, "批次不存在或已超出内存保留期")
            b = {"batch_id": batch_id, "created_at": 0, "prefix": "",
                 "items": recovered, "provider": "", "trials": 0}
        # 拉 DB 运行，按 batch_id + dataset_id 建映射（list_runs 已按时间倒序，首个即最新）
        runs = await db.list_runs(limit=2000)
        run_by_ds: dict[str, dict] = {}
        for r in runs:
            p = r.get("params") or {}
            if p.get("batch_id") == batch_id and p.get("dataset_id"):
                run_by_ds.setdefault(p["dataset_id"], r)

        rows = []
        for it in b["items"]:
            dsid = it["dataset_id"]
            r = run_by_ds.get(dsid)
            row = {
                "dataset_id": dsid, "name": it.get("name"),
                "domain": it.get("domain"), "tier": it.get("tier"),
                "grader": it.get("grader"), "dataset_version": it.get("dataset_version"),
                "status": it.get("status", "running"), "error": it.get("error"),
            }
            if r:
                row.update({
                    "run_id": r["id"], "pass_rate": r["pass_rate"],
                    "avg_pass_at_k": r["avg_pass_at_k"], "total": r["total"],
                    "passed": r["passed"], "inconclusive": r["inconclusive"],
                })
            rows.append(row)

        # 矩阵：按域聚合（加权通过率）
        domains: dict[str, dict] = {}
        for row in rows:
            d = row.get("domain") or "other"
            dd = domains.setdefault(d, {"domain": d, "rows": [], "cases": 0, "passed": 0})
            dd["rows"].append(row)
            if row.get("total"):
                dd["cases"] += row["total"]
                dd["passed"] += row.get("passed", 0)
        matrix = [{
            "domain": d, "count": len(dd["rows"]),
            "domain_pass_rate": (dd["passed"] / dd["cases"]) if dd["cases"] else None,
            "cases": dd["cases"], "passed": dd["passed"], "rows": dd["rows"],
        } for d, dd in domains.items()]

        done = sum(1 for r in rows if r.get("status") == "done")
        running = sum(1 for r in rows if r.get("status") == "running")
        failed = sum(1 for r in rows if r.get("status") == "error")
        all_passed = sum(r.get("passed", 0) or 0 for r in rows)
        all_cases = sum(r.get("total", 0) or 0 for r in rows)
        return {
            "batch_id": batch_id, "created_at": b.get("created_at"),
            "prefix": b.get("prefix", ""), "items": rows, "matrix": matrix,
            "summary": {
                "selected": len(rows), "done": done, "running": running, "failed": failed,
                "total_cases": sum(r.get("total", 0) or 0 for r in rows),
                "overall_pass_rate": (all_passed / all_cases) if all_cases else None,
            },
        }

    # -----------------------------------------------------------------------
    # R5：执行提供方（被测对象）自定义配置 CRUD（替代仅下拉框选择）
    # -----------------------------------------------------------------------
    @app.get("/api/providers")
    async def list_providers_api():
        from ..core.providers import PROVIDERS
        if db is None:
            return {"providers": [], "builtins": []}
        custom = await db.list_provider_configs()
        builtins = [{
            "name": k, "display_name": _PROVIDER_DISPLAY.get(k, k),
            "kind": k, "builtin": True, "base_url": "", "api_key_set": False,
            "model": "", "extra": {},
        } for k in PROVIDERS.available() if k in _SUT_KINDS]
        # 自定义配置可与内置同名（覆盖展示），这里并列展示
        return {"providers": builtins + custom, "builtins": builtins}

    @app.post("/api/providers")
    async def create_provider_api(req: ConfigRequest, request: Request):
        _enforce(Cap.MANAGE, request)
        if db is None:
            raise HTTPException(500, "未连接数据库")
        if not req.name or not req.kind:
            raise HTTPException(400, "name 与 kind 必填")
        pid = await db.save_provider_config(
            req.name, req.display_name or req.name, req.kind,
            base_url=req.base_url, api_key=req.api_key, model=req.model, extra=req.extra,
        )
        await db.record_audit("provider_create", _caller(request), req.name, ok=True)
        return {"ok": True, "id": pid, "name": req.name}

    @app.delete("/api/providers/{name}")
    async def delete_provider_api(name: str, request: Request):
        _enforce(Cap.DELETE, request)
        if db is None:
            raise HTTPException(500, "未连接数据库")
        ok = await db.delete_provider_config(name)
        if not ok:
            raise HTTPException(404, "提供方不存在")
        await db.record_audit("provider_delete", _caller(request), name, ok=True)
        return {"ok": True}

    # -----------------------------------------------------------------------
    # R6：Judge 通道（评测用 LLM）自定义配置 CRUD（支持用户自定义）
    # -----------------------------------------------------------------------
    @app.get("/api/judges")
    async def list_judges_api():
        from ..core.providers import PROVIDERS
        if db is None:
            return {"judges": [], "builtins": []}
        custom = await db.list_judge_configs()
        builtins = [{
            "name": k, "display_name": _JUDGE_DISPLAY.get(k, k),
            "kind": k, "builtin": True, "base_url": "", "api_key_set": False,
            "model": "", "extra": {},
        } for k in PROVIDERS.available() if k in _JUDGE_KINDS]
        return {"judges": builtins + custom, "builtins": builtins}

    @app.post("/api/judges")
    async def create_judge_api(req: ConfigRequest, request: Request):
        _enforce(Cap.MANAGE, request)
        if db is None:
            raise HTTPException(500, "未连接数据库")
        if not req.name or not req.kind:
            raise HTTPException(400, "name 与 kind 必填")
        jid = await db.save_judge_config(
            req.name, req.display_name or req.name, req.kind,
            base_url=req.base_url, api_key=req.api_key, model=req.model, extra=req.extra,
        )
        await db.record_audit("judge_create", _caller(request), req.name, ok=True)
        return {"ok": True, "id": jid, "name": req.name}

    @app.delete("/api/judges/{name}")
    async def delete_judge_api(name: str, request: Request):
        _enforce(Cap.DELETE, request)
        if db is None:
            raise HTTPException(500, "未连接数据库")
        ok = await db.delete_judge_config(name)
        if not ok:
            raise HTTPException(404, "Judge 配置不存在")
        await db.record_audit("judge_delete", _caller(request), name, ok=True)
        return {"ok": True}

    # -----------------------------------------------------------------------
    # R8：运行缺陷清单（聚合失败 / 不可判用例）
    # -----------------------------------------------------------------------
    @app.get("/api/runs/{rid}/defects")
    async def run_defects(rid: str):
        if db is None:
            raise HTTPException(500, "未连接数据库")
        r = await db.get_run(rid)
        if r is None:
            raise HTTPException(404, "run not found")
        return await db.list_run_defects(rid)

    # -----------------------------------------------------------------------
    # R2：多运行统一汇总报告（支持任意 run_ids 或整批 batch_id）
    # -----------------------------------------------------------------------
    @app.post("/api/runs/summary")
    async def runs_summary(req: SummaryRequest):
        if db is None:
            raise HTTPException(500, "未连接数据库")
        runs: list = []
        if req.batch_id:
            all_runs = await db.list_runs(limit=2000)
            runs = [r for r in all_runs
                    if (r.get("params") or {}).get("batch_id") == req.batch_id]
        if req.run_ids:
            wanted = set(req.run_ids)
            all_runs = await db.list_runs(limit=2000)
            runs = [r for r in all_runs if r["id"] in wanted]
        if not runs:
            raise HTTPException(404, "没有可汇总的运行")
        # 去重（按 id 保留首个）
        seen: set = set()
        uniq: list = []
        for r in runs:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            uniq.append(r)
        runs = uniq

        total_cases = sum(r.get("total", 0) or 0 for r in runs)
        all_passed = sum(r.get("passed", 0) or 0 for r in runs)
        all_inc = sum(r.get("inconclusive", 0) or 0 for r in runs)
        by_provider: dict = {}
        for r in runs:
            p = r.get("provider") or "unknown"
            d = by_provider.setdefault(p, {"provider": p, "runs": 0, "cases": 0, "passed": 0})
            d["runs"] += 1
            d["cases"] += r.get("total", 0) or 0
            d["passed"] += r.get("passed", 0) or 0
        matrix = [{
            "provider": p, "runs": d["runs"], "cases": d["cases"],
            "passed": d["passed"],
            "pass_rate": (d["passed"] / d["cases"]) if d["cases"] else None,
        } for p, d in by_provider.items()]
        return {
            "count": len(runs),
            "runs": [{
                "id": r["id"], "name": r["name"], "provider": r.get("provider"),
                "judge_provider": r.get("judge_provider"), "status": r.get("status"),
                "total": r.get("total"), "passed": r.get("passed"),
                "inconclusive": r.get("inconclusive"), "pass_rate": r.get("pass_rate"),
                "avg_pass_at_k": r.get("avg_pass_at_k"),
                "start_time": r.get("start_time"), "end_time": r.get("end_time"),
            } for r in runs],
            "matrix": matrix,
            "summary": {
                "total_cases": total_cases,
                "overall_pass_rate": (all_passed / total_cases) if total_cases else None,
                "passed": all_passed, "inconclusive": all_inc,
            },
        }

    # ---- 静态托管 ----
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/")
        async def index():
            return FileResponse(str(STATIC_DIR / "index.html"))

    return app


def main():
    parser = argparse.ArgumentParser(description="智化融合评测 Harness 控制台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--db-url", default=None)
    args = parser.parse_args()
    import uvicorn
    app = create_app(args.db_url)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
