"""eval_harness · 数据持久化（Phase 4）

审视结论：框架此前**仅把结果落 JSON 文件**（`_dump`），无结构化持久化、无跨运行查询、
无版本管理、Web UI 无法按需检索。本模块补齐：

- 双后端：PostgreSQL(asyncpg) 生产 / SQLite(aiosqlite) 零配置本地，SQLAlchemy 2.0 async 统一。
- 表：case_sets(用例集版本) / runs(一次评测) / cases(逐题) / graders(逐评分器)
       / trials(逐次执行) / traces(生产 trace 回灌，创新⑥)。
- 版本钉（创新④）：run 落 model_version / dataset_version / harness_version。
- 脚手架三方解耦（创新③）：run 落 three_way_json。
- Judge 成本（创新⑤）：graders 落 tokens / cost_usd。

默认零配置用 SQLite（./eval_harness.db）；生产可接 Postgres：
    export EVAL_DB_URL=postgresql+asyncpg://USER:PASSWORD@localhost:5432/eval_harness
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Optional

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, Text, JSON, ForeignKey,
    select, func, text,
)
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# ----------------------------------------------------------------------------
Base = declarative_base()


class CaseSet(Base):
    """用例集版本（创新④ 版本钉 + 去污染）。"""
    __tablename__ = "case_sets"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    version = Column(String(64), nullable=False)     # 数据集版本（内容哈希）
    source_path = Column(Text)
    hash = Column(String(64))
    created_at = Column(Float, default=time.time)
    meta = Column(JSON)


class Run(Base):
    """一次评测运行（Outcome 聚合）。"""
    __tablename__ = "runs"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255))
    provider = Column(String(64))
    judge_provider = Column(String(64))
    grader = Column(String(64))
    trials = Column(Integer, default=1)
    trial_policy = Column(String(16))
    combine = Column(String(8))
    concurrency = Column(Integer)
    rate = Column(Float)
    created_at = Column(Float, default=time.time)
    total = Column(Integer, default=0)
    passed = Column(Integer, default=0)
    inconclusive = Column(Integer, default=0)
    pass_rate = Column(Float, default=0.0)
    avg_pass_at_k = Column(Float, default=0.0)
    config_json = Column(JSON)
    harness_version = Column(String(32))
    model_version = Column(String(64))    # 版本钉
    dataset_version = Column(String(64))  # 数据集版本钉
    three_way_json = Column(JSON)          # 脚手架三方解耦（创新③）
    # 代码版本钉（N3 并入增强）：随运行自动记录 git 来源，绝不中断评测
    git_available = Column(Boolean, default=False)
    git_commit = Column(String(64))
    git_branch = Column(String(64))
    git_tag = Column(String(64))
    git_dirty = Column(Boolean, default=False)
    git_author_name = Column(String(128))
    git_author_email = Column(String(128))
    git_commit_message = Column(Text)
    git_commit_time = Column(String(64))
    # —— RBAC 行级 ACL（N12）——
    owner = Column(String(128))                 # 发起者（X-Harness-User 头）
    visibility = Column(String(16), default="all")  # all / private
    # —— 外部关联键（跨系统）：由调用方的工作台/平台传入其评测运行 id，
    # 用于把 harness 原始执行记录关联回外部评测项目。可空、可重复（幂等重跑）。——
    external_id = Column(String(128), index=True)
    # —— O13：token/成本预算 —— #
    token_budget_usd = Column(Float, default=0.0)
    cost_used_usd = Column(Float, default=0.0)
    budget_aborted = Column(Boolean, default=False)
    # —— O16：多模态附件（元信息，base64 或路径） —— #
    attachments_json = Column(JSON)


class CaseRow(Base):
    """逐题结果。"""
    __tablename__ = "cases"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), ForeignKey("runs.id", ondelete="CASCADE"))
    case_id = Column(String(64))
    suite = Column(String(32))
    category = Column(String(64))
    difficulty = Column(String(16))
    input = Column(Text)
    gold = Column(Text)
    grader = Column(String(64))
    response = Column(Text)
    passed = Column(Boolean, default=False)
    inconclusive = Column(Boolean, default=False)
    k = Column(Integer, default=1)
    pass_rate = Column(Float)
    pass_at_k = Column(Float)
    pass_k = Column(Boolean)
    duration_ms = Column(Float)
    error = Column(Text)
    # 失败归类 + HTTP 状态码（环境类/缺陷分离用，对齐 qe-platform 桥接层口径）
    failure_class = Column(String(32))
    status_code = Column(Integer)
    graders = relationship("GraderRow", cascade="all,delete")
    trials = relationship("TrialRow", cascade="all,delete")


class GraderRow(Base):
    """逐评分器结果（含 judge 成本，创新⑤）。"""
    __tablename__ = "graders"
    id = Column(Integer, primary_key=True, autoincrement=True)
    case_row_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"))
    grader = Column(String(64))
    score = Column(Float)
    passed = Column(Boolean)
    detail = Column(Text)
    duration_ms = Column(Float)
    tokens = Column(Integer, default=0)
    cost_usd = Column(Float, default=0.0)


class TrialRow(Base):
    """逐次执行（k 次 Trial）。"""
    __tablename__ = "trials"
    id = Column(Integer, primary_key=True, autoincrement=True)
    case_row_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"))
    idx = Column(Integer)
    response = Column(Text)
    passed = Column(Boolean)
    error = Column(Text)
    failure_class = Column(String(32))
    status_code = Column(Integer)
    duration_ms = Column(Float)


class TraceRow(Base):
    """生产 trace 回灌（创新⑥）。"""
    __tablename__ = "traces"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), ForeignKey("runs.id", ondelete="CASCADE"), nullable=True)
    source = Column(String(255))
    payload_json = Column(JSON)
    created_at = Column(Float, default=time.time)


class Dashboard(Base):
    """自定义看板（N7）：name + config_json(widget 列表)。"""
    __tablename__ = "dashboards"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    config_json = Column(JSON)
    created_at = Column(Float, default=time.time)


# ----------------------------------------------------------------------------
class Database:
    """异步持久化门面：PostgreSQL / SQLite 双后端。"""

    def __init__(self, url: Optional[str] = None):
        self.url = url or os.environ.get("EVAL_DB_URL") or "sqlite+aiosqlite:///./eval_harness.db"
        # SQLite 相对路径相对 cwd；统一成绝对更稳
        if self.url.startswith("sqlite") and ":///" not in self.url.replace("sqlite+aiosqlite", "sqlite"):
            pass
        connect_args = {}
        if self.url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
        self.engine = create_async_engine(self.url, echo=False, future=True,
                                           pool_pre_ping=True, connect_args=connect_args)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    async def init(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # 旧库无 git 列时补充（create_all 不会 ALTER 已存在的表）
        await self._migrate_git_columns()
        await self._migrate_run_extra_columns()
        await self._migrate_external_id_column()
        await self._migrate_failure_class_columns()
        return self

    async def _migrate_failure_class_columns(self):
        """为已有 cases / trials 表补充 failure_class + status_code 列（O17 之后新增）。"""
        case_cols = await self._columns_of("cases")
        trial_cols = await self._columns_of("trials")
        async with self.engine.begin() as conn:
            if "failure_class" not in case_cols:
                await conn.execute(text("ALTER TABLE cases ADD COLUMN failure_class VARCHAR(32)"))
            if "status_code" not in case_cols:
                await conn.execute(text("ALTER TABLE cases ADD COLUMN status_code INTEGER"))
            if "failure_class" not in trial_cols:
                await conn.execute(text("ALTER TABLE trials ADD COLUMN failure_class VARCHAR(32)"))
            if "status_code" not in trial_cols:
                await conn.execute(text("ALTER TABLE trials ADD COLUMN status_code INTEGER"))

    async def _migrate_external_id_column(self):
        """为已有 runs 表补充 external_id 关联键列（N12 之后新增）。"""
        existing = await self._runs_columns()
        if "external_id" in existing:
            return
        async with self.engine.begin() as conn:
            await conn.execute(text("ALTER TABLE runs ADD COLUMN external_id VARCHAR(128)"))

    # N12/O13/O16 新增 Run 列的增量迁移（旧库兼容）
    _EXTRA_RUN_COLUMNS = (
        ("owner", "VARCHAR(128)"),
        ("visibility", "VARCHAR(16)"),
        ("token_budget_usd", "FLOAT"),
        ("cost_used_usd", "FLOAT"),
        ("budget_aborted", "BOOLEAN"),
        ("attachments_json", "JSON"),
    )

    async def _migrate_run_extra_columns(self):
        existing = await self._runs_columns()
        needed = [(name, ddl) for name, ddl in self._EXTRA_RUN_COLUMNS if name not in existing]
        if not needed:
            return
        async with self.engine.begin() as conn:
            for name, ddl in needed:
                await conn.execute(text(f"ALTER TABLE runs ADD COLUMN {name} {ddl}"))

    # git 代码版本钉列（N3 并入增强）的增量迁移
    _GIT_COLUMNS = (
        ("git_available", "BOOLEAN"),
        ("git_commit", "VARCHAR(64)"),
        ("git_branch", "VARCHAR(64)"),
        ("git_tag", "VARCHAR(64)"),
        ("git_dirty", "BOOLEAN"),
        ("git_author_name", "VARCHAR(128)"),
        ("git_author_email", "VARCHAR(128)"),
        ("git_commit_message", "TEXT"),
        ("git_commit_time", "VARCHAR(64)"),
    )

    async def _runs_columns(self) -> set:
        return await self._columns_of("runs")

    async def _columns_of(self, table: str) -> set:
        async with self.engine.connect() as conn:
            if self.url.startswith("sqlite"):
                rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
                return {r[1] for r in rows}
            rows = (await conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name=:t"
            ), {"t": table})).fetchall()
            return {r[0] for r in rows}

    async def _migrate_git_columns(self):
        existing = await self._runs_columns()
        needed = [(name, ddl) for name, ddl in self._GIT_COLUMNS if name not in existing]
        if not needed:
            return
        async with self.engine.begin() as conn:
            for name, ddl in needed:
                await conn.execute(text(f"ALTER TABLE runs ADD COLUMN {name} {ddl}"))

    async def close(self):
        await self.engine.dispose()

    # ---- 写入 ----
    async def save_run(self, name: str, meta: dict, results: list,
                       three_way: Optional[dict] = None, run_id: Optional[str] = None,
                       owner: Optional[str] = None, visibility: str = "all",
                       token_budget_usd: float = 0.0, cost_used_usd: float = 0.0,
                       budget_aborted: bool = False, attachments: Optional[list] = None,
                       external_id: Optional[str] = None) -> str:
        run_id = run_id or str(uuid.uuid4())
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        inc = sum(1 for r in results if r.inconclusive)
        avg_pk = (sum(r.pass_at_k for r in results) / total) if total else 0.0
        async with self.Session() as s:
            run = Run(
                id=run_id, name=name, provider=meta.get("provider"),
                judge_provider=meta.get("judge_provider"), grader=meta.get("grader"),
                trials=meta.get("trials", 1), trial_policy=meta.get("trial_policy"),
                combine=meta.get("combine"), concurrency=meta.get("concurrency"),
                rate=meta.get("rate"), total=total, passed=passed, inconclusive=inc,
                pass_rate=(passed / total if total else 0.0), avg_pass_at_k=avg_pk,
                config_json=meta, harness_version=meta.get("harness_version"),
                model_version=meta.get("model_version"), dataset_version=meta.get("dataset_version"),
                three_way_json=three_way,
                owner=owner, visibility=visibility,
                external_id=external_id,
                token_budget_usd=token_budget_usd, cost_used_usd=cost_used_usd,
                budget_aborted=budget_aborted, attachments_json=attachments,
                # 代码版本钉（N3 并入增强）
                git_available=bool(meta.get("git_available", False)),
                git_commit=meta.get("git_commit"),
                git_branch=meta.get("git_branch"),
                git_tag=meta.get("git_tag"),
                git_dirty=bool(meta.get("git_dirty", False)),
                git_author_name=meta.get("git_author_name"),
                git_author_email=meta.get("git_author_email"),
                git_commit_message=meta.get("git_commit_message"),
                git_commit_time=meta.get("git_commit_time"),
            )
            s.add(run)
            await s.flush()
            for r in results:
                c = CaseRow(
                    run_id=run_id, case_id=str(r.case.id), suite=r.case.suite,
                    category=r.case.category, difficulty=r.case.difficulty,
                    input=r.case.input, gold=r.case.gold,
                    grader=r.case.grader or meta.get("grader", ""),
                    response=r.response, passed=r.passed, inconclusive=r.inconclusive,
                    k=r.k, pass_rate=r.pass_rate, pass_at_k=r.pass_at_k, pass_k=r.pass_k,
                    duration_ms=r.duration_ms, error=r.error,
                    failure_class=r.failure_class or None, status_code=r.status_code,
                )
                s.add(c)
                await s.flush()
                for g in r.graders:
                    s.add(GraderRow(
                        case_row_id=c.id, grader=g.grader, score=g.score, passed=g.passed,
                        detail=g.detail, duration_ms=g.duration_ms,
                        tokens=getattr(g, "tokens", 0) or 0,
                        cost_usd=getattr(g, "cost_usd", 0.0) or 0.0,
                    ))
                for i, t in enumerate(r.trials):
                    s.add(TrialRow(
                        case_row_id=c.id, idx=i, response=t.response, passed=t.passed,
                        error=t.error, failure_class=getattr(t, "failure_class", "") or None,
                        status_code=getattr(t, "status_code", None),
                        duration_ms=t.duration_ms,
                    ))
            await s.commit()
        return run_id

    async def save_case_set(self, name: str, version: str, source_path: str,
                            hashv: str, meta: Optional[dict] = None) -> str:
        csid = str(uuid.uuid4())
        async with self.Session() as s:
            s.add(CaseSet(id=csid, name=name, version=version, source_path=source_path,
                          hash=hashv, meta=meta or {}))
            await s.commit()
        return csid

    async def ingest_trace(self, source: str, payload: dict, run_id: Optional[str] = None) -> int:
        async with self.Session() as s:
            t = TraceRow(run_id=run_id, source=source, payload_json=payload)
            s.add(t)
            await s.flush()
            tid = t.id
            await s.commit()
        return tid

    # ---- 查询 ----
    async def list_runs(self, limit: int = 50) -> list[dict]:
        async with self.Session() as s:
            rows = (await s.execute(
                select(Run).order_by(Run.created_at.desc()).limit(limit)
            )).scalars().all()
            return [_run_to_dict(r) for r in rows]

    async def get_run(self, run_id: str) -> Optional[dict]:
        async with self.Session() as s:
            r = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
            if r is None:
                return None
            cases = (await s.execute(
                select(CaseRow).where(CaseRow.run_id == run_id).order_by(CaseRow.id)
            )).scalars().all()
            out = _run_to_dict(r)
            out["cases"] = []
            for c in cases:
                grader_rows = (await s.execute(
                    select(GraderRow).where(GraderRow.case_row_id == c.id)
                )).scalars().all()
                trial_rows = (await s.execute(
                    select(TrialRow).where(TrialRow.case_row_id == c.id).order_by(TrialRow.idx)
                )).scalars().all()
                out["cases"].append({
                    "case_id": c.case_id, "suite": c.suite, "category": c.category,
                    "difficulty": c.difficulty, "input": c.input, "gold": c.gold,
                    "grader": c.grader, "response": c.response, "passed": c.passed,
                    "inconclusive": c.inconclusive, "k": c.k, "pass_rate": c.pass_rate,
                    "pass_at_k": c.pass_at_k, "pass_k": c.pass_k, "duration_ms": c.duration_ms,
                    "error": c.error, "failure_class": c.failure_class, "status_code": c.status_code,
                    "graders": [{"grader": g.grader, "score": g.score, "passed": g.passed,
                                 "detail": g.detail, "tokens": g.tokens, "cost_usd": g.cost_usd}
                                for g in grader_rows],
                    "trials": [{"idx": t.idx, "response": t.response, "passed": t.passed,
                                "error": t.error, "failure_class": t.failure_class,
                                "status_code": t.status_code, "duration_ms": t.duration_ms}
                               for t in trial_rows],
                })
            return out

    async def list_case_sets(self) -> list[dict]:
        async with self.Session() as s:
            rows = (await s.execute(select(CaseSet).order_by(CaseSet.created_at.desc()))).scalars().all()
            return [{"id": r.id, "name": r.name, "version": r.version, "hash": r.hash,
                     "source_path": r.source_path, "created_at": r.created_at} for r in rows]

    async def count_traces(self) -> int:
        async with self.Session() as s:
            return int((await s.execute(
                select(func.count()).select_from(TraceRow))).scalar() or 0)

    async def list_traces(self, limit: int = 50) -> list[dict]:
        async with self.Session() as s:
            rows = (await s.execute(
                select(TraceRow).order_by(TraceRow.created_at.desc()).limit(limit)
            )).scalars().all()
            return [{"id": r.id, "source": r.source, "created_at": r.created_at,
                     "payload": r.payload_json} for r in rows]

    # ---- N10：日志检索（跨 runs/cases/traces 的 LIKE 检索）----
    async def search(self, query: str, limit: int = 50) -> dict:
        q = f"%{query}%"
        async with self.Session() as s:
            runs = (await s.execute(
                select(Run).where(
                    (Run.name.ilike(q)) | (Run.provider.ilike(q)) |
                    (Run.model_version.ilike(q)) | (Run.git_commit.ilike(q))
                ).order_by(Run.created_at.desc()).limit(limit)
            )).scalars().all()
            cases = (await s.execute(
                select(CaseRow).where(
                    (CaseRow.input.ilike(q)) | (CaseRow.response.ilike(q)) |
                    (CaseRow.gold.ilike(q)) | (CaseRow.case_id.ilike(q))
                ).order_by(CaseRow.id.desc()).limit(limit)
            )).scalars().all()
        return {
            "query": query,
            "runs": [{"id": r.id, "name": r.name, "provider": r.provider,
                      "model_version": r.model_version, "pass_rate": r.pass_rate}
                     for r in runs],
            "cases": [{"run_id": c.run_id, "case_id": c.case_id, "suite": c.suite,
                       "category": c.category, "input": (c.input or "")[:120],
                       "response": (c.response or "")[:120], "passed": c.passed}
                      for c in cases],
            "traces": [],  # 占位：trace payload 体积大，检索走 /api/sql
        }

    # ---- N7：自定义看板 CRUD ----
    async def save_dashboard(self, name: str, config: dict, did: Optional[str] = None) -> str:
        did = did or str(uuid.uuid4())
        async with self.Session() as s:
            existing = (await s.execute(select(Dashboard).where(Dashboard.id == did))).scalar_one_or_none()
            if existing:
                existing.name = name
                existing.config_json = config
            else:
                s.add(Dashboard(id=did, name=name, config_json=config))
            await s.commit()
        return did

    async def list_dashboards(self) -> list[dict]:
        async with self.Session() as s:
            rows = (await s.execute(select(Dashboard).order_by(Dashboard.created_at.desc()))).scalars().all()
            return [{"id": r.id, "name": r.name, "widgets": len((r.config_json or {}).get("widgets", [])),
                     "created_at": r.created_at} for r in rows]

    async def get_dashboard(self, did: str) -> Optional[dict]:
        async with self.Session() as s:
            r = (await s.execute(select(Dashboard).where(Dashboard.id == did))).scalar_one_or_none()
            if r is None:
                return None
            return {"id": r.id, "name": r.name, "config": r.config_json or {"widgets": []},
                    "created_at": r.created_at}

    async def delete_dashboard(self, did: str) -> bool:
        async with self.Session() as s:
            r = (await s.execute(select(Dashboard).where(Dashboard.id == did))).scalar_one_or_none()
            if r is None:
                return False
            await s.delete(r)
            await s.commit()
            return True

    # ---- N2：项目级 Rubric CRUD ----
    async def save_rubric(self, name: str, criteria: dict, pass_threshold: float = 0.6,
                          rid: Optional[str] = None) -> str:
        rid = rid or str(uuid.uuid4())
        async with self.Session() as s:
            existing = (await s.execute(select(RubricRow).where(RubricRow.id == rid))).scalar_one_or_none()
            if existing:
                existing.name = name
                existing.criteria_json = criteria
                existing.pass_threshold = pass_threshold
            else:
                s.add(RubricRow(id=rid, name=name, criteria_json=criteria,
                                pass_threshold=pass_threshold))
            await s.commit()
        return rid

    async def list_rubrics(self) -> list[dict]:
        async with self.Session() as s:
            rows = (await s.execute(select(RubricRow).order_by(RubricRow.created_at.desc()))).scalars().all()
            return [{"id": r.id, "name": r.name, "criteria": len((r.criteria_json or {}).get("criteria", [])),
                     "pass_threshold": r.pass_threshold, "created_at": r.created_at} for r in rows]

    async def get_rubric(self, rid: str) -> Optional[dict]:
        async with self.Session() as s:
            r = (await s.execute(select(RubricRow).where(RubricRow.id == rid))).scalar_one_or_none()
            if r is None:
                return None
            return {"id": r.id, "name": r.name, "criteria": r.criteria_json or {"criteria": []},
                    "pass_threshold": r.pass_threshold, "created_at": r.created_at}

    async def delete_rubric(self, rid: str) -> bool:
        async with self.Session() as s:
            r = (await s.execute(select(RubricRow).where(RubricRow.id == rid))).scalar_one_or_none()
            if r is None:
                return False
            await s.delete(r)
            await s.commit()
            return True

    # ---- O17：数据集快照 ----
    async def save_dataset_snapshot(self, name: str, source_path: str, content: str,
                                    hashv: str) -> str:
        sid = str(uuid.uuid4())
        async with self.Session() as s:
            s.add(DatasetSnapshot(id=sid, name=name, source_path=source_path,
                                  hash=hashv, content=content))
            await s.commit()
        return sid

    async def list_snapshots(self) -> list[dict]:
        async with self.Session() as s:
            rows = (await s.execute(select(DatasetSnapshot).order_by(DatasetSnapshot.created_at.desc()))).scalars().all()
            return [{"id": r.id, "name": r.name, "source_path": r.source_path, "hash": r.hash,
                     "created_at": r.created_at} for r in rows]

    async def get_snapshot(self, sid: str) -> Optional[dict]:
        async with self.Session() as s:
            r = (await s.execute(select(DatasetSnapshot).where(DatasetSnapshot.id == sid))).scalar_one_or_none()
            if r is None:
                return None
            return {"id": r.id, "name": r.name, "source_path": r.source_path, "hash": r.hash,
                    "content": r.content, "created_at": r.created_at}

    # ---- O17：自定义视图（保存的 SQL 查询） ----
    async def save_view(self, name: str, sql: str, vid: Optional[str] = None) -> str:
        vid = vid or str(uuid.uuid4())
        async with self.Session() as s:
            existing = (await s.execute(select(SavedView).where(SavedView.id == vid))).scalar_one_or_none()
            if existing:
                existing.name = name
                existing.sql = sql
            else:
                s.add(SavedView(id=vid, name=name, sql=sql))
            await s.commit()
        return vid

    async def list_views(self) -> list[dict]:
        async with self.Session() as s:
            rows = (await s.execute(select(SavedView).order_by(SavedView.created_at.desc()))).scalars().all()
            return [{"id": r.id, "name": r.name, "sql": r.sql, "created_at": r.created_at} for r in rows]

    async def get_view(self, vid: str) -> Optional[dict]:
        async with self.Session() as s:
            r = (await s.execute(select(SavedView).where(SavedView.id == vid))).scalar_one_or_none()
            if r is None:
                return None
            return {"id": r.id, "name": r.name, "sql": r.sql, "created_at": r.created_at}

    async def delete_view(self, vid: str) -> bool:
        async with self.Session() as s:
            r = (await s.execute(select(SavedView).where(SavedView.id == vid))).scalar_one_or_none()
            if r is None:
                return False
            await s.delete(r)
            await s.commit()
            return True

    async def attach_to_run(self, run_id: str, attachments: list) -> bool:
        async with self.Session() as s:
            r = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
            if r is None:
                return False
            r.attachments_json = attachments
            await s.commit()
        return True

    # ---- O2：合规审计日志 ----
    async def record_audit(self, event_type: str, principal: str, detail: str = "",
                           ip: str = "", ok: bool = True, meta: Optional[dict] = None) -> int:
        async with self.Session() as s:
            row = AuditLog(event_type=event_type, principal=principal, detail=detail,
                           ip=ip, ok=ok, meta=meta or {})
            s.add(row)
            await s.flush()
            aid = row.id
            await s.commit()
        return aid

    async def list_audit(self, event_type: Optional[str] = None, limit: int = 200) -> list:
        async with self.Session() as s:
            if event_type:
                rows = (await s.execute(
                    select(AuditLog).where(AuditLog.event_type == event_type)
                    .order_by(AuditLog.ts.desc()).limit(limit)
                )).scalars().all()
            else:
                rows = (await s.execute(
                    select(AuditLog).order_by(AuditLog.ts.desc()).limit(limit)
                )).scalars().all()
            return [{"id": r.id, "event_type": r.event_type, "principal": r.principal,
                     "detail": r.detail, "ip": r.ip, "ok": r.ok, "ts": r.ts,
                     "meta": r.meta} for r in rows]

    # ---- O4：版本化资产（prompt / function） ----
    async def save_asset(self, kind: str, name: str, version: str, content: str,
                         content_hash: str = "", meta: Optional[dict] = None) -> int:
        async with self.Session() as s:
            row = AssetVersion(kind=kind, name=name, version=version,
                               content_hash=content_hash or _sha16(content),
                               content=content, meta_json=meta or {})
            s.add(row)
            await s.flush()
            aid = row.id
            await s.commit()
        return aid

    async def get_asset(self, name: str, version: Optional[str] = None) -> Optional[dict]:
        async with self.Session() as s:
            q = select(AssetVersion).where(AssetVersion.name == name)
            if version:
                q = q.where(AssetVersion.version == version)
            else:
                q = q.order_by(AssetVersion.created_at.desc())
            row = (await s.execute(q)).scalars().first()
            if row is None:
                return None
            return _asset_to_dict(row)

    async def list_assets(self) -> list:
        async with self.Session() as s:
            rows = (await s.execute(
                select(AssetVersion).order_by(AssetVersion.created_at.desc())
            )).scalars().all()
            return [_asset_to_dict(r) for r in rows]

    async def asset_versions(self, name: str) -> list:
        async with self.Session() as s:
            rows = (await s.execute(
                select(AssetVersion).where(AssetVersion.name == name)
                .order_by(AssetVersion.created_at.desc())
            )).scalars().all()
            return [{"version": r.version, "content_hash": r.content_hash,
                     "created_at": r.created_at} for r in rows]

    async def delete_asset(self, name: str, version: Optional[str] = None) -> int:
        async with self.Session() as s:
            q = select(AssetVersion).where(AssetVersion.name == name)
            if version:
                q = q.where(AssetVersion.version == version)
            rows = (await s.execute(q)).scalars().all()
            n = len(rows)
            for r in rows:
                await s.delete(r)
            await s.commit()
        return n


def _sha16(content: str) -> str:
    import hashlib
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _asset_to_dict(r: AssetVersion) -> dict:
    return {
        "id": r.id, "kind": r.kind, "name": r.name, "version": r.version,
        "content_hash": r.content_hash, "content": r.content,
        "created_at": r.created_at, "meta": r.meta_json or {},
    }


class RubricRow(Base):
    """项目级评分标准（N2）：name + criteria_json（带权重的准则列表）。"""
    __tablename__ = "rubrics"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    criteria_json = Column(JSON)
    pass_threshold = Column(Float, default=0.6)
    created_at = Column(Float, default=time.time)


class DatasetSnapshot(Base):
    """数据集快照（O17）：评测时数据集内容的版本化留存，支持回滚复评。"""
    __tablename__ = "dataset_snapshots"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    source_path = Column(Text)
    hash = Column(String(64))
    content = Column(Text)            # 数据集原文（jsonl），回滚时写回
    created_at = Column(Float, default=time.time)


class SavedView(Base):
    """自定义 Trace/数据视图（O17）：保存的 SQL 查询（复用 N9 四道闸）。"""
    __tablename__ = "saved_views"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    sql = Column(Text, nullable=False)
    created_at = Column(Float, default=time.time)



class AuditLog(Base):
    """合规审计日志（O2 等保留痕）：登录/换组/评测触发等可抽查事件。"""
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(64), nullable=False)
    principal = Column(String(128))
    detail = Column(Text)
    ip = Column(String(64))
    ok = Column(Boolean, default=True)
    ts = Column(Float, default=time.time)
    meta = Column(JSON)


class AssetVersion(Base):
    """版本化资产（O4）：prompt / function 为一级对象，按 name+version 管理。

    - kind: prompt | function（function 资产可注册进 ToolRegistry 托管执行）
    - content_hash: 内容 SHA-256 前 16 位，用于幂等判定与去重
    - content: 资产正文（prompt 文本 / function 源码表达式）
    """
    __tablename__ = "asset_versions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String(16), nullable=False)         # prompt | function
    name = Column(String(255), nullable=False)
    version = Column(String(64), nullable=False)       # 语义化版本或内容哈希
    content_hash = Column(String(64))
    content = Column(Text, nullable=False)
    created_at = Column(Float, default=time.time)
    meta_json = Column(JSON)


def _run_to_dict(r: Run) -> dict:
    return {
        "id": r.id, "name": r.name, "provider": r.provider, "judge_provider": r.judge_provider,
        "grader": r.grader, "trials": r.trials, "trial_policy": r.trial_policy,
        "external_id": r.external_id,
        "combine": r.combine, "concurrency": r.concurrency, "rate": r.rate,
        "created_at": r.created_at, "total": r.total, "passed": r.passed,
        "inconclusive": r.inconclusive, "pass_rate": r.pass_rate,
        "avg_pass_at_k": r.avg_pass_at_k, "config_json": r.config_json,
        "harness_version": r.harness_version, "model_version": r.model_version,
        "dataset_version": r.dataset_version, "three_way_json": r.three_way_json,
        "git_available": r.git_available, "git_commit": r.git_commit, "git_branch": r.git_branch,
        "git_tag": r.git_tag, "git_dirty": r.git_dirty, "git_author_name": r.git_author_name,
        "git_author_email": r.git_author_email,         "git_commit_message": r.git_commit_message,
        "git_commit_time": r.git_commit_time,
        "params": (r.config_json or {}).get("params"),
        "owner": r.owner,
        "visibility": r.visibility or "all",
        "token_budget_usd": r.token_budget_usd,
        "cost_used_usd": r.cost_used_usd,
        "budget_aborted": r.budget_aborted,
        "attachments": r.attachments_json or [],
        # O14：回链 meta（含 wb_meta：contract_id / callback_url），供 Web 控制台双向回查。
        # Run 表无独立 meta 列，wb_meta 随运行配置落库于 config_json，此处提取回显。
        "meta": {"wb_meta": (r.config_json or {}).get("wb_meta")},
    }
