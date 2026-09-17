"""eval_harness · 数据出库（N10 · 日志检索与数据出库）

把评测结果导出为可分析/可归档的格式：
- fmt      : jsonl / csv / parquet（parquet 需 pyarrow，缺失自动回退 jsonl 并告警）
- partition: date / provider / model / run / none（Hive 风格分区目录）
- s3_url   : 形如 s3://bucket/prefix/ ，给定则上传（需 boto3，否则明确报错）

落地路径示例（partition=date）：
    out_dir/date=2026-09-17/part-000.jsonl
    out_dir/date=2026-09-17/_manifest.json

Web 端通过 ``/api/export`` 触发并下载 zip。
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
import zipfile
from typing import Optional

from .persistence import Database


def _partition_key(run: dict, partition: str) -> str:
    if partition == "date":
        return "date=" + time.strftime("%Y-%m-%d", time.localtime(run.get("created_at") or time.time()))
    if partition == "provider":
        return "provider=" + (run.get("provider") or "unknown")
    if partition == "model":
        return "model=" + (run.get("model_version") or "unknown")
    if partition == "run":
        return "run=" + (run.get("id") or "unknown")
    return "all"


def _run_rows(run: dict) -> list[dict]:
    rows = []
    for c in run.get("cases", []):
        rows.append({
            "run_id": run.get("id"), "run_name": run.get("name"),
            "provider": run.get("provider"), "model_version": run.get("model_version"),
            "case_id": c.get("case_id"), "suite": c.get("suite"),
            "category": c.get("category"), "passed": c.get("passed"),
            "inconclusive": c.get("inconclusive"), "response": c.get("response"),
            "gold": c.get("gold"), "input": c.get("input"),
        })
    return rows


def _write_part(path: str, rows: list[dict], fmt: str) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if fmt == "csv":
        if not rows:
            open(path, "w", encoding="utf-8").close()
            return 0
        cols = list(rows[0].keys())
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        return len(rows)
    # jsonl（parquet 缺失时也走这里）
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


async def export(db: Database, out_dir: str, fmt: str = "jsonl",
                partition: str = "date", s3_url: Optional[str] = None,
                limit: int = 500) -> dict:
    if fmt not in ("jsonl", "csv", "parquet"):
        raise ValueError(f"不支持的导出格式：{fmt}")
    runs = await db.list_runs(limit=limit)
    written = 0
    manifest = {"fmt": fmt, "partition": partition, "parts": [], "parquet_fallback": False}
    ext = fmt

    if fmt == "parquet":
        try:
            import pyarrow.json  # noqa: F401
            import pyarrow.parquet as pq  # type: ignore
        except Exception:
            fmt = "jsonl"
            ext = "jsonl"
            manifest["parquet_fallback"] = True

    for run in runs:
        full = await db.get_run(run["id"])
        rows = _run_rows(full)
        key = _partition_key(run, partition)
        part_dir = os.path.join(out_dir, key)
        os.makedirs(part_dir, exist_ok=True)
        if fmt == "parquet":
            import pyarrow as pa  # type: ignore
            import pyarrow.json as pj  # type: ignore
            tbl = pj.read_json(io.BytesIO(("\n".join(
                json.dumps(r, ensure_ascii=False) for r in rows) or "{}").encode("utf-8")))
            pq.write_table(tbl, os.path.join(part_dir, "part-000.parquet"))
        else:
            written += _write_part(os.path.join(part_dir, f"part-000.{ext}"), rows, fmt)
        manifest["parts"].append({"partition": key, "rows": len(rows)})

    with open(os.path.join(out_dir, "_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    if s3_url:
        _upload_s3(out_dir, s3_url)

    return {
        "out_dir": out_dir, "fmt": fmt, "partition": partition,
        "runs": len(runs), "rows": written,
        "parquet_fallback": manifest["parquet_fallback"],
        "s3": s3_url or None,
    }


def _upload_s3(out_dir: str, s3_url: str):
    try:
        import boto3  # type: ignore
    except Exception:
        raise RuntimeError("导出到 S3 需要 boto3（pip install boto3）")
    from urllib.parse import urlparse
    parsed = urlparse(s3_url)
    bucket = parsed.netloc
    prefix = parsed.path.lstrip("/")
    client = boto3.client("s3")
    for root, _, files in os.walk(out_dir):
        for fn in files:
            local = os.path.join(root, fn)
            rel = os.path.relpath(local, out_dir)
            client.upload_file(local, bucket, f"{prefix}/{rel}" if prefix else rel)


def zip_dir(src_dir: str, zip_path: str) -> str:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(src_dir):
            for fn in files:
                local = os.path.join(root, fn)
                z.write(local, os.path.relpath(local, src_dir))
    return zip_path
