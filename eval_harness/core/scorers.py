"""eval_harness · 预置评测器库（N1：autoevals 平替）

对齐 Braintrust autoevals 的「开箱即用评测器」能力，挂入现有三层评分器 / Registry：
- 代码确定性评测器（快·客观·可复现）：valid_json / json_diff / exact_match / contains /
  levenshtein / numeric_diff / list_contains / embedding_similarity / moderation
- LLM-as-Judge 评测器（需注入 judge_provider，复用创新⑤成本追踪）：
  factuality / faithfulness / context_precision / context_recall / context_relevancy /
  answer_relevancy / summary / closed_qa / translation

设计约束（与 engine._build_grader 对齐）：
- 所有评测器 `judge(pred:str, case:Case) -> GraderResult`，可被 `--grader a,b` 逗号串直接调用。
- 代码类零参构造即可；LLM-judge 类接受 `judge_provider=`（engine 会注入）。
- 不使用外部付费依赖：embedding 走可插拔 embedder（DeepSeek 无 embedding，故默认需配置）；
  moderation 走规则词表（离线可用），不依赖 OpenAI Moderation API。

注册：每个评测器 `@GRADERS.register("名")`；`AUTOEVALS` 目录供 CLI/Web 列举。
"""
from __future__ import annotations

import json
import math
import re
import time
from typing import Callable, List, Optional

from .graders import _parse_judge, _estimate_tokens, _estimate_cost, normalize
from .models import Case, GraderResult
from .registry import GRADERS


# ===================== 通用工具 =====================
def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _levenshtein_sim(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    return max(0.0, 1.0 - _levenshtein(a, b) / max(len(a), len(b), 1))


def _extract_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}|\[.*\]", text, re.S)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                return None
    return None


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (na * nb)))


def _json_similarity(a, b) -> float:
    """递归结构+内容相似度（JSONDiff 核心），返回 0~1。"""
    if type(a) is not type(b):
        # 标量类型不同：允许数值/字符串弱比较
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return _num_sim(a, b)
        if isinstance(a, str) and isinstance(b, str):
            return _levenshtein_sim(a, b)
        return 0.0
    if isinstance(a, dict):
        keys = set(a) | set(b)
        if not keys:
            return 1.0
        return sum(_json_similarity(a.get(k, "__MISSING__"), b.get(k, "__MISSING__")) for k in keys) / len(keys)
    if isinstance(a, list):
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        # 贪心最佳匹配（v1 近似，非匈牙利；对常规评测足够）
        used = [False] * len(b)
        total = 0.0
        for x in a:
            best, bi = -1.0, -1
            for j, y in enumerate(b):
                if not used[j]:
                    s = _json_similarity(x, y)
                    if s > best:
                        best, bi = s, j
            if bi >= 0:
                used[bi] = True
            total += max(0.0, best)
        return total / len(a)
    if isinstance(a, str):
        return _levenshtein_sim(a, b)
    if isinstance(a, (int, float)):
        return _num_sim(a, b)
    return 1.0 if a == b else 0.0


def _num_sim(a: float, b: float) -> float:
    if a == b:
        return 1.0
    denom = max(abs(a), abs(b), 1e-9)
    return max(0.0, 1.0 - abs(a - b) / denom)


def _numbers(text: str):
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text or "")]


# ===================== ① 代码/规则评测器 =====================
@GRADERS.register("valid_json")
class ValidJSONScorer:
    """ValidJSON：pred 是否为合法 JSON；若 case.meta['json_schema'] 提供则额外校验 schema。"""
    name = "valid_json"
    channel = "code"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 1.0):
        self.threshold = threshold

    def judge(self, pred: str, case: Case) -> GraderResult:
        t0 = time.time()
        obj = _extract_json(pred)
        if obj is None:
            return GraderResult(self.name, 0.0, False, detail="非合法 JSON",
                                duration_ms=(time.time() - t0) * 1000)
        schema = case.meta.get("json_schema")
        if schema:
            ok, msg = self._check_schema(obj, schema)
            if not ok:
                return GraderResult(self.name, 0.0, False, detail=f"schema 不符:{msg}",
                                    duration_ms=(time.time() - t0) * 1000)
        return GraderResult(self.name, 1.0, True, detail="合法 JSON" + ("+schema命中" if schema else ""),
                            duration_ms=(time.time() - t0) * 1000)

    @staticmethod
    def _check_schema(obj, schema) -> tuple[bool, str]:
        required = schema.get("required", [])
        missing = [k for k in required if k not in obj]
        if missing:
            return False, f"缺 {missing}"
        props = schema.get("properties", {})
        for k, spec in props.items():
            if k in obj and "type" in spec:
                v = obj[k]
                t = spec["type"]
                if t == "number" and not isinstance(v, (int, float)):
                    return False, f"{k} 非数字"
                if t == "string" and not isinstance(v, str):
                    return False, f"{k} 非字符串"
                if t == "boolean" and not isinstance(v, bool):
                    return False, f"{k} 非布尔"
                if t == "array" and not isinstance(v, list):
                    return False, f"{k} 非数组"
                if t == "object" and not isinstance(v, dict):
                    return False, f"{k} 非对象"
        return True, "ok"


@GRADERS.register("json_diff")
class JSONDiffScorer:
    """JSONDiff：pred 与 case.gold(JSON) 的递归结构+内容相似度，返回 0~1。"""
    name = "json_diff"
    channel = "code"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 0.6):
        self.threshold = threshold

    def judge(self, pred: str, case: Case) -> GraderResult:
        t0 = time.time()
        a = _extract_json(pred)
        b = _extract_json(case.gold) if case.gold else None
        if a is None or b is None:
            return GraderResult(self.name, 0.0, False, detail="JSON 解析失败",
                                duration_ms=(time.time() - t0) * 1000)
        sim = _json_similarity(a, b)
        return GraderResult(self.name, sim, sim >= self.threshold,
                            detail=f"相似度={sim:.2f}", duration_ms=(time.time() - t0) * 1000)


@GRADERS.register("exact_match")
class ExactMatchScorer:
    """ExactMatch：归一化后精确相等（复用 graders.normalize）。"""
    name = "exact_match"
    channel = "code"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 1.0):
        self.threshold = threshold

    def judge(self, pred: str, case: Case) -> GraderResult:
        t0 = time.time()
        ok = normalize(case.gold or "") == normalize(pred)
        return GraderResult(self.name, 1.0 if ok else 0.0, ok, detail=f"hit={ok}",
                            duration_ms=(time.time() - t0) * 1000)


@GRADERS.register("contains")
class ContainsScorer:
    """Contains：pred 是否包含 gold（或数值核心一致）。"""
    name = "contains"
    channel = "code"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 1.0):
        self.threshold = threshold

    def judge(self, pred: str, case: Case) -> GraderResult:
        t0 = time.time()
        g, p = normalize(case.gold or ""), normalize(pred)
        ok = (g in p) or (bool(_numbers(case.gold)) and set(_numbers(case.gold)) <= set(_numbers(pred)))
        return GraderResult(self.name, 1.0 if ok else 0.0, ok, detail=f"hit={ok}",
                            duration_ms=(time.time() - t0) * 1000)


@GRADERS.register("levenshtein")
class LevenshteinScorer:
    """Levenshtein：pred 与 gold 的字符串相似度（0~1）。"""
    name = "levenshtein"
    channel = "code"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 0.6):
        self.threshold = threshold

    def judge(self, pred: str, case: Case) -> GraderResult:
        t0 = time.time()
        sim = _levenshtein_sim(pred or "", case.gold or "")
        return GraderResult(self.name, sim, sim >= self.threshold, detail=f"sim={sim:.2f}",
                            duration_ms=(time.time() - t0) * 1000)


@GRADERS.register("numeric_diff")
class NumericDiffScorer:
    """NumericDiff：比较两个数值（取自文本首个数）的归一化差异，返回 0~1。"""
    name = "numeric_diff"
    channel = "code"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 0.8):
        self.threshold = threshold

    def judge(self, pred: str, case: Case) -> GraderResult:
        t0 = time.time()
        pa, ga = _numbers(pred), _numbers(case.gold)
        if not pa or not ga:
            return GraderResult(self.name, 0.0, False, detail="无可比数值",
                                duration_ms=(time.time() - t0) * 1000)
        sim = _num_sim(pa[0], ga[0])
        return GraderResult(self.name, sim, sim >= self.threshold, detail=f"num_sim={sim:.2f}",
                            duration_ms=(time.time() - t0) * 1000)


@GRADERS.register("list_contains")
class ListContainsScorer:
    """ListContains：两个列表（pred/gold，逗号或换行分隔）的语义重叠（贪心最佳匹配）。"""
    name = "list_contains"
    channel = "code"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 0.6):
        self.threshold = threshold

    def judge(self, pred: str, case: Case) -> GraderResult:
        t0 = time.time()
        def _split(s):
            return [x.strip() for x in re.split(r"[，,、\n]", s or "") if x.strip()]
        pa, ga = _split(pred), _split(case.gold)
        if not pa or not ga:
            return GraderResult(self.name, 0.0, False, detail="空列表",
                                duration_ms=(time.time() - t0) * 1000)
        used = [False] * len(ga)
        total = 0.0
        for x in pa:
            best, bi = -1.0, -1
            for j, y in enumerate(ga):
                if not used[j]:
                    s = _levenshtein_sim(x, y)
                    if s > best:
                        best, bi = s, j
            if bi >= 0:
                used[bi] = True
            total += max(0.0, best)
        sim = total / len(pa)
        return GraderResult(self.name, sim, sim >= self.threshold, detail=f"重叠={sim:.2f}",
                            duration_ms=(time.time() - t0) * 1000)


@GRADERS.register("embedding_similarity")
class EmbeddingSimilarityScorer:
    """EmbeddingSimilarity：pred 与 gold 的余弦相似度。embedder 可插拔：
    构造传入 embedder=Callable[[str],list[float]]，或 case.meta['embedder']。
    默认不依赖任何付费 embedding 服务（DeepSeek 无 embedding，故需显式配置）。"""
    name = "embedding_similarity"
    channel = "code"

    def __init__(self, judge_provider=None, human_labels=None, embedder: Optional[Callable[[str], List[float]]] = None,
                 threshold: float = 0.6):
        self.embedder = embedder
        self.threshold = threshold

    def judge(self, pred: str, case: Case) -> GraderResult:
        t0 = time.time()
        emb = self.embedder or case.meta.get("embedder")
        if emb is None:
            return GraderResult(self.name, 0.0, False, detail="NEEDS_EMBEDDER(配置 embedder)",
                                duration_ms=(time.time() - t0) * 1000)
        try:
            sim = _cosine(emb(pred or ""), emb(case.gold or ""))
        except Exception as e:
            return GraderResult(self.name, 0.0, False, detail=f"embedder 错误:{e}",
                                duration_ms=(time.time() - t0) * 1000)
        return GraderResult(self.name, sim, sim >= self.threshold, detail=f"cos={sim:.2f}",
                            duration_ms=(time.time() - t0) * 1000)


@GRADERS.register("moderation")
class ModerationScorer:
    """Moderation（规则版，离线可用）：基于内置+自定义词表检测违规内容。
    非 OpenAI Moderation API 依赖；命中任一违规词则返回 0 分并列出命中项。"""
    name = "moderation"
    channel = "code"
    _DEFAULT_TERMS = ["违禁", "色情", "暴力", "恐怖主义", "自残", "赌博", "毒品", "诈骗",
                     "illegal", "porn", "violence", "terrorism", "suicide", "drug"]

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 1.0):
        self.threshold = threshold

    def judge(self, pred: str, case: Case) -> GraderResult:
        t0 = time.time()
        terms = self._DEFAULT_TERMS + list(case.meta.get("moderation_terms", []))
        low = (pred or "").lower()
        hits = [t for t in terms if t.lower() in low]
        ok = not hits
        return GraderResult(self.name, 1.0 if ok else 0.0, ok,
                            detail=("clean" if ok else f"命中:{hits}"),
                            duration_ms=(time.time() - t0) * 1000)


# ===================== ② LLM-as-Judge 评测器（复用创新⑤成本追踪）=====================
class _JudgeScorer:
    """LLM-judge 基类：按 rubric 调 judge_provider 结构化打分（0~1）。"""
    channel = "judge"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 0.6):
        self.judge_provider = judge_provider
        self.threshold = threshold
        self.rubric = "按正确性(0-1)打分，只回数字。"

    def _extra(self, case: Case) -> str:
        return ""

    def _prompt(self, case: Case, pred: str, extra: str) -> str:
        return (
            "你是一名严格评测员。请根据 RUBRIC 对【被测回答】打分。\n"
            "只输出一行 JSON：{\"score\":0到1的小数,\"reason\":\"一句话理由\"}，不要任何其他内容。\n"
            f"RUBRIC: {self.rubric}\n"
            f"问题: {case.input}\n"
            f"标准答案: {case.gold or '(开放题，无标准答案)'}\n"
            f"{extra}"
            f"被测回答: {pred}\n"
        )

    def judge(self, pred: str, case: Case) -> GraderResult:
        t0 = time.time()
        if self.judge_provider is None:
            return GraderResult(self.name, 0.0, False,
                                detail="judge_provider 未配置(需 --judge-provider)",
                                duration_ms=(time.time() - t0) * 1000)
        extra = self._extra(case)
        prompt = self._prompt(case, pred, extra)
        raw = self.judge_provider.ask(prompt)
        tokens = _estimate_tokens(prompt, raw or "")
        cost = _estimate_cost(tokens)
        if raw == "__BUDGET_EXCEEDED__":
            return GraderResult(self.name, 0.0, False, detail="BUDGET_EXCEEDED",
                                duration_ms=(time.time() - t0) * 1000, tokens=tokens, cost_usd=cost)
        score, reason = _parse_judge(raw)
        return GraderResult(self.name, score, score >= self.threshold,
                            detail=f"reason={reason}", duration_ms=(time.time() - t0) * 1000,
                            tokens=tokens, cost_usd=cost)


@GRADERS.register("factuality")
class FactualityScorer(_JudgeScorer):
    """Factuality：pred 相对标准答案的事实一致性（0~1）。"""
    name = "factuality"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 0.6):
        super().__init__(judge_provider, human_labels, threshold)
        self.rubric = ("判断被测回答与标准答案在事实上是否一致（允许表述差异，"
                       "不容忍捏造/矛盾）。1=完全一致，0=存在事实错误。")


@GRADERS.register("faithfulness")
class FaithfulnessScorer(_JudgeScorer):
    """Faithfulness（RAG 接地性）：pred 是否仅基于【检索上下文】、无幻觉。"""
    name = "faithfulness"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 0.6):
        super().__init__(judge_provider, human_labels, threshold)
        self.rubric = ("判断被测回答是否完全可由【检索上下文】支撑、无超出上下文的捏造。"
                       "1=完全接地，0=出现幻觉。")

    def _extra(self, case: Case) -> str:
        ctx = case.meta.get("context", "")
        return f"检索上下文: {ctx}\n" if ctx else ""


@GRADERS.register("context_precision")
class ContextPrecisionScorer(_JudgeScorer):
    """ContextPrecision（RAG）：检索上下文里『相关』内容是否排在前面。"""
    name = "context_precision"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 0.6):
        super().__init__(judge_provider, human_labels, threshold)
        self.rubric = "评估检索上下文的整体精度（相关片段占比与排序）。1=高精，0=几乎无关。"

    def _extra(self, case: Case) -> str:
        ctx = case.meta.get("context", "")
        return f"检索上下文: {ctx}\n" if ctx else ""


@GRADERS.register("context_recall")
class ContextRecallScorer(_JudgeScorer):
    """ContextRecall（RAG）：标准答案所需信息是否都在检索上下文中。"""
    name = "context_recall"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 0.6):
        super().__init__(judge_provider, human_labels, threshold)
        self.rubric = "评估检索上下文是否覆盖了回答标准答案所需的全部信息。1=全覆盖，0=大量缺失。"

    def _extra(self, case: Case) -> str:
        ctx = case.meta.get("context", "")
        return f"检索上下文: {ctx}\n" if ctx else ""


@GRADERS.register("context_relevancy")
class ContextRelevancyScorer(_JudgeScorer):
    """ContextRelevancy（RAG）：检索上下文与问题的相关程度。"""
    name = "context_relevancy"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 0.6):
        super().__init__(judge_provider, human_labels, threshold)
        self.rubric = "评估检索上下文与问题的相关程度。1=高度相关，0=无关。"

    def _extra(self, case: Case) -> str:
        ctx = case.meta.get("context", "")
        return f"检索上下文: {ctx}\n" if ctx else ""


@GRADERS.register("answer_relevancy")
class AnswerRelevancyScorer(_JudgeScorer):
    """AnswerRelevancy：回答是否切题、回答了问题。"""
    name = "answer_relevancy"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 0.6):
        super().__init__(judge_provider, human_labels, threshold)
        self.rubric = "评估回答与问题的相关程度（是否真正回答了所问）。1=高度切题，0=答非所问。"


@GRADERS.register("summary")
class SummaryScorer(_JudgeScorer):
    """Summary：摘要质量（相对于原文是否保留关键信息、无冗余）。"""
    name = "summary"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 0.6):
        super().__init__(judge_provider, human_labels, threshold)
        self.rubric = "评估摘要是否准确覆盖原文要点、无冗余与捏造。1=优，0=差。"

    def _extra(self, case: Case) -> str:
        src = case.meta.get("source_text", "")
        return f"原文: {src}\n" if src else ""


@GRADERS.register("closed_qa")
class ClosedQAScorer(_JudgeScorer):
    """ClosedQA：封闭式问答质量（是否基于知识正确作答）。"""
    name = "closed_qa"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 0.6):
        super().__init__(judge_provider, human_labels, threshold)
        self.rubric = "评估回答是否正确回答了问题（可指定约束）。1=正确，0=错误。"


@GRADERS.register("translation")
class TranslationScorer(_JudgeScorer):
    """Translation：翻译质量（相对专家译文是否准确通顺）。"""
    name = "translation"

    def __init__(self, judge_provider=None, human_labels=None, threshold: float = 0.6):
        super().__init__(judge_provider, human_labels, threshold)
        self.rubric = "评估翻译相对于标准译文的准确性与流畅度。1=优，0=错译。"


# ===================== 目录（供 CLI / Web 列举）=====================
AUTOEVALS: dict = {
    "valid_json": "代码·响应是否为合法 JSON（可选 schema 校验）",
    "json_diff": "代码·pred 与 gold 的 JSON 递归相似度",
    "exact_match": "代码·归一化精确匹配",
    "contains": "代码·包含/数值核心一致",
    "levenshtein": "代码·字符串编辑相似度",
    "numeric_diff": "代码·数值归一化差异",
    "list_contains": "代码·列表语义重叠",
    "embedding_similarity": "代码·余弦相似度（需配置 embedder）",
    "moderation": "代码·规则词表违规检测（离线）",
    "factuality": "Judge·事实一致性",
    "faithfulness": "Judge·RAG 接地性（无幻觉）",
    "context_precision": "Judge·检索精度",
    "context_recall": "Judge·检索召回",
    "context_relevancy": "Judge·检索相关度",
    "answer_relevancy": "Judge·回答切题度",
    "summary": "Judge·摘要质量",
    "closed_qa": "Judge·封闭式问答",
    "translation": "Judge·翻译质量",
}


def list_autoevals() -> dict:
    """返回可用预置评测器目录（名称->说明）。"""
    return dict(AUTOEVALS)
