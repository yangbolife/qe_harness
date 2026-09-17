"""eval_harness · 只读 SQL 查询层（N9）

为什么需要：agent 评测的数据（运行 / 逐题 / 评分器 / 轨迹）要能被**按任意口径**分析——
甲方要自定义统计口径、交付要出报表、审计要查操作。此前只有固定 REST 端点，
没有查询语言，任何新口径都要改代码。

对齐 Braintrust：SQL over logs/experiments/datasets，沙箱保存查询 + CLI + API 三入口。

安全模型（四道闸，缺一不可）：
1. **语义闸**：剥离字符串字面量与注释后扫描。只允许单条 ``SELECT`` / ``WITH``；
   命中写操作 / DDL / 多语句分隔符即拒绝。
2. **结构闸**：用户只能看到 5 个预置视图（runs / cases / graders / trials / traces），
   以 CTE 前导方式注入，不直接持表；视图只暴露必要列（含 ``*_int`` 便于求和）。
3. **容量闸**：任何查询都被外层 ``LIMIT`` 封顶；长文本按 ``preview_length`` 截断。
4. **时间闸**：``asyncio.wait_for`` 超时中断；PostgreSQL 上额外尝试只读事务。

双后端：视图的时间戳格式化由方言决定，其余语法保持在 PG / SQLite 的共同子集内
（不用 JOIN 之外的窗口函数、不用 PIVOT）。
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import date, datetime
from typing import Optional

from sqlalchemy import text

VIEW_NAMES = ("runs", "cases", "graders", "trials", "traces")

DEFAULT_LIMIT = 200
DEFAULT_TIMEOUT = 15.0
MAX_LIMIT = 5000


class SQLGuardError(ValueError):
    """查询被只读守卫拒绝（语义闸 / 参数非法）。"""


# --------------------------------------------------------------------------- #
# 视图定义（方言相关的只有时间戳格式化）
# --------------------------------------------------------------------------- #
def _ts(col: str, dialect: str) -> str:
    if dialect == "sqlite":
        return f"datetime({col},'unixepoch')"
    return f"to_timestamp({col})::text"


def _views(dialect: str) -> dict:
    """返回 {视图名: SELECT 语句}。列名稳定，供沙箱自动补全与文档引用。"""
    ts = lambda c: _ts(c, dialect)  # noqa: E731
    return {
        # 一次评测运行（Outcome 聚合）
        "runs": f"""
            SELECT r.id            AS run_id,
                   r.name          AS run_name,
                   r.provider      AS provider,
                   r.judge_provider AS judge_provider,
                   r.grader        AS grader,
                   r.trials        AS trials,
                   r.trial_policy  AS trial_policy,
                   r.combine       AS combine,
                   r.harness_version AS harness_version,
                   r.model_version   AS model_version,
                   r.dataset_version AS dataset_version,
                   r.git_available  AS git_available,
                   r.git_commit     AS git_commit,
                   r.git_branch     AS git_branch,
                   r.git_tag        AS git_tag,
                   r.git_dirty      AS git_dirty,
                   r.git_author_name AS git_author_name,
                   r.git_author_email AS git_author_email,
                   r.git_commit_message AS git_commit_message,
                   r.git_commit_time AS git_commit_time,
                   r.total         AS total,
                   r.passed        AS passed,
                   r.inconclusive  AS inconclusive,
                   r.pass_rate     AS pass_rate,
                   r.avg_pass_at_k AS avg_pass_at_k,
                   {ts('r.created_at')} AS created_at
            FROM runs r
        """,
        # 逐题结果（含输入/期望/回答，便于按业务类别下钻）
        "cases": f"""
            SELECT c.run_id        AS run_id,
                   r.name          AS run_name,
                   c.case_id       AS case_id,
                   c.suite         AS suite,
                   c.category      AS category,
                   c.difficulty    AS difficulty,
                   c.grader        AS grader,
                   c.passed        AS passed,
                   CASE WHEN c.passed THEN 1 ELSE 0 END AS passed_int,
                   c.inconclusive  AS inconclusive,
                   c.k             AS k,
                   c.pass_rate     AS pass_rate,
                   c.pass_at_k     AS pass_at_k,
                   c.duration_ms   AS duration_ms,
                   c.error         AS error,
                   c.input         AS input,
                   c.gold          AS gold,
                   c.response      AS response
            FROM cases c
            LEFT JOIN runs r ON r.id = c.run_id
        """,
        # 逐评分器（含 judge 成本，创新⑤）
        "graders": """
            SELECT c.run_id     AS run_id,
                   c.case_id    AS case_id,
                   c.suite      AS suite,
                   c.category   AS category,
                   g.grader     AS grader,
                   g.score      AS score,
                   g.passed     AS passed,
                   CASE WHEN g.passed THEN 1 ELSE 0 END AS passed_int,
                   g.tokens     AS tokens,
                   g.cost_usd   AS cost_usd,
                   g.duration_ms AS duration_ms,
                   g.detail     AS detail
            FROM graders g
            JOIN cases c ON c.id = g.case_row_id
        """,
        # 逐次执行（k 次 Trial，一致性分析用）
        "trials": """
            SELECT c.run_id   AS run_id,
                   c.case_id  AS case_id,
                   t.idx      AS idx,
                   t.passed   AS passed,
                   CASE WHEN t.passed THEN 1 ELSE 0 END AS passed_int,
                   t.error    AS error,
                   t.duration_ms AS duration_ms,
                   t.response AS response
            FROM trials t
            JOIN cases c ON c.id = t.case_row_id
        """,
        # 生产 trace 回灌（创新⑥）
        "traces": f"""
            SELECT t.id       AS trace_id,
                   t.run_id   AS run_id,
                   t.source   AS source,
                   {ts('t.created_at')} AS created_at
            FROM traces t
        """,
    }


# --------------------------------------------------------------------------- #
# 语义闸
# --------------------------------------------------------------------------- #
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|grant|revoke"
    r"|truncate|merge|call|copy|comment|reindex|analyze|into|set|lock)\b"
    r"|replace\s+into",
    re.I,
)


def _strip_noise(sql: str) -> str:
    """剥离字符串字面量与注释，避免把字面量里的 'delete' 误判成写操作。"""
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    sql = re.sub(r'"(?:[^"]|"")*"', '""', sql)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return sql


def validate(sql: str) -> str:
    """校验并返回原 SQL（未改写）。不通过则抛 SQLGuardError。"""
    if not sql or not sql.strip():
        raise SQLGuardError("SQL 不能为空")
    raw = sql.strip()
    body = _strip_noise(raw).strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    if ";" in body:
        raise SQLGuardError("只允许单条语句（检测到多语句分隔符 ';'）")
    head = body.lstrip("( ").upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        raise SQLGuardError("只允许 SELECT / WITH 查询（本层只读）")
    m = _FORBIDDEN.search(body)
    if m:
        raise SQLGuardError(f"查询含被禁止的关键字：{m.group(0)}（本层只读，仅支持 SELECT）")
    return raw


def compose(sql: str, dialect: str = "postgresql", limit: int = DEFAULT_LIMIT) -> str:
    """把用户 SQL 与预置视图拼成一条可执行语句（视图以 CTE 前导注入）。

    用户 SQL 若自身以 WITH 开头，则把其 CTE 体并入同一 WITH 列表。

    视图 CTE 用 ``vw_<name>`` 别名注入，避免与同名基表（runs/cases/...）
    在 CTE 作用域内发生名称冲突；用户写的文档视图名（runs/cases/...）会被
    安全改写为 ``vw_<name>``（字符串字面量与注释内的词不会被误改）。
    """
    raw = validate(sql)
    prefixed = {f"vw_{name}": body for name, body in _views(dialect).items()}
    views_sql = ",\n".join(f"{name} AS (\n{body.strip()}\n)" for name, body in prefixed.items())
    body = _rewrite_view_refs(raw, VIEW_NAMES, "vw_").rstrip().rstrip(";").strip()
    if body.upper().startswith("WITH"):
        body = body[4:].lstrip()
        composed = f"WITH {views_sql},\n{body}"
    else:
        composed = f"WITH {views_sql}\n{body}"
    # 容量闸：外层无条件封顶，用户自带 LIMIT 也越不过
    return f"SELECT * FROM (\n{composed}\n) AS _capped LIMIT {int(limit)}"


def _rewrite_view_refs(sql: str, names, prefix: str) -> str:
    """把用户 SQL 里出现的文档视图名改写成 CTE 别名（``<prefix><name>``）。

    先抽离字符串字面量与注释，避免把字面量里的 'runs' 误改；再按整词替换；
    最后还原字面量。
    """
    literals: list[str] = []

    def _stash(m):
        literals.append(m.group(0))
        return f"\x00{len(literals) - 1}\x00"

    s = re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", _stash, sql)
    s = re.sub(r"--[^\n]*", _stash, s)
    s = re.sub(r"/\*.*?\*/", _stash, s, flags=re.S)
    for name in names:
        s = re.sub(rf"\b{name}\b", f"{prefix}{name}", s)
    s = re.sub(r"\x00(\d+)\x00", lambda m: literals[int(m.group(1))], s)
    return s


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
def _dialect_of(db) -> str:
    url = str(getattr(db, "url", "") or "")
    return "sqlite" if url.startswith("sqlite") else "postgresql"


def _cell(v, preview_length: int):
    """把单元格转成 JSON 可序列化值；长文本按 preview_length 截断。"""
    if v is None:
        return None
    if isinstance(v, bool) or isinstance(v, (int, float)):
        return v
    if isinstance(v, (bytes, bytearray)):
        v = v.decode("utf-8", "replace")
    if isinstance(v, (datetime, date)):
        v = v.isoformat()
    s = str(v)
    if preview_length and preview_length > 0 and len(s) > preview_length:
        return s[:preview_length] + "…"
    return s


def _clamp_limit(limit: Optional[int]) -> int:
    if not limit or limit <= 0:
        return DEFAULT_LIMIT
    return min(int(limit), MAX_LIMIT)


async def run_query(
    db,
    sql: str,
    *,
    limit: Optional[int] = DEFAULT_LIMIT,
    timeout: float = DEFAULT_TIMEOUT,
    preview_length: int = 1024,
) -> dict:
    """执行只读查询并返回结构化结果。

    返回：``{"columns", "rows", "row_count", "truncated", "elapsed_ms", "sql", "views"}``
    """
    from sqlalchemy import text as _text  # 局部导入，保持模块可独立被测试导入

    limit = _clamp_limit(limit)
    dialect = _dialect_of(db)
    composed = compose(sql, dialect=dialect, limit=limit)

    async with db.Session() as s:
        if dialect == "postgresql":
            try:  # 双保险：即便语义闸被绕过，写操作也会被 PG 拒绝
                await s.execute(_text("SET TRANSACTION READ ONLY"))
            except Exception:
                pass

        async def _exec():
            return await s.execute(_text(composed))

        t0 = time.time()
        try:
            res = await asyncio.wait_for(_exec(), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise SQLGuardError(f"查询超时（>{timeout:g}s）；请加上时间范围或聚合条件") from e
        columns = list(res.keys())
        raw_rows = res.fetchall()
        elapsed = (time.time() - t0) * 1000

    rows = [
        {col: _cell(row[i], preview_length) for i, col in enumerate(columns)}
        for row in raw_rows
    ]
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": len(rows) >= limit,
        "elapsed_ms": round(elapsed, 2),
        "sql": sql,
        "views": list(VIEW_NAMES),
        "limit": limit,
    }


async def describe_views(db) -> list:
    """列出预置视图及其列（供 Web 沙箱自动补全与 CLI ``--list-views``）。"""
    from sqlalchemy import text as _text

    dialect = _dialect_of(db)
    out = []
    async with db.Session() as s:
        for name, body in _views(dialect).items():
            try:
                res = await s.execute(_text(f"SELECT * FROM (\n{body.strip()}\n) AS v LIMIT 0"))
                cols = list(res.keys())
            except Exception:
                cols = []
            out.append({"name": name, "columns": cols})
    return out


# --------------------------------------------------------------------------- #
# 人类可读输出（CLI 用）
# --------------------------------------------------------------------------- #
def format_table(result: dict, max_width: int = 28) -> str:
    """把查询结果渲染成等宽表格（不引入第三方表格库）。"""
    cols = result.get("columns") or []
    if not cols:
        return "(无列)"
    cells = [
        ["" if r.get(c) is None else str(r.get(c)).replace("\n", "⏎") for c in cols]
        for r in result.get("rows", [])
    ]
    widths = []
    for i, c in enumerate(cols):
        w = len(str(c))
        for row in cells:
            w = max(w, len(row[i]))
        widths.append(min(w, max_width))

    def _cut(s: str, w: int) -> str:
        # 中文按 2 宽度估算，避免表格错位
        cur = 0
        out_chars = []
        for ch in s:
            cw = 2 if ord(ch) > 0x2E80 else 1
            if cur + cw > w:
                break
            out_chars.append(ch)
            cur += cw
        return "".join(out_chars)

    def _line(vals) -> str:
        parts = []
        for i, v in enumerate(vals):
            s = _cut(str(v), widths[i])
            pad = widths[i] - sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)
            parts.append(s + " " * max(0, pad))
        return " | ".join(parts)

    lines = [_line(cols), "-+-".join("-" * w for w in widths)]
    lines += [_line(r) for r in cells]
    return "\n".join(lines)


PREVIEW_QUERIES = [
    {
        "name": "各 provider 通过率",
        "sql": "SELECT provider, count(*) AS runs, round(avg(pass_rate), 4) AS avg_pass_rate "
               "FROM runs GROUP BY provider ORDER BY runs DESC",
    },
    {
        "name": "评分器成本与通过率",
        "sql": "SELECT grader, count(*) AS n, round(sum(cost_usd), 6) AS cost_usd, "
               "round(avg(score), 3) AS avg_score FROM graders GROUP BY grader ORDER BY cost_usd DESC",
    },
    {
        "name": "失败用例（含输入与回答）",
        "sql": "SELECT run_name, case_id, suite, category, input, gold, response "
               "FROM cases WHERE passed = false ORDER BY run_id DESC",
    },
    {
        "name": "按业务类别统计",
        "sql": "SELECT category, count(*) AS n, sum(passed_int) AS passed, "
               "round(avg(pass_at_k), 3) AS avg_pass_at_k FROM cases GROUP BY category ORDER BY n DESC",
    },
    {
        "name": "轨迹来源分布",
        "sql": "SELECT source, count(*) AS n FROM traces GROUP BY source ORDER BY n DESC",
    },
]
