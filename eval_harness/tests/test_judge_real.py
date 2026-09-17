"""eval_harness · 真实 LLM-as-Judge 接入校验（需联网 + DeepSeek key）

默认跳过；设 EVAL_REAL=1 且 DEEPSEEK_API_KEY 后运行，验证 judge 评分器确实调用
强模型按 rubric 打分（非 stub）。
"""
import json
import os
import tempfile

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("EVAL_REAL"),
    reason="需 EVAL_REAL=1 且 DEEPSEEK_API_KEY 才跑真实 judge",
)

from eval_harness.core.engine import run_suite


def _make_suite():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "judge_smoke.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "id": "j1",
            "input": "中国的首都是哪里？",
            "gold": "北京",
            "category": "geo",
            "grader": "judge",
            "meta": {"rubric": "回答是否准确给出中国首都（北京）"},
        }, ensure_ascii=False) + "\n")
    return p


def test_judge_real_calls_strong_model():
    p = _make_suite()
    results = run_suite(p, provider_name="deepseek", judge_provider_name="deepseek")
    assert results, "空结果"
    g = results[0].graders[0]
    assert g.grader == "judge"
    assert g.score >= 0.6, f"judge 分数过低: {g.score} | {g.detail}"
    print(f"\n[真实 judge] score={g.score:.2f} | {g.detail}")
