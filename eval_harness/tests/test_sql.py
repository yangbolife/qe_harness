"""N9 只读 SQL 查询层 + N3 git 代码版本钉 测试（mock，不烧 key）

覆盖：
- 语义闸：拒绝 INSERT/UPDATE/DROP/多语句
- 结构闸：视图 CTE 注入 + 外层 LIMIT 封顶
- 执行：真实只读查询在 SQLite 后端跑通（runs/cases/graders/trials/traces）
- describe_views：预置视图及其列
- format_table：等宽渲染不崩
- N3：run_suite_async 落库的 run 含 git_* 字段
"""
import os

os.environ.setdefault("EVAL_HARNESS_GIT", "0")  # 测试里不依赖具体仓库 git 状态

import pytest

from eval_harness.core.engine import run_suite_async
from eval_harness.core.persistence import Database
from eval_harness.core.sql import (
    SQLGuardError,
    compose,
    describe_views,
    format_table,
    run_query,
    validate,
)


# ---------------- 语义闸 ----------------
def test_validate_rejects_writes():
    for bad in (
        "INSERT INTO runs(name) VALUES ('x')",
        "UPDATE runs SET passed=1",
        "DELETE FROM cases",
        "DROP TABLE runs",
        "SELECT 1; SELECT 2",
        "ALTER TABLE runs ADD COLUMN x INT",
        "SELECT * FROM runs; DROP TABLE runs",
    ):
        with pytest.raises(SQLGuardError):
            validate(bad)


def test_validate_allows_select_and_with():
    # 字面量里含 'update' 不应误杀
    assert validate("SELECT input FROM cases WHERE input LIKE 'update me'") is not None
    assert validate("WITH x AS (SELECT 1) SELECT * FROM x") is not None


# ---------------- 结构闸 ----------------
def test_compose_injects_views_and_limit():
    out = compose("SELECT provider FROM runs", dialect="postgresql", limit=50)
    # 视图以 vw_ 别名注入，避免与同名基表冲突；用户写的 runs 被改写为 vw_runs
    assert "WITH vw_runs AS (" in out
    assert "FROM vw_runs" in out
    assert out.strip().endswith("LIMIT 50")
    # 用户自带更大 LIMIT 也越不过外层
    out2 = compose("SELECT * FROM cases LIMIT 99999", dialect="sqlite", limit=10)
    assert out2.strip().endswith("LIMIT 10")
    assert "FROM vw_cases" in out2


# ---------------- 执行（SQLite 后端） ----------------
@pytest.mark.asyncio
async def test_sql_run_query_readonly(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'eval_sql.db'}"
    db = Database(db_url)
    await db.init()
    await run_suite_async(
        "eval_harness/examples/practice3_smoke.jsonl",
        provider_name="mock", provider_kwargs={"mode": "full"},
        default_grader="code,judge", trials=2, trial_policy="any",
        store=db, run_name="sql-run", model_version="mock-full", dataset_version="v1",
    )
    # runs 视图可查
    res = await run_query(db, "SELECT provider, passed, total FROM runs")
    assert res["row_count"] == 1
    assert res["columns"] == ["provider", "passed", "total"]
    assert res["rows"][0]["provider"] == "mock"

    # 跨视图 JOIN：每题通过情况
    res2 = await run_query(
        db, "SELECT run_name, count(*) AS n FROM cases GROUP BY run_name"
    )
    assert res2["rows"][0]["n"] >= 1

    # graders 视图含 judge 成本
    res3 = await run_query(
        db, "SELECT grader, count(*) AS n FROM graders GROUP BY grader"
    )
    graders = {r["grader"]: r["n"] for r in res3["rows"]}
    assert "code" in graders and "judge" in graders

    # 容量闸生效
    res4 = await run_query(db, "SELECT * FROM cases", limit=1)
    assert res4["row_count"] <= 1

    await db.close()


# ---------------- describe_views ----------------
@pytest.mark.asyncio
async def test_describe_views(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'eval_views.db'}"
    db = Database(db_url)
    await db.init()
    views = await describe_views(db)
    names = {v["name"] for v in views}
    assert {"runs", "cases", "graders", "trials", "traces"} <= names
    runs_view = next(v for v in views if v["name"] == "runs")
    assert "provider" in runs_view["columns"]
    await db.close()


# ---------------- format_table ----------------
def test_format_table_renders():
    res = {
        "columns": ["a", "b"],
        "rows": [{"a": 1, "b": "x"}, {"a": 2, "b": "中文"}],
    }
    out = format_table(res)
    assert "a" in out and "b" in out
    assert "|" in out


# ---------------- N3 git 代码版本钉 ----------------
@pytest.mark.asyncio
async def test_git_metadata_wired(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'eval_git.db'}"
    db = Database(db_url)
    await db.init()
    await run_suite_async(
        "eval_harness/examples/practice3_smoke.jsonl",
        provider_name="mock", provider_kwargs={"mode": "full"},
        default_grader="code", trials=1, store=db, run_name="git-run",
    )
    runs = await db.list_runs()
    assert len(runs) == 1
    r = runs[0]
    # git 字段随运行落库（EVAL_HARNESS_GIT=0 时 available=False，字段仍应在）
    assert "git_available" in r
    assert "git_commit" in r
    assert r["git_available"] is False  # 测试环境禁用 git 采集
    await db.close()


# ---------------- N8 评测参数（Parameters） ----------------
def test_parse_params_cli():
    from eval_harness.cli import _parse_params
    p = _parse_params(["temperature=0.7", "top_p=0.9", "note=exp", "bad", "=noval"])
    assert p == {"temperature": 0.7, "top_p": 0.9, "note": "exp"}


@pytest.mark.asyncio
async def test_eval_params_recorded(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'eval_params.db'}"
    db = Database(db_url)
    await db.init()
    await run_suite_async(
        "eval_harness/examples/practice3_smoke.jsonl",
        provider_name="mock", provider_kwargs={"mode": "full"},
        default_grader="code", trials=1, store=db, run_name="params-run",
        params={"temperature": 0.7, "top_p": 0.9, "note": "exp"},
    )
    runs = await db.list_runs()
    assert runs[0]["params"] == {"temperature": 0.7, "top_p": 0.9, "note": "exp"}
    await db.close()
