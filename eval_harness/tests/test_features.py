"""N4/N7/N10/N11 四项特性的集成测试。

每个测试在单个 asyncio.run 内完成「建库 → 灌数据 → 断言」，避免跨事件循环导致
SQLAlchemy 引擎绑定到已关闭 loop 的 classic 错误。
"""
import asyncio
import json
import os

from eval_harness.core.persistence import Database
from eval_harness.core.engine import run_suite_async, aggregate
from eval_harness.core.gate import GateConfig, evaluate_gate
from eval_harness.core import lm_eval_loader, export as export_mod, dashboards as dashboards_mod

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")


def _seed(tmp_path, name="seed"):
    async def _go():
        db = Database(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        await db.init()
        results = await run_suite_async(
            os.path.join(EXAMPLES, "practice3_smoke.jsonl"),
            provider_name="mock", provider_kwargs={"mode": "full"},
            default_grader="code", trials=2, store=db, run_name=name,
        )
        return db, results
    return asyncio.run(_go())


# ---------------- N4 · CI 质量闸门 ----------------
def test_gate_pass_and_fail():
    agg = {"total": 10, "passed": 9, "inconclusive": 0, "pass_rate": 0.9,
           "by_category": {"safety": {"total": 2, "passed": 2, "pass_rate": 1.0}}}
    assert evaluate_gate(agg, None, GateConfig(pass_rate_min=0.8)).passed
    assert not evaluate_gate(agg, None, GateConfig(pass_rate_min=0.95)).passed
    # inconclusive_max=0 表示不允许任何熔断：含 1 个 inconclusive 应失败
    agg_inc = {"total": 10, "passed": 9, "inconclusive": 1, "pass_rate": 0.9, "by_category": {}}
    assert not evaluate_gate(agg_inc, None, GateConfig(inconclusive_max=0.0, pass_rate_min=0.0)).passed


def test_gate_regression():
    agg = {"total": 10, "passed": 8, "inconclusive": 0, "pass_rate": 0.8, "by_category": {}}
    base = {"pass_rate": 0.9}
    assert not evaluate_gate(agg, base, GateConfig(regression_max_drop=0.05)).passed  # 跌 10pp
    assert evaluate_gate(agg, base, GateConfig(regression_max_drop=0.2)).passed


def test_gate_junit_and_json():
    agg = {"total": 4, "passed": 4, "inconclusive": 0, "pass_rate": 1.0, "by_category": {}}
    g = evaluate_gate(agg, None, GateConfig(pass_rate_min=0.8))
    assert g.exit_code() == 0
    xml = g.junit_xml()
    assert "<testsuite" in xml and "<failure" not in xml
    assert "passed" in g.to_dict()


# ---------------- N11 · lm-eval loader ----------------
def test_lm_eval_loader(tmp_path):
    sample = tmp_path / "samples_test.jsonl"
    sample.write_text(json.dumps({
        "doc_id": "1", "prompt": "1+1?", "filtered_resps": ["2"],
        "returns": "2", "metrics": {"acc": 1.0},
    }) + "\n" + json.dumps({
        "doc_id": "2", "prompt": "2+2?", "filtered_resps": ["5"],
        "returns": "4", "metrics": {"acc": 0.0},
    }) + "\n", encoding="utf-8")

    async def _go():
        db = Database(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        await db.init()
        rid = await lm_eval_loader.ingest_lm_eval(db, str(sample), "lmeval-run",
                                                   metric_threshold=0.5, suite="math")
        run = await db.get_run(rid)
        return run
    run = asyncio.run(_go())
    assert run["passed"] == 1 and run["total"] == 2  # 1 通过 / 2 题
    assert run["model_version"] == "lmeval-run"


# ---------------- N10 · 日志检索 + 数据出库 ----------------
def test_search(tmp_path):
    db, _ = _seed(tmp_path)
    r = asyncio.run(db.search("seed"))
    assert any(x["name"] == "seed" for x in r["runs"])
    asyncio.run(db.close())


def test_export_jsonl(tmp_path):
    db, _ = _seed(tmp_path)
    out = tmp_path / "out"
    info = asyncio.run(export_mod.export(db, str(out), fmt="jsonl", partition="date"))
    assert info["runs"] == 1 and info["rows"] > 0
    parts = list(out.rglob("*.jsonl"))
    assert parts
    # 校验分区目录存在
    assert any(p.parent.name.startswith("date=") for p in parts)
    asyncio.run(db.close())


def test_export_csv(tmp_path):
    db, _ = _seed(tmp_path)
    out = tmp_path / "outcsv"
    info = asyncio.run(export_mod.export(db, str(out), fmt="csv", partition="none"))
    assert info["fmt"] == "csv"
    assert list(out.rglob("*.csv"))
    asyncio.run(db.close())


# ---------------- N7 · 自定义看板 ----------------
def test_dashboard_crud_and_render(tmp_path):
    db, _ = _seed(tmp_path)
    did = asyncio.run(db.save_dashboard("周报", {"widgets": [
        {"type": "kpi", "title": "平均通过率", "metric": "avg_pass_rate"},
        {"type": "barchart", "title": "按提供方",
         "sql": "SELECT provider AS label, COUNT(*) AS value FROM runs GROUP BY provider",
         "chart_type": "bar"},
    ]}))
    assert did
    lst = asyncio.run(db.list_dashboards())
    assert any(d["id"] == did for d in lst)
    widgets = asyncio.run(dashboards_mod.compute_dashboard({"widgets": [
        {"type": "kpi", "title": "平均通过率", "metric": "avg_pass_rate"},
        {"type": "barchart", "title": "按提供方",
         "sql": "SELECT provider AS label, COUNT(*) AS value FROM runs GROUP BY provider",
         "chart_type": "bar"},
    ]}, db))
    kpi = [w for w in widgets if w["type"] == "kpi"][0]
    assert kpi["value"] >= 0
    chart = [w for w in widgets if w["type"] == "bar"][0]
    assert len(chart["labels"]) >= 1 and len(chart["values"]) >= 1
    # 删除
    assert asyncio.run(db.delete_dashboard(did)) is True
    asyncio.run(db.close())
