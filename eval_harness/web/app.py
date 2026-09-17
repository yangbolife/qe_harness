"""eval_harness · Apple 风格可视化控制台（创新⑦）

面向内部评测人员的 Web 控制台：
- FastAPI 后端：读取持久化层(PostgreSQL/SQLite)数据，提供 REST + 静态托管
- 单页 SPA（web/static）：概览 / 运行列表 / 运行详情 / 生产轨迹 / 用例集 / 新建评测
- 直接复用 engine.run_suite_async（含 6 创新点落库）启动评测

启动：
    python -m eval_harness.web            # 默认 sqlite
    EVAL_DB_URL=postgresql+asyncpg://... python -m eval_harness.web
    python -m eval_harness.web --port 8848 --db-url postgresql+asyncpg://...
"""
from __future__ import annotations

import argparse
import asyncio
import os
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

HERE = Path(__file__).resolve().parent
EXAMPLES_DIR = HERE.parent / "examples"
STATIC_DIR = HERE / "static"

db: Optional[Database] = None
# 在途评测任务（内存态，仅用于「运行中」提示）
_jobs: dict[str, dict] = {}

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


def _resolve_suite(suite: str) -> str:
    """把 suite（catalog id / 文件名 / 绝对路径）解析为可加载的 JSONL 路径。

    优先级：绝对路径 → catalog 条目 suite_file（datasets/ 与 examples/ 都查）
    → 裸文件名（datasets/ 与 examples/ 都查）。
    """
    try:
        return dc.resolve_dataset_path(suite)
    except FileNotFoundError:
        raise HTTPException(400, f"找不到用例集：{suite}（不在 catalog/datasets/examples 下）")


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
        """返回评测数据集目录（分级 + 决策字段）。

        数据集按粗粒度分级返回：基础 / 进阶 / 深度 / 专家(环境)，外加开发/演示 fixtures。
        每条带评测人员决策所需的：分级、状态、实际用例数、估算耗时、估算 token、
        适用场景、推荐评分器、许可与是否可商用、是否需执行环境。
        """
        tiers = {}
        for tier, items in dc.group_by_tier().items():
            tiers[tier] = {
                "label": dc.TIER_LABELS.get(tier, tier),
                "datasets": [
                    {
                        "id": e["id"], "name": e["name"], "tier": e["tier"],
                        "status": e["status"], "status_label": e["status_label"],
                        "runnable": e["runnable"], "cases": e["actual_cases"],
                        "category": e.get("category", ""), "recommended_grader": e.get("recommended_grader", ""),
                        "est_duration_min": e.get("est_duration_min"), "est_tokens": e.get("est_tokens"),
                        "license": e.get("license", ""), "commercial_use": e.get("commercial_use"),
                        "requires_env": bool(e.get("requires_env")), "scenario": e.get("scenario", ""),
                        "source": e.get("source", ""), "official_url": e.get("official_url", ""),
                        "full_scale": e.get("full_scale", ""), "languages": e.get("languages", []),
                    }
                    for e in items
                ],
            }
        return {"tiers": tiers, "fixtures": dc.list_fixtures(), "summary": dc.summary()}

    @app.get("/api/jobs")
    async def jobs():
        return {"jobs": list(_jobs.values())}

    @app.post("/api/runs")
    async def launch_eval(req: NewEvalRequest, request: Request):
        _enforce(Cap.CREATE_RUN, request)
        # 延迟导入，避免循环
        from ..core.engine import run_suite_async
        from ..core.providers import PROVIDERS

        try:
            suite_path = _resolve_suite(req.suite)
        except FileNotFoundError as e:
            raise HTTPException(400, str(e))

        job_id = f"job_{int(asyncio.get_event_loop().time()*1000)}"
        _jobs[job_id] = {
            "id": job_id, "suite": req.suite, "status": "running",
            "started_at": asyncio.get_event_loop().time(), "run_id": None,
        }

        provider_kwargs = {}
        if req.provider == "mock":
            provider_kwargs = {"mode": req.provider_mode}
        judge_provider_name = req.judge_provider or None
        judge_provider_kwargs = None
        if judge_provider_name == "deepseek":
            key = os.environ.get("DEEPSEEK_API_KEY")
            if key:
                judge_provider_kwargs = {"api_key": key}
        run_name = req.run_name or f"{Path(req.suite).stem}@{req.provider}"

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
                    suite_path, provider_name=req.provider,
                    provider_kwargs=provider_kwargs,
                    judge_provider_name=judge_provider_name,
                    judge_provider_kwargs=judge_provider_kwargs,
                    default_grader=req.grader, trials=req.trials,
                    trial_policy=req.trial_policy, three_way=req.three_way,
                    store=db, run_name=run_name,
                    model_version=req.model_version, dataset_version=req.dataset_version,
                    judge_budget=req.judge_budget, wb_meta=wb_meta,
                )
                _jobs[job_id]["status"] = "done"
                _jobs[job_id]["cases"] = len(results)
            except Exception as e:  # noqa: BLE001
                _jobs[job_id]["status"] = "error"
                _jobs[job_id]["error"] = str(e)

        asyncio.create_task(_runner())
        return {"job_id": job_id, "status": "launched", "suite": req.suite}

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
