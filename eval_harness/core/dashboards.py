"""eval_harness · 自定义看板 widget 计算（N7）

看板 = 一组 widget，每个 widget 描述一类可视化。后端把 widget 计算成前端可直接渲染的数据：
- kpi   : 单一指标卡（avg_pass_rate / run_count / case_pass_rate / case_count），可带过滤
- chart : 用户自定义 SQL（仅可引用预置视图）返回 label,value 两列 → 柱状/折线/环形
- sql   : 用户自定义 SQL 表格（复用只读 SQL 层四道闸）

过滤列白名单，值做单引号转义，且最终仍过 run_query 的只读闸，安全。
"""
from __future__ import annotations

from .sql import run_query

_RUN_FILTER_COLS = {"provider", "model_version", "judge_provider", "grader"}
_CASE_FILTER_COLS = {"suite", "category", "grader"}


def _sql_str(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def _where(filt: dict, cols: set) -> str:
    if not filt:
        return ""
    parts = [f"{k} = {_sql_str(v)}" for k, v in filt.items() if k in cols and v not in (None, "")]
    return (" WHERE " + " AND ".join(parts)) if parts else ""


async def compute_widget(widget: dict, db) -> dict:
    t = widget.get("type")
    if t == "kpi":
        metric = widget.get("metric", "avg_pass_rate")
        filt = widget.get("filter") or {}
        if metric in ("avg_pass_rate", "run_count"):
            base, cols, sel = "runs", _RUN_FILTER_COLS, (
                "AVG(pass_rate)*100" if metric == "avg_pass_rate" else "COUNT(*)")
        else:
            base, cols, sel = "cases", _CASE_FILTER_COLS, (
                "AVG(CASE WHEN passed THEN 1 ELSE 0 END)*100" if metric == "case_pass_rate"
                else "COUNT(*)")
        sql = f"SELECT {sel} AS v FROM {base}{_where(filt, cols)}"
        res = await run_query(db, sql, limit=1)
        val = res["rows"][0]["v"] if res["rows"] else 0
        suffix = "%" if "pass_rate" in metric else ""
        return {"type": "kpi", "title": widget.get("title", metric),
                "value": round(float(val), 2), "suffix": suffix}

    if t in ("chart", "barchart", "linechart", "doughnut"):
        sql = widget.get("sql", "")
        res = await run_query(db, sql, limit=widget.get("limit", 200))
        labels, values = [], []
        for r in res["rows"]:
            keys = list(r.keys())
            labels.append(str(r.get("label", r.get(keys[0] if keys else "", ""))))
            values.append(float(r.get("value", 0) or 0))
        return {"type": widget.get("chart_type", "bar"), "title": widget.get("title", "chart"),
                "labels": labels, "values": values}

    if t == "sql":
        res = await run_query(db, widget.get("sql", ""), limit=widget.get("limit", 200))
        return {"type": "sql", "title": widget.get("title", "sql"),
                "columns": res["columns"], "rows": res["rows"]}

    return {"type": "unknown", "title": widget.get("title", ""), "error": "未知 widget 类型"}


async def compute_dashboard(config: dict, db) -> list[dict]:
    """把整个看板 config 的 widgets 逐一计算。"""
    widgets = config.get("widgets", []) if isinstance(config, dict) else []
    out = []
    for w in widgets:
        try:
            out.append(await compute_widget(w, db))
        except Exception as e:  # noqa: BLE001
            out.append({"type": w.get("type", "error"), "title": w.get("title", ""),
                        "error": str(e)})
    return out
