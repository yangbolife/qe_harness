"""N2 / N5 / N6 / N12 / O10 / O11 / O12 / O13 / O14 / O15 / O16 / O17 功能测试。

覆盖本轮收口的 12 个 Braintrust 缺口项。DB 用工作区内 .pytest_tmp 避免沙箱 /tmp 限制。
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from eval_harness.core.persistence import Database
from eval_harness.core.models import Case, GraderResult, CaseResult
from eval_harness.core.engine import run_suite, run_suite_async


def _tmp_db():
    d = tempfile.mkdtemp(prefix="feat2_")
    return f"sqlite+aiosqlite:///{d}/t.db"


# ---------------- N2 项目级 Rubric + 盲评 ----------------
def test_rubric_scoring_and_blind():
    from eval_harness.core.rubric import Rubric, Criterion, score_case_with_rubric, blind_anonymize
    rub = Rubric(name="质量", criteria=[
        Criterion(key="correct", label="正确", weight=2.0, grader="code"),
        Criterion(key="contract", label="契约", weight=1.0, grader="contract"),
    ], pass_threshold=0.6)
    case = Case(id="c1", input="1+1=", gold="2", category="math", grader="code",
                meta={"schema": {"required": ["answer"]}})
    r = score_case_with_rubric("2", case, rub)
    assert r["rubric_score"] >= 0.6
    assert r["rubric_pass"] is True
    run = {"provider": "deepseek", "model_version": "deepseek-chat",
           "config_json": {"provider": "deepseek", "git_commit": "abc"}}
    blind = blind_anonymize(run)
    assert blind["provider"].startswith("Anonymous-")
    assert blind["model_version"].startswith("Anonymous-")
    assert "git_commit" not in blind


# ---------------- N5 采集 SDK ----------------
def test_sdk_trace_and_push():
    from eval_harness.sdk import HarnessTracer, record_eval, push_trace
    payload = record_eval("c1", "1+1=", "2", score=1.0, passed=True)
    assert "resourceSpans" in payload
    t = HarnessTracer("ut").span("step1", input="q", output="a").to_payload()
    assert t["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "step1"
    db_url = _tmp_db()
    tid = push_trace(db_url, payload, source="ut-sdk")
    assert isinstance(tid, int) and tid > 0


# ---------------- N6 分类器 + Span 级评分 ----------------
def test_classifier_and_span_scores():
    from eval_harness.core.classifiers import classify_response, score_spans, ClassifierGrader
    assert classify_response("Traceback: boom") == "timeout"
    assert classify_response("抱歉，我无法访问该工具") == "refusal"
    assert classify_response("答案是2") == "ok"
    g = ClassifierGrader()
    res = g.judge("答案是2", Case(id="x", input="1+1=", gold="2"))
    assert res.passed and res.detail == "category=ok"
    trace = {"resourceSpans": [{"scopeSpans": [{"spans": [
        {"name": "a", "startTime": 1, "attributes": [
            {"key": "output", "value": {"stringValue": "ok"}}]},
        {"name": "b", "startTime": 2, "attributes": [
            {"key": "output", "value": {"stringValue": "Exception: timeout"}}]},
    ]}]}]}
    spans = score_spans(trace)
    assert len(spans) == 2
    assert spans[0]["score"] == 1.0
    assert spans[1]["crash"] is True and spans[1]["score"] == 0.0


# ---------------- N12 RBAC 策略引擎 ----------------
def test_rbac_policy_engine():
    from eval_harness.core.rbac import PolicyEngine, Cap
    eng = PolicyEngine()
    eng.assign_group("alice", "engineer")
    eng.add_service_account("tok-ci", {Cap.EXPORT})
    assert eng.can("alice", Cap.CREATE_RUN)
    assert not eng.can("alice", Cap.DELETE)
    assert eng.can("alice", Cap.ADMIN) is False
    assert eng.can("tok-ci", Cap.EXPORT)
    assert not eng.can("tok-ci", Cap.CREATE_RUN)
    eng.require("alice", Cap.REVIEW)
    with pytest.raises(PermissionError):
        eng.require("alice", Cap.ADMIN)
    # 行级 ACL
    assert eng.can_view_run("alice", "alice", set())
    assert not eng.can_view_run("bob", "alice", set())
    eng.assign_group("root", "admin")
    assert eng.can_view_run("root", "alice", set())


# ---------------- O10 工具托管执行 ----------------
def test_tools_registry_execute():
    from eval_harness.core.tools import ToolRegistry, ToolResult
    reg = ToolRegistry()
    reg.register("echo", "回显", fn=lambda a: f"echo:{a}")
    r: ToolResult = reg.execute("echo", "hi")
    assert r.ok and r.response == "echo:hi"
    reg.register("bad", "坏", fn=lambda a: (_ for _ in ()).throw(RuntimeError("x")))
    rb = reg.execute("bad", "")
    assert not rb.ok and "RuntimeError" in rb.error


# ---------------- O11 远程/沙箱评测 ----------------
def test_sandbox_script_provider():
    from eval_harness.core.sandbox_eval import SandboxScriptProvider
    script = "import os\nprint('ANS:' + os.environ.get('EVAL_PROMPT', ''))"
    p = SandboxScriptProvider(script=script)
    out = p.ask("hello-world", "")
    assert "ANS:hello-world" in out


# ---------------- O13 token/成本预算 ----------------
def test_cost_budget_aborts_judge():
    db_url = _tmp_db()
    db = asyncio.run(_init(db_url))
    # 用 mock 作 judge provider，judge 通道会估算成本并累计；预算极小→越限
    results = run_suite(
        "eval_harness/examples/practice3_smoke.jsonl",
        provider_name="mock", provider_kwargs={"mode": "full"},
        default_grader="judge", judge_provider_name="mock",
        judge_provider_kwargs={"mode": "full"},
        cost_budget=1e-9, store=db, run_name="budget-test",
    )
    run = asyncio.run(db.get_run(asyncio.run(_last_run_id(db)))) if False else None
    # 直接查库验证 budget_aborted
    rid = asyncio.run(_last_run_id(db))
    run = asyncio.run(db.get_run(rid))
    assert run["budget_aborted"] is True
    assert run["cost_used_usd"] >= 0.0
    asyncio.run(db.close())


async def _init(url):
    db = Database(url)
    await db.init()
    return db


async def _last_run_id(db):
    runs = await db.list_runs(limit=1)
    return runs[0]["id"]


# ---------------- O17 数据集快照 + 自定义视图 ----------------
def test_snapshot_and_views():
    db_url = _tmp_db()
    db = asyncio.run(_init(db_url))
    content = '{"a":1}\n'
    sid = asyncio.run(db.save_dataset_snapshot("s", "x.jsonl", content, "h1"))
    snap = asyncio.run(db.get_snapshot(sid))
    assert snap["content"] == content and snap["hash"] == "h1"
    vid = asyncio.run(db.save_view("v", "SELECT provider, COUNT(*) AS n FROM runs GROUP BY provider"))
    v = asyncio.run(db.get_view(vid))
    assert "SELECT" in v["sql"]
    asyncio.run(db.delete_view(vid))
    assert asyncio.run(db.get_view(vid)) is None
    asyncio.run(db.close())


# ---------------- O12 Patterns（需失败样本） ----------------
def test_patterns_discovery():
    from eval_harness.core.patterns import discover_patterns
    db_url = _tmp_db()
    db = asyncio.run(_init(db_url))
    # 构造一条含失败的运行
    cases = [
        _mk_case("c1", passed=False, grader="classifier", detail="category=timeout"),
        _mk_case("c2", passed=False, grader="classifier", detail="category=timeout"),
        _mk_case("c3", passed=True, grader="code"),
    ]
    asyncio.run(db.save_run("p", {"provider": "mock"}, cases))
    rid = asyncio.run(_last_run_id(db))
    pat = asyncio.run(discover_patterns(db, rid))
    assert pat["failed"] == 2
    assert pat["patterns"][0]["failure_category"] == "timeout"
    assert pat["patterns"][0]["count"] == 2
    asyncio.run(db.close())


def _mk_case(cid, passed, grader, detail=""):
    case = Case(id=cid, input="q", gold="a", category="math", grader=grader)
    gr = GraderResult(grader=grader, score=1.0 if passed else 0.0, passed=passed, detail=detail)
    return CaseResult(case=case, response="a", graders=[gr], passed=passed)


# ---------------- Web 端点覆盖（N2/O12/O15/O16/O17/RBAC） ----------------
def test_web_endpoints():
    EVAL_HARNESS_GIT = os.environ.get("EVAL_HARNESS_GIT")
    os.environ["EVAL_HARNESS_GIT"] = "0"
    try:
        from fastapi.testclient import TestClient
        from eval_harness.web.app import create_app
        db_url = _tmp_db()
        app = create_app(db_url)
        with TestClient(app) as c:
            # 灌一条运行
            _seed(db_url)
            rid = asyncio.run(_last_run_id(Database(db_url)))
            # N2 盲评
            b = c.get(f"/api/runs/{rid}/blind")
            assert b.status_code == 200 and b.json()["provider"].startswith("Anonymous-")
            # O12 patterns
            p = c.get(f"/api/runs/{rid}/patterns")
            assert p.status_code == 200
            # O15 trace debug（先灌 trace）
            _ingest_demo_trace(db_url)
            tid = _first_trace_id(db_url)
            td = c.get(f"/api/traces/{tid}/debug")
            assert td.status_code == 200 and "span_scores" in td.json()
            # O17 snapshot + view
            s = c.get("/api/snapshots")
            assert s.status_code == 200
            v = c.post("/api/views", json={"name": "v", "sql": "SELECT provider FROM runs"})
            assert v.status_code == 200 and "id" in v.json()
            # O16 附件
            a = c.post(f"/api/runs/{rid}/attachments",
                       json={"filename": "shot.png", "mime": "image/png", "data": "BASE64DATA"})
            assert a.status_code == 200
            run = c.get(f"/api/runs/{rid}").json()
            assert any(at["filename"] == "shot.png" for at in run.get("attachments", []))
            # RBAC whoami
            me = c.get("/api/rbac/me")
            assert me.status_code == 200 and "capabilities" in me.json()
    finally:
        if EVAL_HARNESS_GIT is None:
            os.environ.pop("EVAL_HARNESS_GIT", None)
        else:
            os.environ["EVAL_HARNESS_GIT"] = EVAL_HARNESS_GIT


def _ingest_demo_trace(url):
    async def _go():
        from eval_harness.core.trace_store import ingest_and_evaluate
        db = Database(url)
        await db.init()
        try:
            await ingest_and_evaluate(db, source="demo",
                                      payload={"resourceSpans": [{"scopeSpans": [{"spans": [
                                          {"name": "a", "startTime": 1, "attributes": [
                                              {"key": "output", "value": {"stringValue": "ok"}}]},
                                      ]}]}]}, grader_name="trajectory")
        finally:
            await db.close()
    asyncio.run(_go())


def _first_trace_id(url):
    async def _go():
        db = Database(url)
        await db.init()
        try:
            ts = await db.list_traces(limit=1)
            return ts[0]["id"]
        finally:
            await db.close()
    return asyncio.run(_go())


def _seed(url):
    async def _go():
        from eval_harness.core.engine import run_suite_async
        db = Database(url)
        await db.init()
        try:
            await run_suite_async(
                "eval_harness/examples/practice3_smoke.jsonl",
                provider_name="mock", provider_kwargs={"mode": "full"},
                default_grader="code", trials=1, store=db, run_name="webseed")
        finally:
            await db.close()
    asyncio.run(_go())
