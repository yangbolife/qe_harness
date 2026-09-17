"""eval_harness · lm-evaluation-harness 结果导入（N11 · 外部评测框架 loader）

lm-eval 用 ``--log_samples`` 会产出 ``samples_<task>_<model>_<time>.jsonl``，
每行一条样本，含 prompt / filtered_resps / returns(标准) / metrics(各指标分数)。

本模块把该文件转成 harness 的 Run 落库，使其能与自有评测在同一看板/SQL 层横向对比：
- 取第一个数值指标（优先 acc/exact_match/acc_norm）作为 score
- score >= threshold 判通过
- 缺失 metrics 的样本记 inconclusive，不计入通过

CLI：``--ingest-lmeval samples_xxx.jsonl --lmeval-name myrun --lmeval-threshold 0.5``
"""
from __future__ import annotations

import json
import os
from typing import Optional

from .models import Case, CaseResult, GraderResult, Trial
from .persistence import Database


def _pick_metric(sample: dict) -> tuple[str, float]:
    metrics = sample.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}
    for name in ("acc", "exact_match", "acc_norm", "exact_match_norm", "bleu", "rougeL"):
        if name in metrics:
            try:
                return name, float(metrics[name])
            except (TypeError, ValueError):
                continue
    if metrics:
        k = next(iter(metrics))
        try:
            return k, float(metrics[k])
        except (TypeError, ValueError):
            return k, 0.0
    return "acc", 0.0


def parse_lm_eval_samples(path: str, metric_threshold: float = 0.5,
                          suite: Optional[str] = None) -> list[CaseResult]:
    results: list[CaseResult] = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            s = json.loads(ln)
            doc_id = str(s.get("doc_id", s.get("doc_id_", len(results))))
            prompt = s.get("prompt", "") or ""
            resps = s.get("filtered_resps") or s.get("completions") or [""]
            resp = resps[0] if isinstance(resps, list) else str(resps)
            returns = s.get("returns", s.get("gold", ""))
            metric_name, score = _pick_metric(s)
            passed = score >= metric_threshold
            gr = GraderResult(grader=f"lm_eval:{metric_name}", score=score,
                              passed=passed, detail=metric_name)
            case = Case(
                id=doc_id, input=prompt, gold=str(returns),
                suite=suite or "lm_eval", category=suite or "lm_eval", grader="lm_eval",
            )
            trial = Trial(response=resp, graders=[gr], passed=passed)
            results.append(CaseResult(
                case=case, trials=[trial], response=resp, graders=[gr],
                passed=passed, k=1, pass_rate=score, pass_at_k=score,
                pass_k=passed, inconclusive=False, duration_ms=0,
            ))
    return results


async def ingest_lm_eval(db: Database, path: str, name: str,
                         metric_threshold: float = 0.5,
                         suite: Optional[str] = None) -> str:
    """把 lm-eval samples 文件导入数据库，返回 run_id。"""
    results = parse_lm_eval_samples(path, metric_threshold, suite)
    meta = {
        "provider": "lm-eval", "grader": "lm_eval",
        "model_version": name, "source_file": os.path.basename(path),
        "harness_version": "lm-eval-import",
    }
    return await db.save_run(name, meta, results, three_way=None)
