"""eval_harness · 数据集分类体系（taxonomy）

背景：catalog 原有的 `category` 是自由文本「一级 / 二级」（如
`智化融合系统 / 集成风险面·权限边界`），38 条里出现 35 种不同取值，且分类名自身
含 `/`（`Web/OS/GUI 智能体`）导致按 `/` 切分也会切坏。结果就是「有分类，但不可比、
不可筛、不可统计」。

本模块提供一套**受控两级分类**，作为界面分组 / 筛选 / 统计的唯一依据：

- 一级 `domain`（域）：按「评测对象是什么」收敛为 10 个受控枚举，带中文标签与一句说明。
  有序（DOMAIN_ORDER），界面按此顺序分组。
- 二级 `capabilities`（能力）：按「考什么能力」收敛为受控标签集，每条数据集 1–3 个。

设计原则：
1. **受控枚举，不是自由文本** —— 新增数据集只能落在既有域里；确实装不下的，先在此
   处加枚举再落数据，避免又退回碎片化。
2. **显式字段优先** —— 数据集条目里的 `domain` / `capabilities` 是权威值（界面上可编辑
   并回写 catalog）。缺失时回落到本模块的映射表，再回落到从 object_axes / dimensions
   派生，保证任何时候都算得出分类，不会出现「无分类」。
3. **原 `category` 不删除** —— 降级为「细分说明」，在详情里展示，保留信息量。
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 一级：域（评测对象）
# ---------------------------------------------------------------------------
# 顺序即界面分组顺序：先摆本项目差异化核心（集成风险面 / 安全），再通用能力，
# 再专项智能体，最后垂直行业。
DOMAINS: dict[str, dict] = {
    "integration_risk": {
        "label": "集成风险面",
        "desc": "智化融合系统交界层：权限边界 / 失效兜底 / 输出契约 / 责任归属 / 幂等 / 注入 / 隐私 / 跨环境一致性——公开数据集覆盖不到的差异化地带。",
    },
    "safety": {
        "label": "安全与合规",
        "desc": "有害行为、越狱与绕过、中文内容安全。",
    },
    "general_llm": {
        "label": "通用大模型",
        "desc": "单轮问答、知识、数学与科学推理、指令跟随、长上下文、多环境综合能力基线。",
    },
    "general_assistant": {
        "label": "通用助手",
        "desc": "多步真实任务编排（跨工具、跨信息源的端到端交付）。",
    },
    "coding_agent": {
        "label": "编码智能体",
        "desc": "函数级代码生成、真实仓库修复、终端与系统工程操作。",
    },
    "tool_use_agent": {
        "label": "工具调用智能体",
        "desc": "函数调用格式正确性与多轮工具编排（含双向控制）。",
    },
    "rag_agent": {
        "label": "RAG 智能体",
        "desc": "检索增强生成的忠实度与端到端质量。",
    },
    "multi_agent": {
        "label": "多智能体协作",
        "desc": "多智能体之间的交接契约、责任归属与协作/竞争。",
    },
    "gui_web_agent": {
        "label": "Web / GUI 智能体",
        "desc": "网页导航与桌面图形界面操作。",
    },
    "vertical": {
        "label": "垂直行业",
        "desc": "强监管与强专业行业的业务正确性：金融、法律、医疗、教育、政务、网络安全、电力能源、农业、电信、制造——覆盖「智化融合系统」在行业落地时最易翻车的业务口径与领域知识。",
    },
}

DOMAIN_ORDER: list[str] = list(DOMAINS.keys())
DOMAIN_LABELS: dict[str, str] = {k: v["label"] for k, v in DOMAINS.items()}

# 兼容旧 object_axes：无法从显式字段与 DOMAIN_OF 判定时的回落
_AXES_TO_DOMAIN = {
    "integration_risk": "integration_risk",
    "safety_agent": "safety",
    "general_llm": "general_llm",
    "general_assistant": "general_assistant",
    "coding_agent": "coding_agent",
    "tool_use_agent": "tool_use_agent",
    "rag_agent": "rag_agent",
    "multi_agent": "multi_agent",
    "gui_agent": "gui_web_agent",
    "web_agent": "gui_web_agent",
    "vertical_finance": "vertical",
    "vertical_legal": "vertical",
    "vertical_medical": "vertical",
    "vertical_education": "vertical",
    "vertical_government": "vertical",
    "vertical_cybersec": "vertical",
    "vertical_energy": "vertical",
    "vertical_agriculture": "vertical",
    "vertical_telecom": "vertical",
    "vertical_manufacturing": "vertical",
}


# ---------------------------------------------------------------------------
# 二级：能力（考什么）
# ---------------------------------------------------------------------------
CAPABILITIES: dict[str, str] = {
    # 通用能力
    "knowledge": "知识问答",
    "reasoning": "推理",
    "math": "数学",
    "china_specific": "中国特定",
    "instruction_following": "指令跟随",
    "long_context": "长上下文",
    "multi_env": "多环境综合",
    "multi_step_task": "多步任务",
    # 专项能力
    "code_generation": "代码生成",
    "repo_repair": "仓库修复",
    "tool_use": "工具调用",
    "multi_turn": "多轮交互",
    "rag_faithfulness": "RAG 忠实度",
    "gui_web": "界面操作",
    "multi_agent_handoff": "多智能体交接",
    # 安全
    "safety_refusal": "安全拒答",
    "jailbreak": "越狱抗性",
    "content_safety": "内容安全",
    # 集成风险面
    "permission": "权限边界",
    "fallback": "失效兜底",
    "output_contract": "输出契约",
    "attribution": "责任归属",
    "idempotency": "幂等与并发",
    "privacy": "隐私保护",
    "prompt_injection": "提示注入",
    "cross_env_consistency": "跨环境一致性",
    # 垂直
    "domain_finance": "金融领域",
    "domain_legal": "法律领域",
    "domain_medical": "医疗领域",
    "domain_education": "教育领域",
    "domain_government": "政务领域",
    "domain_cybersec": "网络安全领域",
    "domain_energy": "电力能源领域",
    "domain_agriculture": "农业领域",
    "domain_telecom": "电信领域",
    "domain_manufacturing": "制造领域",
}

# 兼容旧 dimensions：显式能力缺失时的派生来源
_DIM_TO_CAP = {
    "single_turn_qa": "knowledge", "knowledge": "knowledge", "domain_knowledge": "knowledge",
    "reasoning": "reasoning", "scientific_reasoning": "reasoning", "math_reasoning": "math",
    "china_specific": "china_specific", "china_compliance": "china_specific",
    "instruction_following": "instruction_following", "format_control": "instruction_following",
    "long_context": "long_context", "information_retrieval": "long_context",
    "multi_env": "multi_env", "multi_step": "multi_step_task",
    "coding": "code_generation", "code_generation": "code_generation",
    "tool_use": "tool_use", "function_calling": "tool_use",
    "multi_turn": "multi_turn",
    "rag": "rag_faithfulness", "faithfulness": "rag_faithfulness",
    "answer_relevance": "rag_faithfulness",
    "gui": "gui_web", "web_navigation": "gui_web",
    "multi_agent": "multi_agent_handoff", "handoff": "multi_agent_handoff",
    "collaboration": "multi_agent_handoff", "trajectory": "multi_agent_handoff",
    "safety": "safety_refusal", "harmful_agent_behavior": "safety_refusal",
    "jailbreak_resistance": "jailbreak", "content_safety": "content_safety",
    "permission_boundary": "permission", "failure_fallback": "fallback",
    "output_contract": "output_contract", "responsibility_attribution": "attribution",
    "idempotency": "idempotency", "reliability": "idempotency",
    "privacy": "privacy", "deidentification": "privacy",
    "prompt_injection": "prompt_injection", "input_trust": "prompt_injection",
    "consistency": "cross_env_consistency",
    "environment": None,  # 这是「需真实环境」的标记，不是能力，派生时丢弃
}

CAPABILITY_ORDER: list[str] = list(CAPABILITIES.keys())


# ---------------------------------------------------------------------------
# 逐条显式映射（作为 catalog 回填与派生兜底的事实来源）
# ---------------------------------------------------------------------------
# 只写「不写就会判错」的条目；新增数据集应直接在 catalog 条目里写 domain/capabilities。
DOMAIN_OF: dict[str, str] = {
    # —— 集成风险面 ——
    "consistency-advanced": "integration_risk",
    "idempotency-advanced": "integration_risk",
    "injection-advanced": "integration_risk",
    "integration-attribution": "integration_risk",
    "integration-contract": "integration_risk",
    "integration-fallback": "integration_risk",
    "integration-permission": "integration_risk",
    "pii-advanced": "integration_risk",
    # —— 安全与合规 ——
    "agentharm-advanced": "safety",
    "jailbreak-advanced": "safety",
    "safetybench-advanced": "safety",
    # —— 通用大模型 ——
    "agieval-basic": "general_llm",
    "arc-basic": "general_llm",
    "ceval-basic": "general_llm",
    "cmmlu-basic": "general_llm",
    "gsm8k-basic": "general_llm",
    "math-basic": "general_llm",
    "mmlu-en-basic": "general_llm",
    "ifeval-deep": "general_llm",
    "longcontext-deep": "general_llm",
    "agentbench": "general_llm",
    # —— 通用助手 ——
    "gaia": "general_assistant",
    # —— 编码智能体 ——
    "humaneval-deep": "coding_agent",
    "swebench-lite": "coding_agent",
    "swebench-verified": "coding_agent",
    "terminal-bench": "coding_agent",
    # —— 工具调用 ——
    "bfcl-deep": "tool_use_agent",
    "tau-bench": "tool_use_agent",
    "tau2-bench": "tool_use_agent",
    # —— RAG ——
    "rgb-advanced": "rag_agent",
    "ragbench-deep": "rag_agent",
    # —— 多智能体 ——
    "multiagent-handoff-deep": "multi_agent",
    "multiagentbench": "multi_agent",
    # —— Web / GUI ——
    "osworld": "gui_web_agent",
    "webarena": "gui_web_agent",
    # —— 垂直行业 ——
    "cmeval-deep": "vertical",
    "fineval-deep": "vertical",
    "lawbench-deep": "vertical",
    "edueval-deep": "vertical",
    "msgabench-deep": "vertical",
    "secbench-deep": "vertical",
    "elecbench-deep": "vertical",
    "agrieval-deep": "vertical",
    "telecom-deep": "vertical",
    "industry-deep": "vertical",
}

CAPS_OF: dict[str, list[str]] = {
    # 集成风险面
    "consistency-advanced": ["cross_env_consistency", "output_contract"],
    "idempotency-advanced": ["idempotency", "fallback"],
    "injection-advanced": ["prompt_injection", "permission"],
    "integration-attribution": ["attribution", "output_contract"],
    "integration-contract": ["output_contract", "fallback"],
    "integration-fallback": ["fallback", "attribution"],
    "integration-permission": ["permission", "attribution"],
    "pii-advanced": ["privacy", "content_safety"],
    # 安全
    "agentharm-advanced": ["safety_refusal", "content_safety"],
    "jailbreak-advanced": ["jailbreak", "safety_refusal"],
    "safetybench-advanced": ["content_safety", "safety_refusal"],
    # 通用大模型
    "agieval-basic": ["knowledge", "reasoning", "china_specific"],
    "arc-basic": ["reasoning", "knowledge"],
    "ceval-basic": ["knowledge", "china_specific"],
    "cmmlu-basic": ["knowledge", "china_specific"],
    "gsm8k-basic": ["math", "reasoning"],
    "math-basic": ["math", "reasoning"],
    "mmlu-en-basic": ["knowledge", "reasoning"],
    "ifeval-deep": ["instruction_following"],
    "longcontext-deep": ["long_context", "instruction_following"],
    "agentbench": ["multi_env", "multi_step_task"],
    # 通用助手
    "gaia": ["multi_step_task", "tool_use"],
    # 编码
    "humaneval-deep": ["code_generation"],
    "swebench-lite": ["repo_repair", "code_generation"],
    "swebench-verified": ["repo_repair", "code_generation"],
    "terminal-bench": ["repo_repair", "multi_step_task"],
    # 工具调用
    "bfcl-deep": ["tool_use", "output_contract"],
    "tau-bench": ["tool_use", "multi_turn"],
    "tau2-bench": ["tool_use", "multi_turn"],
    # RAG
    "rgb-advanced": ["rag_faithfulness"],
    "ragbench-deep": ["rag_faithfulness", "knowledge"],
    # 多智能体
    "multiagent-handoff-deep": ["multi_agent_handoff", "attribution"],
    "multiagentbench": ["multi_agent_handoff", "multi_env"],
    # Web / GUI
    "osworld": ["gui_web", "multi_step_task"],
    "webarena": ["gui_web", "multi_step_task"],
    # 垂直
    "cmeval-deep": ["domain_medical", "knowledge"],
    "fineval-deep": ["domain_finance", "knowledge"],
    "lawbench-deep": ["domain_legal", "knowledge"],
    "edueval-deep": ["domain_education", "knowledge"],
    "msgabench-deep": ["domain_government", "knowledge"],
    "secbench-deep": ["domain_cybersec", "knowledge"],
    "elecbench-deep": ["domain_energy", "knowledge"],
    "agrieval-deep": ["domain_agriculture", "knowledge"],
    "telecom-deep": ["domain_telecom", "knowledge"],
    "industry-deep": ["domain_manufacturing", "knowledge"],
}


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------
def is_domain(key: str) -> bool:
    return key in DOMAINS


def is_capability(key: str) -> bool:
    return key in CAPABILITIES


def domain_of(entry: dict) -> str:
    """解析条目所属域。显式字段 > 逐条映射 > object_axes 推导 > other。"""
    d = str(entry.get("domain") or "").strip()
    if d and is_domain(d):
        return d
    did = str(entry.get("id") or "")
    if did in DOMAIN_OF:
        return DOMAIN_OF[did]
    for ax in (entry.get("object_axes") or []):
        if ax in _AXES_TO_DOMAIN:
            return _AXES_TO_DOMAIN[ax]
    return "other"


def capabilities_of(entry: dict) -> list[str]:
    """解析条目能力标签（受控、去重、按 CAPABILITY_ORDER 排序）。

    显式字段优先；缺失时用逐条映射；再缺失时从旧 dimensions 派生。
    """
    raw = entry.get("capabilities")
    out: list[str] = []
    if isinstance(raw, list) and raw:
        out = [c for c in (str(x).strip() for x in raw) if is_capability(c)]
    if not out and str(entry.get("id") or "") in CAPS_OF:
        out = [c for c in CAPS_OF[str(entry["id"])] if is_capability(c)]
    if not out:
        for dim in (entry.get("dimensions") or []):
            cap = _DIM_TO_CAP.get(str(dim))
            if cap and cap not in out:
                out.append(cap)
    # 按受控顺序稳定排序，保证同一批数据每次渲染顺序一致
    return [c for c in CAPABILITY_ORDER if c in out][:4]


def domain_label_of(entry: dict) -> str:
    k = domain_of(entry)
    return DOMAINS.get(k, {}).get("label", "未分类")


def capability_labels(caps: list[str]) -> list[str]:
    return [CAPABILITIES.get(c, c) for c in caps]


def domains_meta() -> list[dict]:
    """给 UI 的域清单（含标签、说明、计数由调用方补）。"""
    return [{"key": k, "label": v["label"], "desc": v["desc"]} for k, v in DOMAINS.items()]


def capabilities_meta() -> list[dict]:
    return [{"key": k, "label": v} for k, v in CAPABILITIES.items()]
