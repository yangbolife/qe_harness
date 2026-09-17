"""N1 预置评测器库（autoevals 平替）测试。

覆盖：代码确定性评测器（确定性断言）+ LLM-judge 评测器（经 MockProvider 跑通 +
未配置 judge_provider 的降级路径）+ Registry 注册完整性 + 目录列举。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval_harness.core import scorers  # noqa: F401  触发注册
from eval_harness.core.graders import GRADERS
from eval_harness.core.models import Case
from eval_harness.core.providers import MockProvider


def _case(pred_gold=None, gold="", meta=None, input="q"):
    return Case(id="1", input=input, gold=gold, meta=meta or {})


# ---------------- 代码/规则评测器 ----------------
def test_valid_json():
    g = scorers.ValidJSONScorer()
    assert g.judge('{"a":1}', _case()).passed is True
    assert g.judge("不是 JSON", _case()).passed is False
    # schema 校验
    c = _case(meta={"json_schema": {"required": ["a"], "properties": {"a": {"type": "number"}}}})
    assert g.judge('{"a":1}', c).passed is True
    assert g.judge('{"a":"x"}', c).passed is False
    assert g.judge('{"b":2}', c).passed is False


def test_json_diff():
    g = scorers.JSONDiffScorer()
    assert g.judge('{"a":1,"b":2}', _case(gold='{"a":1,"b":2}')).score == 1.0
    r = g.judge('{"a":1}', _case(gold='{"a":1,"b":2}'))
    assert 0.0 < r.score < 1.0


def test_exact_match():
    g = scorers.ExactMatchScorer()
    assert g.judge("Hello World", _case(gold="hello world")).passed is True  # 归一化去大小写
    assert g.judge("Hello", _case(gold="World")).passed is False


def test_contains():
    g = scorers.ContainsScorer()
    assert g.judge("答案是 30 万公里", _case(gold="30万")).passed is True
    assert g.judge("无关内容", _case(gold="30万")).passed is False


def test_levenshtein():
    g = scorers.LevenshteinScorer()
    assert g.judge("abcd", _case(gold="abcd")).score == 1.0
    assert g.judge("完全不一样xyz", _case(gold="短")).score < 0.5


def test_numeric_diff():
    g = scorers.NumericDiffScorer()
    assert g.judge("结果 100", _case(gold="100")).score == 1.0
    assert g.judge("结果 200", _case(gold="100")).score < 1.0


def test_list_contains():
    g = scorers.ListContainsScorer()
    r = g.judge("苹果, 香蕉", _case(gold="香蕉, 苹果"))
    assert r.score == 1.0
    r2 = g.judge("苹果", _case(gold="香蕉, 橘子"))
    assert r2.score < 1.0


def test_embedding_similarity():
    emb = lambda s: [float(len(s)), float(s.count("a")), float(s.count("e"))]
    g = scorers.EmbeddingSimilarityScorer(embedder=emb)
    assert g.judge("same text", _case(gold="same text")).score == 1.0
    assert g.judge("aaa", _case(gold="eee")).score < 1.0
    # 未配置 embedder → 降级提示
    none_g = scorers.EmbeddingSimilarityScorer()
    assert "NEEDS_EMBEDDER" in none_g.judge("x", _case()).detail


def test_moderation():
    g = scorers.ModerationScorer()
    assert g.judge("这是正常内容", _case()).passed is True
    assert g.judge("包含违规暴力内容", _case()).passed is False
    # 自定义词表
    assert g.judge("绝密泄露", _case(meta={"moderation_terms": ["绝密"]})).passed is False


# ---------------- LLM-judge 评测器 ----------------
def test_factuality_with_mock_judge():
    jp = MockProvider(mode="judge")
    g = scorers.FactualityScorer(judge_provider=jp)
    r = g.judge("巴黎是法国首都。", _case(gold="巴黎是法国首都。"))
    assert 0.0 <= r.score <= 1.0
    assert isinstance(r.passed, bool)
    assert r.tokens > 0  # 创新⑤ 成本追踪生效


def test_factuality_no_judge_provider():
    g = scorers.FactualityScorer()
    r = g.judge("x", _case())
    assert r.passed is False
    assert "judge_provider 未配置" in r.detail


def test_faithfulness_uses_context():
    jp = MockProvider(mode="judge")
    g = scorers.FaithfulnessScorer(judge_provider=jp)
    r = g.judge("据上下文，答案是 A。", _case(meta={"context": "权威资料表明答案为 A。"}))
    assert 0.0 <= r.score <= 1.0


def test_ragas_context_recall_registered():
    jp = MockProvider(mode="judge")
    g = scorers.ContextRecallScorer(judge_provider=jp)
    r = g.judge("回答", _case(meta={"context": "标准答案所需信息在此。"}))
    assert 0.0 <= r.score <= 1.0


# ---------------- 注册完整性 ----------------
def test_all_autoevals_registered():
    for name in scorers.AUTOEVALS:
        assert name in GRADERS.available(), f"{name} 未注册"


def test_catalog_count():
    assert len(scorers.list_autoevals()) == 18


def test_engine_build_grader_injects_judge():
    # 验证 engine._build_grader 能为 LLM-judge 评测器注入 judge_provider（不抛 TypeError）
    from eval_harness.core.engine import _build_grader
    jp = MockProvider(mode="judge")
    g = _build_grader("factuality", judge_provider=jp)
    assert isinstance(g, scorers.FactualityScorer)
    assert g.judge_provider is jp
    # 旧评测器零参回退不受影响
    cg = _build_grader("code")
    assert cg is not None
