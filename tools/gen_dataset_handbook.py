#!/usr/bin/env python
"""从 catalog.json 生成《评测数据集手册》HTML（单一事实来源，catalog 改了重跑即可）。

用法：
    python tools/gen_dataset_handbook.py [输出路径]
默认输出到工作区根的 `评测数据集手册.html`，再用 tools/html2pdf.sh 转 PDF。
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parent.parent
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

from eval_harness.core.datasets_catalog import (  # noqa: E402
    CATALOG_PATH, TIER_ORDER, list_datasets, summary,
)

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HARNESS_ROOT.parent / "评测数据集手册.html"

TIER_META = {
    "basic": ("基础", "通用能力基线：中英知识、数学推理、科学推理、中文特定知识、标准化考试。零外部依赖，分钟级出结果。",
              "先看「这个智能体基本盘稳不稳」"),
    "advanced": ("进阶", "安全与集成风险面：越权/兜底/契约/归因四探针，加上提示注入、越狱、中文内容安全、PII、幂等、跨环境一致性。",
                 "要看交界层风险与安全底线（交付验收建议必备）"),
    "deep": ("深度", "垂直领域与复杂能力：法律/金融/医疗、端到端 RAG、工具调用契约、代码生成（沙箱真执行单测）、指令跟随、长上下文、多智能体交接。",
             "业务正确性需要行业口径支撑时"),
    "expert": ("专家（环境）", "需代码沙箱 / 浏览器 / VM 的真实 Agent 环境。", "需要真刀真枪跑环境，属重活，需提前排期"),
}


def esc(x) -> str:
    return html.escape(str(x if x is not None else "—"))


def yndef(v) -> str:
    return "可商用" if v else "非商用（研究用途）"


def main() -> None:
    cat = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    datasets = list_datasets()
    s = summary()
    generated = cat.get("generated_by", "")

    parts: list[str] = []
    parts.append(f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>评测数据集手册 · 质擎智评</title>
<style>
  :root {{ --ink:#111827; --ink2:#4b5563; --ink3:#6b7280; --line:#e5e7eb; --line-soft:#f1f3f5;
           --brand:#1d4ed8; --brand-soft:#eff4ff; --ok:#047857; --ok-soft:#ecfdf5;
           --warn:#b45309; --warn-soft:#fffbeb; --danger:#b91c1c; --danger-soft:#fef2f2; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:44px 40px 64px; background:#fff; color:var(--ink);
          font-family:-apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
          line-height:1.7; font-size:14px; }}
  .wrap {{ max-width:1080px; margin:0 auto; }}
  h1 {{ font-size:27px; margin:0 0 6px; letter-spacing:.2px; }}
  h2 {{ font-size:18px; margin:40px 0 12px; padding-bottom:8px; border-bottom:2px solid var(--line); }}
  h3 {{ font-size:15px; margin:24px 0 8px; }}
  .sub {{ color:var(--ink3); font-size:13px; margin-bottom:26px; }}
  .kpis {{ display:flex; gap:12px; flex-wrap:wrap; margin:18px 0 8px; }}
  .kpi {{ flex:1 1 150px; border:1px solid var(--line); border-radius:12px; padding:13px 15px; }}
  .kpi .n {{ font-size:23px; font-weight:650; }}
  .kpi .l {{ color:var(--ink3); font-size:12px; margin-top:2px; }}
  .cards {{ display:flex; gap:10px; flex-wrap:wrap; margin:14px 0 6px; }}
  .card {{ flex:1 1 220px; border:1px solid var(--line); border-radius:12px; padding:13px 15px; }}
  .card .t {{ font-weight:650; font-size:14px; }}
  .card .d {{ color:var(--ink2); font-size:12.5px; margin-top:5px; }}
  .card .w {{ color:var(--brand); font-size:12px; margin-top:7px; }}
  table {{ width:100%; border-collapse:collapse; margin:10px 0 4px; font-size:12.5px; }}
  th {{ background:#f8fafc; text-align:left; font-weight:600; color:var(--ink2);
        padding:8px 9px; border-bottom:1px solid var(--line); white-space:nowrap; }}
  td {{ padding:8px 9px; border-bottom:1px solid var(--line-soft); vertical-align:top; }}
  tr:nth-child(even) td {{ background:#fcfdfe; }}
  .tag {{ display:inline-block; padding:1px 7px; border-radius:999px; font-size:11.5px; white-space:nowrap; }}
  .t-ok {{ background:var(--ok-soft); color:var(--ok); }}
  .t-warn {{ background:var(--warn-soft); color:var(--warn); }}
  .t-gray {{ background:#f3f4f6; color:var(--ink3); }}
  .t-danger {{ background:var(--danger-soft); color:var(--danger); }}
  .t-blue {{ background:var(--brand-soft); color:var(--brand); }}
  code {{ background:#f3f4f6; padding:1px 5px; border-radius:4px; font-size:12px;
          font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
  pre {{ background:#0f172a; color:#e2e8f0; padding:14px 16px; border-radius:10px;
         overflow-x:auto; font-size:12px; line-height:1.65;
         font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
  pre .c {{ color:#94a3b8; }}
  .note {{ border-left:3px solid var(--brand); background:var(--brand-soft);
           padding:11px 14px; border-radius:0 8px 8px 0; margin:14px 0; font-size:13px; }}
  .warn {{ border-left:3px solid var(--warn); background:var(--warn-soft); }}
  .dng {{ border-left:3px solid var(--danger); background:var(--danger-soft); }}
  ul {{ margin:8px 0 8px 20px; padding:0; }}
  li {{ margin:4px 0; }}
  .muted {{ color:var(--ink3); font-size:12px; }}
  ol {{ margin:8px 0 8px 22px; padding:0; }}
</style>
</head>
<body><div class="wrap">
<h1>评测数据集手册</h1>
<div class="sub">质擎智评 · 智能体交付验收平台　|　数据来源：评测引擎数据集注册表（catalog.json）　|　{esc(generated)}</div>

<div class="kpis">
  <div class="kpi"><div class="n">{s['total']}</div><div class="l">收录数据集</div></div>
  <div class="kpi"><div class="n">{s['ready']}</div><div class="l">可直接运行</div></div>
  <div class="kpi"><div class="n">{s['env']}</div><div class="l">环境类（占位待接入）</div></div>
  <div class="kpi"><div class="n">{sum(1 for d in datasets if d.get('commercial_use'))}</div><div class="l">可商用许可</div></div>
</div>

<h2>一、分级框架：什么时候选哪一档</h2>
<div class="cards">
""")

    for t in TIER_ORDER:
        name, desc, when = TIER_META[t]
        n = sum(1 for d in datasets if d.get("tier") == t)
        parts.append(
            f'  <div class="card"><div class="t">{esc(name)}　<span class="muted">{n} 套</span></div>'
            f'<div class="d">{esc(desc)}</div><div class="w">适用：{esc(when)}</div></div>\n'
        )
    parts.append("</div>\n")

    parts.append("""<div class="note">
选数据集不是"越多越好"：<strong>基础档</strong>回答"基本盘稳不稳"，几分钟出结果；
<strong>进阶档</strong>是安全与集成风险面（越权/兜底/契约/归因四探针 + 注入/越狱/PII/幂等/跨环境一致性），
是本项目的差异化战场，交付验收建议必备；
<strong>深度档</strong>用于业务正确性需要行业口径支撑的场景（法律/金融/医疗）与复杂能力（代码生成、指令跟随、长上下文、多智能体交接）；
<strong>专家（环境）档</strong>是重活，需提前排期。
不指定数据集时，评测走工作台内置引擎的默认标准集。
</div>

<h2>二、数据集清单（按档分组）</h2>
""")

    for ti, t in enumerate(TIER_ORDER, start=1):
        name = TIER_META[t][0]
        rows = [d for d in datasets if d.get("tier") == t]
        parts.append(f"<h3>2.{ti}　{esc(name)}（{len(rows)} 套）</h3>\n")
        parts.append(
            "<table><thead><tr>"
            "<th>数据集</th><th>用例</th><th>估算耗时</th><th>估算 token</th>"
            "<th>评分器</th><th>许可 / 商用</th><th>状态</th><th>适用场景</th>"
            "</tr></thead><tbody>\n"
        )
        for d in rows:
            status = d["status"]
            tone = {"ready": "t-ok", "env": "t-warn", "planned": "t-gray"}.get(status, "t-gray")
            judge = ' <span class="tag t-blue">需 judge</span>' if d.get("recommends_judge") else ""
            lic_tone = "t-ok" if d.get("commercial_use") else "t-danger"
            parts.append(
                "<tr>"
                f"<td><strong>{esc(d['name'])}</strong><div class=\"muted\">{esc(d['id'])}</div></td>"
                f"<td>{esc(d.get('actual_cases'))}</td>"
                f"<td>约 {esc(d.get('est_duration_min'))} 分钟</td>"
                f"<td>≈ {d.get('est_tokens', 0):,}</td>"
                f"<td>{esc(d.get('recommended_grader'))}{judge}</td>"
                f"<td>{esc(d.get('license'))}<br><span class=\"tag {lic_tone}\">{yn_def_short(d.get('commercial_use'))}</span></td>"
                f"<td><span class=\"tag {tone}\">{esc(d.get('status_label'))}</span></td>"
                f"<td>{esc(d.get('scenario'))}</td>"
                "</tr>\n"
            )
        parts.append("</tbody></table>\n")

    parts.append("""
<div class="muted">耗时与 token 为「模型相关估算值」（取决于被测模型的响应长度与并发），跑完后由实测回写校准，不作为报价依据。</div>

<h2>三、评测人员怎么用</h2>

<h3>3.1　命令行（引擎侧直跑）</h3>
<pre><span class="c"># 列出全部数据集（分级 / 状态 / 条数）</span>
python -m eval_harness --list-datasets

<span class="c"># 跑一套数据集：位置参数传数据集 id 即可（不必记文件路径）</span>
python -m eval_harness lawbench-deep --provider mock --grader code --print

<span class="c"># judge 类数据集（RAG 忠实度 / 幻觉）必须带 judge 模型</span>
python -m eval_harness ragbench-deep --provider mock --grader judge \\
    --judge-provider mock --judge-provider-kwargs '{"mode":"judge"}'

<span class="c"># 校验文件就位与条数 / 输出可跑集 SHA256</span>
python tools/fetch_datasets.py --check
python tools/fetch_datasets.py --verify</pre>

<h3>3.2　工作台（业务侧选用）</h3>
<ol>
  <li>新建评测 → 走到 <strong>第 7 步「选择评测数据集」</strong>；</li>
  <li>按四档切换，卡片上直接看用例数、估算耗时 / token、许可、适用场景；</li>
  <li>选中某套 → 提交后本次评测由评测引擎执行，<strong>报告会写明所用数据集与版本</strong>，可按 id 复现；</li>
  <li>不选 → 走内置引擎的默认标准集，不受影响。</li>
</ol>

<div class="note warn">
<strong>两条硬提示：</strong>
① 标了 <span class="tag t-blue">需 judge</span> 的数据集（RGB、RAGBench）必须配置 judge 模型才能评分；
未配置时引擎会用内置占位实现，<strong>报告里会明确声明该部分仅用于链路验证、不构成交付结论</strong>。
② 标了 <span class="tag t-danger">非商用</span> 的数据集（AgentHarm、RGB、GAIA、FinEval，以及许可待确认的 CMMLU / SafetyBench / CMExam）仅限研究用途，商用前须经法务确认。
</div>

<h2>四、替换为官方原始数据（联网主机）</h2>
<p>仓库内已预置<strong>结构对齐的本地构造样本</strong>，保证离线也能跑通全链路。
要换成官方原始数据，在<strong>能访问 HuggingFace 的主机</strong>上执行：</p>
<pre><span class="c"># 0) 一次性：装数据集依赖</span>
pip install datasets

<span class="c"># 1) 联网探测：先看官方数据集的真实字段名（用于校准转换器映射）</span>
python tools/fetch_datasets.py --probe

<span class="c"># 2) 拉取并抽样替换（--force 覆盖样本，--pin 把版本/条数/sha256 写回 catalog 留痕）</span>
python tools/fetch_datasets.py --only mmlu-en-basic,ceval-basic,gsm8k-basic,arc-basic,cmmlu-basic,math-basic \\
    --sample 200 --seed 7 --force --pin v1.0-official
python tools/fetch_datasets.py --only agentharm-advanced,rgb-advanced,safetybench-advanced \\
    --sample 100 --seed 7 --force --pin v1.0-official
python tools/fetch_datasets.py --only lawbench-deep,ragbench-deep,bfcl-deep,humaneval-deep,fineval-deep \\
    --sample 100 --seed 7 --force --pin v1.0-official

<span class="c"># 3) 校验替换结果</span>
python tools/fetch_datasets.py --check</pre>
<div class="muted"><strong>哪些能换、哪些不能换</strong>——判据是「官方题集的字段结构是否与 harness 用例同构」：
<ul style="margin:8px 0 8px">
  <li><span class="tag t-ok">可一条命令替换</span>：上面三条命令覆盖的集（四选一 / 数值答案类，转换器已就绪）。</li>
  <li><span class="tag t-warn">自研集，无需替换</span>：集成风险面四探针、提示注入、越狱、中文安全、PII、幂等、
      跨环境一致性、长上下文、多智能体交接——<strong>本地样本就是正式来源</strong>，不存在官方原始数据，
      fetch 会直接报 <code>internal</code>，不会误覆盖。</li>
  <li><span class="tag t-warn">判分逻辑不同构</span>：AGIEval（题库打包）、CMExam（走 Git 仓库）、
      IFEval（25 类指令校验器需实现）——需另写执行桥接，目前 fetch 会报 <code>no_converter</code>，
      不硬编、不假装能拉。</li>
  <li><span class="tag t-danger">环境类</span>：需沙箱 / 浏览器 / VM，见第六节。</li>
</ul>
执行前先跑 <code>--probe</code> 看官方真实字段名；字段随版本漂移时以官方 README 为准。
本沙箱环境<strong>无法访问 HuggingFace</strong>（连接超时）且未安装 datasets 库，故替换动作需在你的联网主机执行。</div>

<h2>五、许可与商用</h2>
<ul>
  <li><span class="tag t-ok">可商用</span>：MMLU、GSM8K、MATH、HumanEval（MIT）、C-Eval、LawBench、
      BFCL、IFEval（Apache-2.0）、RAGBench（MIT）、AGIEval（MIT）、ARC（CC-BY-SA-4.0），
      以及<strong>本项目全部自建集</strong>（集成风险面四探针、提示注入、越狱、PII、幂等、
      跨环境一致性、长上下文、多智能体交接）。</li>
  <li><span class="tag t-danger">非商用 / 待法务确认</span>：AgentHarm（研究用途）、RGB（研究用途）、
      GAIA（CC-BY-4.0 限非商业）、FinEval（CC-BY-NC）、CMMLU / SafetyBench / CMExam（许可需确认）
      ——<strong>入库与对外交付前须法务确认</strong>。</li>
  <li>本项目自建的「集成风险面 / 业务正确性 / 跨环境一致性 / 多智能体交接」类数据集不依赖第三方许可，
      是最可放心对外交付的部分，也正是与公开 benchmark 的差异所在。</li>
</ul>

<h2>六、当前空白与后续</h2>
<ul>
  <li><strong>环境类未接入（10 套）</strong>：SWE-bench Lite / SWE-bench Verified / WebArena / OSWorld /
      GAIA / τ-bench / τ²-bench / Terminal-Bench / AgentBench / MultiAgentBench——
      目前仅登记接入说明（元数据占位），需代码沙箱 / 浏览器 / VM 桥接，属重活，需排期。</li>
  <li><strong>本轮已把两处公开空白自建落地</strong>：多智能体协作 → multiagent-handoff-deep
      （用 trajectory 评分器给出交接覆盖率与最早出错步骤，失败可归因而非只看终分）；
      跨环境一致性 → consistency-advanced（跨环境口径 / 跨轮约束保持 / 单位与时区归一 /
      指标定义冲突 / 版本漂移，8 类）。</li>
  <li><strong>仍需自建的方向</strong>：① 成本效能联合——token 与延迟要和正确性一起判，而不是单看通过率；
      ② 争议定责深化——attribution 目前是确定性检查，尚缺多方责任划分与举证链；
      ③ 中国合规预检——面向公众有舆论属性服务的备案与内容合规，目前只覆盖了内容安全一类。</li>
  <li><strong>judge 通道未接真实模型</strong>：RGB / RAGBench 仍走占位判官（报告会声明「不构成交付结论」），
      接入真实评分模型后方可作为交付依据。</li>
  <li><strong>官方原始数据未拉取</strong>：见第四节，联网主机执行三条命令即可替换「可替换」的部分。</li>
  <li><strong>耗时 / token 仍是估算值</strong>：需要在联网 / 真实模型条件下跑一批，用实测值回写 catalog 校准。</li>
</ul>

<div class="muted" style="margin-top:34px;padding-top:14px;border-top:1px solid var(--line)">
质擎智评（QualEngine）· AgentEval　|　本手册由 catalog.json 自动生成，数据集清单更新后重跑
<code>python tools/gen_dataset_handbook.py</code> 即可。
</div>

</div></body></html>
""")

    OUT.write_text("".join(parts), encoding="utf-8")
    print(f"written: {OUT}  ({OUT.stat().st_size} bytes)")


def yn_def_short(v) -> str:
    return "可商用" if v else "非商用"


if __name__ == "__main__":
    main()
