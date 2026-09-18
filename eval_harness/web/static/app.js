"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

let charts = {};
let refreshTimer = null;

// ---------------- 工具 ----------------
function fmtPct(x) { return (x * 100).toFixed(1) + "%"; }
function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._t);
  t._t = setTimeout(() => t.classList.add("hidden"), 2400);
}
function rateBar(p) {
  const pct = Math.max(0, Math.min(100, p * 100));
  const color = p >= 0.6 ? "var(--green)" : p >= 0.3 ? "var(--amber)" : "var(--red)";
  return `<span class="rate-bar"><span class="bar"><i style="width:${pct}%;background:${color}"></i></span><b>${fmtPct(p)}</b></span>`;
}
function passTag(b) {
  return b ? `<span class="tag green">通过</span>` : `<span class="tag red">未过</span>`;
}
function statusBadge(st) {
  const map = {
    running: ["amber", "进行中"], completed: ["green", "已完成"],
    failed: ["red", "失败"], aborted: ["gray", "已中止"], pending: ["blue", "未开始"],
  };
  const [cls, label] = map[st] || ["gray", st || "未知"];
  return `<span class="tag ${cls}">${label}</span>`;
}
function progressBar(done, total) {
  const pct = total > 0 ? Math.round(done / total * 100) : 0;
  return `<span class="rate-bar"><span class="bar"><i style="width:${pct}%;background:var(--blue)"></i></span><b>${done}/${total}</b></span>`;
}

// ---------------- 导航 ----------------
const TITLES = {
  dashboard: ["概览", "交付验收全景"],
  runs: ["评测运行", "历次评测与明细"],
  traces: ["生产轨迹", "创新⑥ · 上线真实轨迹复评"],
  datasets: ["数据集", "目录管理 · 分类 / 元数据 / 增删改 / 体检"],
  dashboards: ["自定义看板", "N7 · 保存你的指标视图"],
  new: ["新建评测", "复用引擎发起一次评测"],
  batch: ["批量评测方案", "多选数据集 → 拆成 N 个独立 run 的覆盖矩阵"],
  providers: ["提供方管理", "R5 · 执行提供方（被测对象）增删改查"],
  judges: ["Judge 通道", "R6 · 评测用 LLM 自定义管理"],
  report: ["汇总报告", "R2 · 多数据集 / 多运行统一汇总"],
  guide: ["新手引导", "🧭 评测流程指引 · 分步可视化 + 一键跳转"],
};
function switchView(v) {
  if (!TITLES[v]) v = "dashboard";
  $$(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === v));
  $$(".view").forEach(s => s.classList.add("hidden"));
  $("#view-" + v).classList.remove("hidden");
  $("#view-title").textContent = TITLES[v][0];
  $("#view-sub").textContent = TITLES[v][1];
  // 用 replaceState 写 hash：视图可直接分享（#datasets），且不会触发 hashchange 造成回环
  if (location.hash.slice(1) !== v) history.replaceState(null, "", "#" + v);
  if (v === "dashboard") loadDashboard();
  else if (v === "runs") loadRuns();
  else if (v === "traces") loadTraces();
  else if (v === "datasets") loadDatasets();
  else if (v === "dashboards") loadDashboards();
  else if (v === "new") loadNew();
  else if (v === "providers") renderProviders();
  else if (v === "judges") renderJudges();
  else if (v === "report") renderReport();
  else if (v === "guide") renderGuide();
}

// ---------------- 概览 ----------------
async function loadDashboard() {
  const d = await (await fetch("/api/dashboard")).json();
  $("#backend-badge").textContent = d.backend === "postgresql" ? "PostgreSQL" : "SQLite";
  $("#ver-badge").textContent = "v" + d.harness_version;

  const k = d.kpis;
  $("#kpi-grid").innerHTML = [
    ["评测运行", k.runs, "次"],
    ["平均通过率", fmtPct(k.avg_pass_rate), "跨全部运行"],
    ["累计用例", k.cases, "题"],
    ["生产轨迹", k.traces, "条回灌"],
    ["Judge 成本", "$" + k.judge_cost_usd.toFixed(4), "累计投入"],
  ].map(([l, v, s]) => `
    <div class="kpi"><div class="k-label">${l}</div><div class="k-value">${v}</div><div class="k-sub">${s}</div></div>
  `).join("");

  // 趋势
  drawChart("chart-trend", "line", {
    labels: d.trend.map(r => r.name),
    datasets: [{
      label: "通过率", data: d.trend.map(r => +(r.pass_rate * 100).toFixed(1)),
      borderColor: "#0071e3", backgroundColor: "rgba(0,113,227,.12)",
      fill: true, tension: .3, pointRadius: 4, borderWidth: 2,
    }],
  }, { y: { ticks: { callback: v => v + "%" }, grid: { color: "#eee" } }, x: { grid: { display: false } } });

  // 三通道（仅取结构完整的记录，避免脏数据导致 NaN）
  const tw = (d.three_way || []).filter(r => typeof r.full_pass_rate === "number"
    && typeof r.bare_pass_rate === "number" && typeof r.harness_pass_rate === "number");
  if (tw.length) {
    drawChart("chart-threeway", "bar", {
      labels: tw.map(r => r.name),
      datasets: [
        { label: "bare(裸模型)", data: tw.map(r => +(r.bare_pass_rate * 100).toFixed(1)), backgroundColor: "#8e8e93" },
        { label: "harness(纯脚手架)", data: tw.map(r => +(r.harness_pass_rate * 100).toFixed(1)), backgroundColor: "#5e5ce6" },
        { label: "full(全系统)", data: tw.map(r => +(r.full_pass_rate * 100).toFixed(1)), backgroundColor: "#34c759" },
      ],
    }, { y: { ticks: { callback: v => v + "%" }, grid: { color: "#eee" } }, x: { grid: { display: false } } });
    const last = tw[tw.length - 1];
    const gain = typeof last.harness_only_gain === "number" ? last.harness_only_gain : (last.harness_pass_rate - last.bare_pass_rate);
    const leak = typeof last.scaffold_leakage === "number" ? last.scaffold_leakage : (last.full_pass_rate - last.bare_pass_rate);
    $("#readout-body").innerHTML = `
      <div class="chip"><div class="c-label">harness_only_gain</div><div class="c-value blue">${gain>=0?"+":""}${(gain*100).toFixed(1)}pp</div></div>
      <div class="chip"><div class="c-label">scaffold_leakage</div><div class="c-value amber">${(leak*100).toFixed(1)}pp</div></div>
      <div class="chip"><div class="c-label">full 口径</div><div class="c-value green">${(last.full_pass_rate*100).toFixed(1)}%</div></div>
      <div class="chip"><div class="c-label">样本 n</div><div class="c-value">${last.n ?? "—"}</div></div>`;
  } else {
    $("#readout-body").innerHTML = `<span class="muted">暂无含三通道解耦的运行（发起评测时勾选「脚手架三通道解耦」）。</span>`;
  }

  // 评分器构成
  const gm = d.grader_mix;
  const labels = Object.keys(gm);
  drawChart("chart-graders", "doughnut", {
    labels,
    datasets: [{ data: labels.map(l => gm[l]), backgroundColor: ["#0071e3", "#5e5ce6", "#34c759", "#ff9f0a", "#ff3b30", "#8e8e93"] }],
  }, { plugins: { legend: { position: "right" } } });
}

function drawChart(id, type, data, opts) {
  if (charts[id]) charts[id].destroy();
  const ctx = document.getElementById(id);
  charts[id] = new Chart(ctx, {
    type, data,
    options: Object.assign({ responsive: true, plugins: { legend: { labels: { font: { family: getComputedStyle(document.body).fontFamily } } } } }, opts),
  });
}

// ---------------- 运行列表 ----------------
let _runsPoll = null;
async function loadRuns() {
  await _fetchRuns();
  renderRuns($("#run-search") ? $("#run-search").value : "");
  if (!_runsPoll) _runsPoll = setInterval(() => {
    if ($("#view-runs").classList.contains("hidden") === false) {
      _fetchRuns().then(() => renderRuns($("#run-search").value)).catch(() => {});
    }
  }, 2500);
}
async function _fetchRuns() {
  const [runsRes, jobsRes] = await Promise.all([
    (await fetch("/api/runs?limit=500")).json(),
    (await fetch("/api/jobs")).json(),
  ]);
  window._runs = runsRes.runs;
  window._jobsMap = {};
  (jobsRes.jobs || []).forEach(j => { if (j.run_id) window._jobsMap[j.run_id] = j; });
}
function renderRuns(q) {
  const runs = window._runs || [];
  const f = (q || "").trim().toLowerCase();
  const body = $("#runs-body");
  const list = runs.filter(r =>
    !f || [r.name, r.model_version, r.provider, r.judge_provider].filter(Boolean).join(" ").toLowerCase().includes(f));
  if (!list.length) { body.innerHTML = `<tr><td colspan="9" class="muted">暂无运行记录。</td></tr>`; return; }
  window._selectedRuns = window._selectedRuns || new Set();
  body.innerHTML = list.map(r => {
    const sel = window._selectedRuns.has(r.id) ? "checked" : "";
    const job = window._jobsMap[r.id];
    const liveBar = (r.status === "running" && job)
      ? `<div class="mini-prog">${progressBar(job.done || 0, job.total || r.total || 0)}</div>` : "";
    // 运行中任务删除会被引擎 upsert 重新创建，故禁止删除（避免"删了又冒出来"）
    const delBtn = (r.status === "running")
      ? `<button class="btn xs ghost" title="运行中任务不可删除，请等待完成后重试" disabled>删除</button>`
      : `<button class="btn xs danger" data-del="${r.id}" data-name="${esc(r.name)}">删除</button>`;
    return `
    <tr data-id="${r.id}">
      <td><input type="checkbox" class="run-cb" value="${r.id}" ${sel} /></td>
      <td>${statusBadge(r.status)}${liveBar}</td>
      <td><b>${esc(r.name)}</b></td>
      <td><span class="tag gray">${esc(r.provider || "")}</span></td>
      <td>${rateBar(r.pass_rate || 0)}</td>
      <td>${r.total}</td>
      <td class="muted">${fmtTime(r.start_time || r.created_at)}</td>
      <td class="muted">${fmtTime(r.end_time)}</td>
      <td class="run-ops">${delBtn}</td>
    </tr>`;
  }).join("");
  $$("#runs-body tr").forEach(tr => {
    tr.onclick = (e) => {
      if (e.target.classList.contains("run-cb")) return;
      if (e.target.closest("[data-del]")) return;   // 删除按钮不触发详情
      openRun(tr.dataset.id);
    };
  });
  $$("#runs-body [data-del]").forEach(btn => {
    btn.onclick = (e) => { e.stopPropagation(); window.deleteRun(btn.dataset.del, btn.dataset.name); };
  });
  $$("#runs-body .run-cb").forEach(cb => cb.onchange = () => {
    cb.checked ? window._selectedRuns.add(cb.value) : window._selectedRuns.delete(cb.value);
  });
}

// ---------------- 删除运行（R-补充） ----------------
window.deleteRun = async function (id, name) {
  if (!confirm(`确定删除运行「${name || id}」？\n该操作会级联删除其全部逐题结果、轨迹与评分记录，且不可恢复。`)) return;
  try {
    const res = await (await fetch("/api/runs/" + id, { method: "DELETE" })).json();
    if (res.ok) {
      toast("已删除运行：" + (name || id));
      window._selectedRuns && window._selectedRuns.delete(id);
      await loadRuns();
    } else {
      toast("删除失败：" + (res.detail || JSON.stringify(res)));
    }
  } catch (e) {
    toast("删除请求异常：" + e.message);
  }
};

// ---------------- 新手评测流程引导（可视化 + 跳转） ----------------
const GUIDE_STEPS = [
  { n: 1, icon: "📚", title: "准备数据集（出考题）", view: "datasets",
    desc: "数据集就是「考题库」。控制台内置 agieval、fineval 等多套中文学科评测集；你也可以上传自己的题目（支持 JSONL 批量导入）。第一次用直接用内置集即可。" },
  { n: 2, icon: "🤖", title: "配置执行提供方（找考生）", view: "providers",
    desc: "「执行提供方」就是被测评的智能体 / 大模型，也就是上考场的「考生」。内置 mock（假数据，先练手）、deepseek 等；真实评测时在这里新增你的被测对象（填 API 地址 / 密钥）。" },
  { n: 3, icon: "⚖️", title: "配置 Judge 通道（请阅卷老师）", view: "judges",
    desc: "Judge 是负责判卷的 LLM（如 deepseek）。它对照标准答案给每道题打分。默认用内置 Judge 即可；需要更严格判卷时，在这里自定义你自己的 Judge 模型。" },
  { n: 4, icon: "🚀", title: "新建评测（开考）", view: "new",
    desc: "在「新建评测」里选数据集 + 选提供方 + 选 Judge，点「启动」即可开考。启动后立即返回任务号，页面不卡顿，可继续干别的。" },
  { n: 5, icon: "📡", title: "实时监控进度（看考试进行）", view: "runs",
    desc: "切到「评测运行」，进行中的任务会显示琥珀色「进行中」徽章和实时进度条（如 390/1321）。点任意一行可看逐题详情、实时进度与缺陷清单。" },
  { n: 6, icon: "📊", title: "查看汇总报告 / 缺陷清单（拿成绩单）", view: "report",
    desc: "勾选多个运行 →「生成汇总报告」得到统一成绩单（综合通过率 + 按提供方聚合）；单个运行详情里的「缺陷清单」就是错题本，按失败类型 / 学科分类，方便定位问题。" },
];

function renderGuide() {
  // 顶部流程图：数据集 → 提供方 → Judge → 新建评测 → 运行中 → 报告/缺陷
  const flow = [
    { t: "数据集", s: "考题" }, { t: "执行提供方", s: "考生" }, { t: "Judge 通道", s: "阅卷" },
    { t: "新建评测", s: "开考" }, { t: "运行中", s: "进度" }, { t: "报告/缺陷", s: "成绩单" },
  ];
  const fw = document.getElementById("guide-flow");
  if (fw) {
    const bw = 118, gap = 26, h = 84;
    const W = flow.length * bw + (flow.length - 1) * gap;
    fw.innerHTML = `<svg viewBox="0 0 ${W} ${h}" width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label="评测流水线流程图">
      ${flow.map((f, i) => {
        const x = i * (bw + gap);
        const arrow = i < flow.length - 1 ? `<line x1="${x + bw}" y1="${h/2}" x2="${x + bw + gap - 6}" y2="${h/2}" stroke="#94a3b8" stroke-width="2" marker-end="url(#ga)"/>` : "";
        return `<g>
          <rect x="${x}" y="8" width="${bw}" height="${h-16}" rx="12" fill="#f1f5f9" stroke="#cbd5e1" stroke-width="1.5"/>
          <text x="${x + bw/2}" y="${h/2 - 4}" text-anchor="middle" font-size="14" font-weight="700" fill="#0f172a">${f.t}</text>
          <text x="${x + bw/2}" y="${h/2 + 16}" text-anchor="middle" font-size="12" fill="#64748b">${f.s}</text>
          ${arrow}
        </g>`;
      }).join("")}
      <defs><marker id="ga" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6 Z" fill="#94a3b8"/></marker></defs>
    </svg>`;
  }
  // 时间轴步骤卡片
  const tl = document.getElementById("guide-timeline");
  if (tl) {
    tl.innerHTML = GUIDE_STEPS.map(s => `
      <div class="tl-item">
        <div class="tl-node">${s.icon}</div>
        <div class="tl-card">
          <div class="tl-head"><span class="tl-num">${s.n}</span><b>${s.title}</b></div>
          <div class="tl-desc">${s.desc}</div>
          <button class="btn primary xs" onclick="switchView('${s.view}')">去操作 ›（${ ({datasets:'数据集',providers:'提供方',judges:'Judge通道',new:'新建评测',runs:'评测运行',report:'汇总报告'})[s.view] }）</button>
        </div>
      </div>`).join("");
  }
}

// ---------------- 运行详情 ----------------
async function openRun(id) {
  const r = await (await fetch("/api/runs/" + id)).json();
  $("#drawer-title").textContent = r.name;
  const tw = r.three_way_json;
  const cost = (r.cases || []).reduce((s, c) => s + (c.graders || []).reduce((a, g) => a + (g.grader === "judge" ? (g.cost_usd || 0) : 0), 0), 0);
  const wb = (r.meta && r.meta.wb_meta) || {};
  const wbBack = wb.callback_url
    ? `<div class="dw-section wb-back"><a class="btn primary" href="${esc(wb.callback_url)}" target="_blank" rel="noopener">↗ 返回质擎智评工作台${wb.contract_id ? "（验收单 " + esc(wb.contract_id) + "）" : ""}</a></div>`
    : "";
  let html = wbBack + `
    <div class="dw-section">
      <h4>状态 / 时间</h4>
      <div class="readout">
        <div class="chip"><div class="c-label">状态</div><div class="c-value">${statusBadge(r.status)}</div></div>
        <div class="chip"><div class="c-label">开始时间</div><div class="c-value" style="font-size:14px">${fmtTime(r.start_time)}</div></div>
        <div class="chip"><div class="c-label">结束时间</div><div class="c-value" style="font-size:14px">${fmtTime(r.end_time)}</div></div>
      </div>
      <div id="run-live"></div>
    </div>
    <div class="dw-section">
      <h4>概要</h4>
      <div class="readout">
        <div class="chip"><div class="c-label">通过率</div><div class="c-value green">${fmtPct(r.pass_rate)}</div></div>
        <div class="chip"><div class="c-label">通过 / 题数</div><div class="c-value">${r.passed}/${r.total}</div></div>
        <div class="chip"><div class="c-label">inconclusive</div><div class="c-value amber">${r.inconclusive || 0}</div></div>
        <div class="chip"><div class="c-label">平均 pass@k</div><div class="c-value blue">${fmtPct(r.avg_pass_at_k || 0)}</div></div>
        <div class="chip"><div class="c-label">Judge 成本</div><div class="c-value">$${cost.toFixed(4)}</div></div>
      </div>
      <div class="readout" style="margin-top:10px">
        <div class="chip"><div class="c-label">harness 版本</div><div class="c-value" style="font-size:14px">${r.harness_version || "—"}</div></div>
        <div class="chip"><div class="c-label">模型版本钉</div><div class="c-value" style="font-size:14px">${r.model_version || "—"}</div></div>
        <div class="chip"><div class="c-label">数据集版本钉</div><div class="c-value" style="font-size:14px">${r.dataset_version || "—"}</div></div>
      </div>
    </div>`;

  if (tw && typeof tw.full_pass_rate === "number") {
    html += `
    <div class="dw-section">
      <h4>脚手架三通道解耦（创新③）</h4>
      <div class="dw-tw">
        <div class="tw"><div class="t">bare 裸模型</div><div class="v">${fmtPct(tw.bare_pass_rate)}</div></div>
        <div class="tw"><div class="t">harness 纯脚手架</div><div class="v">${fmtPct(tw.harness_pass_rate)}</div></div>
        <div class="tw"><div class="t">full 全系统</div><div class="v">${fmtPct(tw.full_pass_rate)}</div></div>
        <div class="tw gain"><div class="t">harness_only_gain</div><div class="v">+${(tw.harness_only_gain*100).toFixed(1)}pp</div></div>
        <div class="tw leak"><div class="t">scaffold_leakage</div><div class="v">${(tw.scaffold_leakage*100).toFixed(1)}pp</div></div>
        <div class="tw"><div class="t">样本数 n</div><div class="v">${tw.n}</div></div>
      </div>
    </div>`;
  }

  html += `
    <div class="dw-section">
      <h4>缺陷清单（R8） <button class="btn ghost xs" id="defects-btn" data-id="${r.id}">查看缺陷</button></h4>
      <div id="defects-body" class="muted">点击「查看缺陷」聚合失败 / 不可判用例。</div>
    </div>`;
  html += `<div class="dw-section"><h4>逐题结果（${r.cases.length}）</h4>`;
  for (const c of r.cases) {
    const gs = (c.graders || []).map(g => `
      <div class="g"><span class="gname">${g.grader}</span>
        <span class="tag ${g.passed ? "green" : "red"}">${g.passed ? "过" : "否"}</span>
        <span class="muted">${g.detail || ""}</span>
        ${g.grader === "judge" ? `<span class="muted">· $${(g.cost_usd||0).toFixed(4)}</span>` : ""}
        <span class="gscore">${ (g.score!=null? (g.score*100).toFixed(0)+"%" : "—") }</span></div>`).join("");
    html += `
      <div class="case-row" onclick="this.classList.toggle('open')">
        <div class="cr-head">
          ${passTag(c.passed)}
          <span class="cid">${c.case_id}</span>
          ${c.grader ? `<span class="tag gray">${c.grader}</span>` : ""}
          <span class="meta">${c.category||""} · ${fmtTime(0)}</span>
        </div>
        <div class="cr-detail">
          <div class="kv">
            <b>输入</b><span>${esc(c.input)}</span>
            <b>标准答案</b><span>${esc(c.gold)}</span>
            <b>实际回答</b><span>${esc(c.response)}</span>
            <b>k / 策略</b><span>${c.k} · ${r.trial_policy||"any"}</span>
            <b>耗时</b><span>${ (c.duration_ms||0).toFixed(1) } ms</span>
          </div>
          <div class="cr-graders">${gs || '<span class="muted">无评分器结果</span>'}</div>
        </div>
      </div>`;
  }
  html += `</div>`;
  $("#drawer-body").innerHTML = html;
  $("#drawer").classList.remove("hidden");
  const dbBtn = $("#defects-btn");
  if (dbBtn) dbBtn.onclick = () => loadDefects(r.id);
  if (r.status === "running") startRunLive(r.id);
}
async function loadDefects(id) {
  const el = $("#defects-body");
  el.innerHTML = `<span class="muted">加载中…</span>`;
  try {
    const d = await (await fetch("/api/runs/" + id + "/defects")).json();
    if (!d.defect_count) { el.innerHTML = `<span class="muted">无缺陷（全部通过 / 可判）。</span>`; return; }
    const byClass = Object.entries(d.by_class || {}).map(([k, v]) => `<span class="tag red">${esc(k)}: ${v}</span>`).join(" ");
    const byCat = Object.entries(d.by_category || {}).map(([k, v]) => `${esc(k)} ${v}`).join(" · ");
    const rows = (d.defects || []).slice(0, 200).map(c => `
      <div class="case-row">
        <div class="cr-head">${passTag(c.passed)} ${c.inconclusive ? '<span class="tag amber">不可判</span>' : ""}
          <span class="cid">${esc(c.case_id)}</span>
          <span class="tag gray">${esc(c.category || "")}</span>
          ${c.failure_class ? `<span class="tag red">${esc(c.failure_class)}</span>` : ""}
          ${c.status_code ? `<span class="muted">HTTP ${c.status_code}</span>` : ""}
        </div>
        <div class="cr-detail"><div class="kv">
          <b>输入</b><span>${esc(c.input)}</span>
          <b>实际回答</b><span>${esc(c.response)}</span>
          <b>错误</b><span>${esc(c.error)}</span>
        </div></div>
      </div>`).join("");
    el.innerHTML = `
      <div class="readout" style="margin-bottom:10px">
        <div class="chip"><div class="c-label">缺陷总数</div><div class="c-value red">${d.defect_count}/${d.total}</div></div>
      </div>
      <div style="margin-bottom:8px">按类型：${byClass || "—"}</div>
      <div class="muted" style="margin-bottom:8px">按分类：${byCat || "—"}</div>
      <div>${rows}</div>`;
  } catch (e) { el.innerHTML = `<span class="tag red">加载失败：${esc(e.message)}</span>`; }
}
let _runLiveTimer = null;
function startRunLive(id) {
  if (_runLiveTimer) clearInterval(_runLiveTimer);
  const tick = async () => {
    const el = $("#run-live");
    if (!el) { clearInterval(_runLiveTimer); _runLiveTimer = null; return; }
    const { jobs } = await (await fetch("/api/jobs")).json().catch(() => ({ jobs: [] }));
    const job = (jobs || []).find(j => j.run_id === id);
    if (job) {
      const last = job.last_case ? `<span class="muted" style="font-size:11px"> · ${esc(job.last_case.case_id || "")}</span>` : "";
      el.innerHTML = `<div class="mini-prog" style="margin-top:6px">${progressBar(job.done || 0, job.total || 0)}${last}</div>`;
    }
    const rd = await (await fetch("/api/runs/" + id)).json().catch(() => null);
    if (rd && rd.status !== "running") { clearInterval(_runLiveTimer); _runLiveTimer = null; }
  };
  _runLiveTimer = setInterval(tick, 1500);
  tick();
}
function esc(s) { return String(s == null ? "" : s).replace(/[&<>]/g, m => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[m])); }

// ---------------- 生产轨迹 ----------------
async function loadTraces() {
  const { traces } = await (await fetch("/api/traces?limit=200")).json();
  const body = $("#traces-body");
  if (!traces.length) { body.innerHTML = `<tr><td colspan="5" class="muted">暂无生产轨迹回灌。</td></tr>`; return; }
  body.innerHTML = traces.map(t => {
    const steps = (t.payload && t.payload.resourceSpans) ? t.payload.resourceSpans.flatMap(rs => rs.scopeSpans.flatMap(ss => ss.spans)).length : 0;
    return `<tr data-id="${t.id}"><td>${t.id}</td><td>${t.source}</td><td>${steps}</td><td class="muted">${fmtTime(t.created_at)}</td><td><button class="btn ghost" onclick="openTrace(${t.id})">查看 ↗</button></td></tr>`;
  }).join("");
  $$("#traces-body tr").forEach(tr => tr.onclick = e => { if (e.target.tagName !== "BUTTON") openTrace(tr.dataset.id); });
}
async function openTrace(id) {
  const d = await (await fetch("/api/traces/" + id)).json();
  const res = d.result;
  const steps = (d.expected_steps || []).map(s => `<span class="tag blue">${esc(s)}</span>`).join(" ");
  const html = `
    <div class="dw-section"><h4>来源 / 概览</h4>
      <div class="readout">
        <div class="chip"><div class="c-label">来源</div><div class="c-value" style="font-size:14px">${esc(d.source)}</div></div>
        <div class="chip"><div class="c-label">轨迹评分</div><div class="c-value ${res.passed ? "green" : "red"}">${(res.score*100).toFixed(0)}%</div></div>
        <div class="chip"><div class="c-label">归因结论</div><div class="c-value ${res.passed ? "green" : "red"}">${res.passed ? "通过" : "未过"}</div></div>
      </div>
    </div>
    <div class="dw-section"><h4>期望步骤（创新② 轨迹归因）</h4><div>${steps}</div></div>
    <div class="dw-section"><h4>执行轨迹文本</h4><pre style="white-space:pre-wrap;font-size:12.5px;background:#fafafa;border:1px solid var(--line);border-radius:12px;padding:12px;margin:0">${esc(d.trajectory_text || "")}</pre></div>
    <div class="dw-section"><h4>归因明细</h4><div class="muted">${esc(res.detail || "")}</div></div>`;
  $("#drawer-title").textContent = "生产轨迹 #" + id;
  $("#drawer-body").innerHTML = html;
  $("#drawer").classList.remove("hidden");
}

// ---------------- 数据集（目录管理：分类 / 元数据 / 增删改 / 体检） ----------------
let DS = null;   // /api/datasets 缓存（条目 + 受控分类体系 + 评分器候选 + fixtures）
const dsState = { tier: "", q: "", runnable: true, commercial: false, nojudge: false, envonly: false, groupby: "domain" };
// 该 hide：表格按受控域分组时，组头要显示域名与说明
let DS_DOMAIN_DESC = {};

function fmtTok(n) {
  if (n == null) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "k";
  return String(n);
}
function dsAll() {
  if (!DS) return [];
  const out = [];
  (DS.tier_order || []).forEach(t => {
    const g = DS.tiers && DS.tiers[t];
    if (g) (g.datasets || []).forEach(d => out.push(d));
  });
  return out;
}
function dsById(id) { return dsAll().find(d => d.id === id) || null; }

function dsFiltered() {
  const q = dsState.q.trim().toLowerCase();
  return dsAll().filter(d => {
    if (dsState.tier && d.tier !== dsState.tier) return false;
    if (dsState.envonly) { if (!d.requires_env) return false; }
    else if (dsState.runnable && !d.runnable) return false;
    if (dsState.commercial && !d.commercial_use) return false;
    if (dsState.nojudge && (d.recommends_judge || (d.graders || []).includes("judge"))) return false;
    if (q) {
      // 受控分类与能力标签也进检索面——评测人员常按「隐私」「工具调用」这类能力找集
      const hay = [d.id, d.name, d.category, d.scenario, d.source, d.description, d.license,
                   d.tier_label, d.domain_label, d.suite_file,
                   (d.capabilities || []).join(" "), (d.capability_labels || []).join(" ")]
        .filter(Boolean).join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

// 表格按受控域分组；域的顺序与说明来自后端 taxonomy（不在前端硬编）
function dsGrouped(list) {
  const by = dsState.groupby;
  if (by === "none") return [{ key: "", label: "", desc: "", items: list }];
  const meta = {};
  if (by === "domain") {
    (DS.domains || []).forEach(dm => { meta[dm.key] = { label: dm.label, desc: dm.desc, order: (DS.domain_order || []).indexOf(dm.key) }; });
  } else {
    (DS.tier_order || []).forEach((t, i) => { meta[t] = { label: (DS.tier_labels || {})[t] || t, desc: "", order: i }; });
  }
  const buckets = new Map();
  list.forEach(d => {
    const k = by === "domain" ? (d.domain || "other") : (d.tier || "other");
    if (!buckets.has(k)) buckets.set(k, []);
    buckets.get(k).push(d);
  });
  return Array.from(buckets.entries())
    .map(([key, items]) => ({ key, items, ...(meta[key] || { label: key, desc: "", order: 999 }) }))
    .sort((a, b) => (a.order - b.order) || a.label.localeCompare(b.label, "zh"));
}

async function loadDatasets(force) {
  try {
    if (!DS || force) DS = await (await fetch("/api/datasets")).json();
  } catch (e) { toast("数据集目录加载失败：" + e.message); return; }
  const s = DS.summary || {};
  $("#ds-hint").textContent = `收录 ${s.total} 条 · 可运行 ${s.ready} · 需环境 ${s.env} · 待拉取 ${s.planned}`;
  DS_DOMAIN_DESC = {};
  (DS.domains || []).forEach(d => { DS_DOMAIN_DESC[d.key] = d.desc || ""; });
  renderDsKpi(); renderDsTabs(); renderDsTable(); renderDsFixtures();
}

function renderDsKpi() {
  const all = dsAll();
  const s = DS.summary || {};
  const comm = all.filter(d => d.commercial_use).length;
  const nc = all.filter(d => d.commercial_use === false).length;
  const pinned = all.filter(d => d.pinned).length;
  const cases = all.filter(d => d.runnable).reduce((a, d) => a + (d.cases || 0), 0);
  const unclassified = all.filter(d => !d.domain || d.domain === "other").length;
  const domains = (DS.domains || []).filter(x => x.count > 0);
  $("#ds-kpi").innerHTML = [
    ["收录数据集", s.total ?? all.length, `分 ${(DS.tier_order || []).length} 档 · 受控分类 ${DS.taxonomy_version || "—"}`],
    ["可运行", s.ready ?? 0, `合计 ${cases} 条用例`],
    ["需环境（占位）", s.env ?? 0, "仅元数据登记，未接环境"],
    ["分类域", domains.length, unclassified ? `${unclassified} 条未分类（待补 domain）` : "全部已归入受控域"],
    ["可商用 / 非商用", `${comm} / ${nc}`, "非商用须法务确认"],
    ["已版本钉", pinned, pinned ? "已记录 sha256" : "尚未钉（无法证明同一份题）"],
  ].map(([l, v, sub]) => `
    <div class="kpi"><div class="k-label">${l}</div><div class="k-value">${v}</div><div class="k-sub">${sub}</div></div>
  `).join("");
}

function renderDsTabs() {
  const all = dsAll();
  const tabs = [["", "全部", all.length]];
  (DS.tier_order || []).forEach(t => tabs.push([t, (DS.tiers[t] || {}).label || t, all.filter(d => d.tier === t).length]));
  $("#ds-tabs").innerHTML = tabs.map(([v, label, n]) =>
    `<button class="ds-tab${dsState.tier === v ? " active" : ""}" data-tier="${v}">${esc(label)}<span class="n">${n}</span></button>`).join("");
  $$("#ds-tabs .ds-tab").forEach(b => b.onclick = () => {
    dsState.tier = b.dataset.tier; renderDsTabs(); renderDsGrid();
  });
}

function dsRowTags(d) {
  // 档位与分类已有独立列，这里只放状态/风险标签，避免表格里标签堆叠看不清
  const t = [d.runnable
    ? `<span class="tag green">可运行</span>`
    : `<span class="tag gray">${d.requires_env ? "需环境" : "未就位"}</span>`];
  if (d.commercial_use === false) t.push(`<span class="tag red">非商用</span>`);
  if (d.recommends_judge) t.push(`<span class="tag amber">需 judge</span>`);
  if (d.pinned) t.push(`<span class="tag gray">已钉</span>`);
  return t.join(" ");
}

function renderDsTable() {
  const list = dsFiltered();
  $("#ds-count").textContent = `命中 ${list.length} / ${dsAll().length} 条`;
  const body = $("#ds-body");
  if (!list.length) {
    body.innerHTML = `<tr><td colspan="11" class="muted ds-empty-cell">没有符合条件的数据集，放宽筛选或清空搜索。</td></tr>`;
    return;
  }
  let html = "";
  dsGrouped(list).forEach(g => {
    if (g.key) {
      html += `<tr class="ds-group-row"><td colspan="11">
        <span class="ds-g-label">${esc(g.label)}</span>
        <span class="ds-g-count">${g.items.length} 条</span>
        ${g.desc ? `<span class="ds-g-desc">${esc(g.desc)}</span>` : ""}
      </td></tr>`;
    }
    html += g.items.map(dsRow).join("");
  });
  body.innerHTML = html;
  bindDsRowActions();
}

function dsRow(d) {
  const caps = (d.capability_labels || []).map(c => `<span class="tag cap">${esc(c)}</span>`).join("");
  const rawScen = String(d.scenario || d.description || "—");
  const scenFull = esc(rawScen);
  const scen = rawScen.length > 96 ? esc(rawScen.slice(0, 96)) + "…" : scenFull;
  return `<tr class="ds-row" data-id="${esc(d.id)}">
    <td class="ds-c-name">
      <div class="n">${esc(d.name)}</div>
      <div class="i mono">${esc(d.id)}${d.suite_file ? " · " + esc(d.suite_file) : ""}</div>
    </td>
    <td class="ds-c-cat">
      <span class="tag domain">${esc(d.domain_label || "未分类")}</span>
      <div class="caps">${caps || '<span class="muted">—</span>'}</div>
    </td>
    <td><span class="tag blue">${esc(d.tier_label || d.tier)}</span></td>
    <td>${dsRowTags(d)}</td>
    <td class="num">${d.cases}</td>
    <td class="wide"><span class="scen" title="${scenFull}">${scen}</span></td>
    <td class="num">${d.est_duration_min != null ? esc(d.est_duration_min) + " min" : "—"}</td>
    <td class="num">${esc(fmtTok(d.est_tokens))}</td>
    <td class="mono">${esc((d.graders || []).join(",") || "—")}</td>
    <td>${esc(d.license || "—")}</td>
    <td class="ops">
      <button class="btn ghost xs" data-op="preview" data-id="${esc(d.id)}"${d.runnable ? "" : ` disabled title="${d.requires_env ? "需执行环境，本机无用例文件" : "用例文件未就位"}"`}>预览</button>
      <button class="btn ghost xs" data-op="detail" data-id="${esc(d.id)}">详情</button>
      <button class="btn ghost xs" data-op="edit" data-id="${esc(d.id)}">编辑</button>
      <button class="btn ghost xs" data-op="cases" data-id="${esc(d.id)}">导入用例</button>
      <button class="btn ghost xs" data-op="use" data-id="${esc(d.id)}"${d.runnable ? "" : " disabled"}>发起评测</button>
      <button class="btn ghost xs danger" data-op="del" data-id="${esc(d.id)}">删除</button>
    </td>
  </tr>`;
}

// 行操作只在渲染后绑一次（用事件委托会被 innerHTML 重建冲掉）
function bindDsRowActions() {
  $$("#ds-body [data-op]").forEach(b => b.onclick = e => {
    e.stopPropagation();
    const id = b.dataset.id, op = b.dataset.op;
    if (op === "preview") openCasesPreview(id);
    else if (op === "detail") openDataset(id);
    else if (op === "edit") openDatasetEditor(id);
    else if (op === "cases") openCaseImporter(id);
    else if (op === "use") { $("#drawer").classList.add("hidden"); goNewEval(id); }
    else if (op === "del") deleteDatasetFlow(id);
  });
  $$("#ds-body tr.ds-row").forEach(tr => tr.onclick = e => {
    if (e.target.closest(".ops")) return;
    openDataset(tr.dataset.id);
  });
}

function renderDsFixtures() {
  const b = $("#ds-fixtures-body");
  const fs = DS.fixtures || [];
  if (!fs.length) { b.innerHTML = `<tr><td colspan="4" class="muted">examples/ 下暂无非 catalog 的 fixtures。</td></tr>`; return; }
  b.innerHTML = fs.map(f => `
    <tr><td><b>${esc(f.name)}</b></td><td>${f.cases}</td>
      <td class="muted mono">${esc(f.path)}</td>
      <td><button class="btn ghost ds-use" data-name="${esc(f.name)}">用它发起评测</button></td></tr>`).join("");
  $$("#ds-fixtures-body .ds-use").forEach(x => x.onclick = () => goNewEval(x.dataset.name));
}

// ---- 数据集：新建 / 编辑（同一套表单，id 仅新建可填） ----
function dsField(label, id, val, opts) {
  const o = opts || {};
  const attrs = [
    `id="${id}"`,
    `class="control${o.mono ? " mono" : ""}"`,
    o.placeholder ? `placeholder="${esc(o.placeholder)}"` : "",
    o.type ? `type="${o.type}"` : "",
    o.readonly ? "readonly" : "",
    o.step ? `step="${o.step}"` : "",
  ].filter(Boolean).join(" ");
  const v = val == null ? "" : String(val);
  const inp = o.area
    ? `<textarea ${attrs} rows="${o.rows || 3}">${esc(v)}</textarea>`
    : `<input ${attrs} value="${esc(v)}" />`;
  return `<div class="field${o.full ? " full" : ""}">
    <label>${esc(label)}${o.required ? ' <span class="req">*</span>' : ""}</label>${inp}
    ${o.hint ? `<span class="hint">${o.hint}</span>` : ""}</div>`;
}

function dsSelect(label, id, val, options, hint) {
  return `<div class="field"><label>${esc(label)}</label>
    <select id="${id}" class="control">${options.map(([v, t]) =>
      `<option value="${esc(v)}"${String(val || "") === String(v) ? " selected" : ""}>${esc(t)}</option>`).join("")}</select>
    ${hint ? `<span class="hint">${hint}</span>` : ""}</div>`;
}

function dsCapPicker(entry) {
  const on = new Set(entry.capabilities || []);
  return `<div class="dw-section">
    <h4>能力标签（二级分类 · 受控）<span class="hint"> 最多 4 个较清晰</span></h4>
    <div class="cap-picker" id="ds-f-caps">
      ${(DS.capabilities || []).map(c =>
        `<label class="cap-chip${on.has(c.key) ? " on" : ""}">
          <input type="checkbox" value="${esc(c.key)}"${on.has(c.key) ? " checked" : ""}/>${esc(c.label)}
        </label>`).join("")}
    </div>
  </div>`;
}

function openDatasetEditor(id) {
  const creating = !id;
  const e = creating ? {} : (dsById(id) || {});
  const tierOpts = (DS.tier_order || []).map(t => [t, `${(DS.tier_labels || {})[t] || t}（${t}）`]);
  const domOpts = [["", "（未分类）"]].concat((DS.domains || []).map(d => [d.key, d.label]));

  let html = "";
  html += `<div class="dw-section"><h4>基本</h4><div class="row">
    ${dsField("数据集 id", "df-id", creating ? "" : id,
      { required: true, readonly: !creating, mono: true,
        placeholder: "小写字母/数字 + - . _ ，如 my-safety-set",
        hint: creating ? "建后不可改（历史评测按 id 引用）" : "id 不可修改" })}
    ${dsField("名称", "df-name", e.name, { required: true, placeholder: "如：中文安全 Benchmark（进阶子集）" })}
  </div><div class="row">
    ${dsSelect("档位", "df-tier", e.tier, tierOpts, "决定「何时该选这一档」")}
    ${dsSelect("所属域（一级分类）", "df-domain", e.domain, domOpts, "界面按此分组")}
  </div></div>`;

  html += dsCapPicker(e);

  html += `<div class="dw-section"><h4>决策信息（表格里直接展示）</h4>
    ${dsField("适用场景", "df-scenario", e.scenario,
      { area: true, rows: 3, full: true, placeholder: "什么情况下该选这套？给谁看？解决什么判断？" })}
    <div class="row">
      ${dsField("估算耗时（分钟）", "df-duration", e.est_duration_min, { type: "number", step: "0.5" })}
      ${dsField("估算 token", "df-tokens", e.est_tokens, { type: "number", placeholder: "如 4000" })}
    </div>
    <div class="row">
      ${dsField("许可", "df-license", e.license, { placeholder: "MIT / Apache-2.0 / CC-BY-NC-4.0 / 自有 / 待确认" })}
      ${dsField("推荐评分器", "df-grader", e.recommended_grader,
        { mono: true, placeholder: "code(exact/contains) / sandbox / permission,attribution" })}
    </div>
    <div class="row">
      <div class="field"><label>可商用</label>
        <label class="check"><input type="checkbox" id="df-commercial"${e.commercial_use ? " checked" : ""}/> 允许商用（非商用须法务确认）</label></div>
      <div class="field"><label>需要 judge 模型</label>
        <label class="check"><input type="checkbox" id="df-judge"${e.recommends_judge ? " checked" : ""}/> 判分依赖模型判官</label></div>
    </div></div>`;

  html += `<div class="dw-section"><h4>内容与来源</h4>
    ${dsField("说明", "df-desc", e.description, { area: true, rows: 2, full: true })}
    <div class="row">
      ${dsField("来源", "df-source", e.source, { placeholder: "作者 · 论文/arXiv · 仓库" })}
      ${dsField("官方链接", "df-url", e.official_url, { placeholder: "https://…" })}
    </div>
    <div class="row">
      ${dsField("细分说明（category）", "df-category", e.category, { placeholder: "自由文本，仅作详情展示" })}
      ${dsField("语言", "df-langs", (e.languages || []).join(","), { placeholder: "zh,en" })}
    </div>
    <div class="row">
      ${dsField("版本", "df-version", e.version, { placeholder: "如 v1.0 (数据集本身 2023)" })}
      ${dsField("更新时间", "df-updated", e.updated, { placeholder: "如 2023-04" })}
    </div>
    <div class="row">
      ${dsField("数据格式", "df-format", e.data_format, { placeholder: "JSONL (harness Case, MCQ)" })}
      ${dsField("全量规模", "df-fullscale", e.full_scale, { placeholder: "如 约 8,000+ 题" })}
    </div></div>`;

  html += `<div class="dw-section"><h4>执行与环境</h4><div class="row">
    ${dsField("用例文件名（suite_file）", "df-suitefile", e.suite_file,
      { mono: true, placeholder: "留空则不指向文件（环境类占位集）" })}
    <div class="field"><label>需要执行环境</label>
      <label class="check"><input type="checkbox" id="df-reqenv"${e.requires_env ? " checked" : ""}/> 需沙箱/浏览器/VM（仅元数据登记）</label></div>
  </div>
    ${dsField("环境说明", "df-envnotes", e.env_notes, { area: true, rows: 2, full: true })}</div>`;

  if (creating) {
    html += `<div class="dw-section"><h4>首批用例（可选）</h4>
      ${dsField("粘贴 JSONL（一行一条用例）", "df-cases", "", { area: true, rows: 6, full: true,
        placeholder: '{"id":"C-001","suite":"my_set","grader":"code","input":"...","gold":"...","meta":{}}' })}
      <p class="hint">留空则只建元数据条目；建好后可在列表里用「导入用例」补齐。用例必填 id 与 input。</p></div>`;
  }

  html += `<details class="dw-more"><summary>高级字段</summary>
    ${dsField("接入说明（access）", "df-access", e.access, { area: true, rows: 2, full: true })}
    <div class="row">
      ${dsField("对齐对象（aligned_to）", "df-aligned", e.aligned_to)}
      ${dsField("来源类型（source_kind）", "df-kind", e.source_kind, { placeholder: "hf_dataset / git / internal / curated_sample" })}
    </div>
    <div class="row">
      ${dsField("用例数（登记值）", "df-casescount", e.cases_count, { type: "number", hint: "导入用例后会自动校准" })}
      ${dsField("对象轴（object_axes）", "df-axes", (e.object_axes || []).join(","), { placeholder: "逗号分隔，兼容字段" })}
    </div>
    ${dsField("样本说明（sample_note）", "df-samplenote", e.sample_note, { area: true, rows: 2, full: true })}
    <div class="row">
      ${dsField("样本构建日期", "df-samplebuilt", e.sample_built, { placeholder: "2026-09-18" })}
      ${dsField("能力轴（dimensions）", "df-dims", (e.dimensions || []).join(","), { placeholder: "逗号分隔，兼容字段" })}
    </div>
  </details>`;

  html += `<div class="dw-section"><div class="form-actions">
    <button class="btn primary" id="df-save">${creating ? "创建数据集" : "保存修改"}</button>
    <button class="btn ghost" id="df-cancel">取消</button>
    <span class="muted" id="df-status">${creating ? "新建后会写入 catalog.json（自动备份）" : "只提交有变化的字段"}</span>
  </div></div>`;

  $("#drawer-title").textContent = creating ? "新增数据集" : `编辑：${e.name || id}`;
  $("#drawer-body").innerHTML = html;
  $("#drawer").classList.remove("hidden");

  // 能力标签点击切换选中态（checkbox 视觉隐藏在 chip 里）
  $$("#ds-f-caps .cap-chip").forEach(lb => lb.onchange = () => lb.classList.toggle("on", lb.querySelector("input").checked));
  $("#df-cancel").onclick = () => { $("#drawer").classList.add("hidden"); };
  $("#df-save").onclick = () => saveDatasetEditor(creating ? null : id);
}

async function saveDatasetEditor(id) {
  const creating = !id;
  const g = k => { const el = $("#" + k); return el ? el.value.trim() : ""; };
  const ck = k => { const el = $("#" + k); return el ? el.checked : false; };
  const body = {
    name: g("df-name"), tier: g("df-tier"), domain: g("df-domain"),
    capabilities: $$("#ds-f-caps input:checked").map(i => i.value),
    scenario: g("df-scenario"), description: g("df-desc"), category: g("df-category"),
    est_duration_min: g("df-duration"), est_tokens: g("df-tokens"),
    license: g("df-license"), recommended_grader: g("df-grader"),
    commercial_use: ck("df-commercial"), recommends_judge: ck("df-judge"),
    source: g("df-source"), official_url: g("df-url"),
    languages: g("df-langs"), version: g("df-version"), updated: g("df-updated"),
    data_format: g("df-format"), full_scale: g("df-fullscale"),
    suite_file: g("df-suitefile"), requires_env: ck("df-reqenv"), env_notes: g("df-envnotes"),
    access: g("df-access"), aligned_to: g("df-aligned"), source_kind: g("df-kind"),
    cases_count: g("df-casescount"), object_axes: g("df-axes"), dimensions: g("df-dims"),
    sample_note: g("df-samplenote"), sample_built: g("df-samplebuilt"),
  };

  const btn = $("#df-save"), st = $("#df-status");
  btn.disabled = true; st.textContent = creating ? "创建中…" : "保存中…";
  try {
    let r, j;
    if (creating) {
      const cases = g("df-cases");
      body.id = g("df-id");
      body.suite_file = body.suite_file || undefined;
      r = await fetch("/api/datasets", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cases ? { ...body, cases: cases.split("\n").map(s => s.trim()).filter(Boolean) } : body),
      });
    } else {
      r = await fetch("/api/datasets/" + encodeURIComponent(id), {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
    }
    j = await r.json();
    if (!r.ok) throw new Error(j.detail || "写入失败");
    const n = creating ? 1 : Object.keys(j.changed || {}).length;
    toast(creating ? `已创建 ${j.id}` : (n ? `已更新 ${n} 个字段` : "无字段变化"));
    $("#drawer").classList.add("hidden");
    await loadDatasets(true);
  } catch (err) {
    st.textContent = ""; btn.disabled = false;
    toast("保存失败：" + err.message);
  }
}

// ---- 数据集：导入用例 ----
function openCaseImporter(id) {
  const d = dsById(id) || {};
  $("#drawer-title").textContent = `导入用例：${d.name || id}`;
  $("#drawer-body").innerHTML = `<div class="dw-section">
    <div class="ds-issue"><span class="who"><span class="tag gray">目标文件</span></span>
      <span class="what"><code>${esc(d.suite_file || id + ".jsonl")}</code> · 当前登记 <b>${d.cases ?? 0}</b> 条 · 导入后 catalog 的用例数会自动校准</span></div>
    <div class="field"><label>粘贴 JSONL（一行一条用例）<span class="req">*</span></label>
      <textarea id="dc-text" class="control mono" rows="12" placeholder='{"id":"C-001","suite":"my_set","grader":"code","input":"题干","gold":"答案","meta":{}}'></textarea>
      <span class="hint">每行一个 JSON 对象，必须含 <code>id</code> 与 <code>input</code>；id 不得重复。</span></div>
    <div class="field"><label>导入方式</label>
      <select id="dc-mode" class="control">
        <option value="replace">覆盖（整份替换原用例）</option>
        <option value="append">追加（保留原用例，id 冲突会报错）</option>
      </select></div>
    <div class="form-actions" style="margin-top:12px">
      <button class="btn primary" id="dc-go">导入</button>
      <button class="btn ghost" id="dc-cancel">取消</button>
      <span class="muted" id="dc-status"></span></div>
  </div>`;
  $("#drawer").classList.remove("hidden");
  $("#dc-cancel").onclick = () => $("#drawer").classList.add("hidden");
  $("#dc-go").onclick = async () => {
    const text = $("#dc-text").value, mode = $("#dc-mode").value;
    const btn = $("#dc-go"), st = $("#dc-status");
    btn.disabled = true; st.textContent = "导入中…";
    try {
      const r = await fetch(`/api/datasets/${encodeURIComponent(id)}/cases`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, mode }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || "导入失败");
      toast(`已${mode === "append" ? "追加" : "覆盖"}：现有 ${j.cases} 条用例`);
      $("#drawer").classList.add("hidden");
      await loadDatasets(true);
    } catch (err) { st.textContent = ""; btn.disabled = false; toast("导入失败：" + err.message); }
  };
}

// ---- 数据集：删除（要求输入 id 二次确认） ----
function deleteDatasetFlow(id) {
  const d = dsById(id) || {};
  $("#drawer-title").textContent = "删除数据集";
  $("#drawer-body").innerHTML = `<div class="dw-section">
    <div class="ds-issue err"><span class="who"><span class="tag red">不可逆</span></span>
      <span class="what">将从 <code>catalog.json</code> 移除 <b>${esc(d.name || id)}</b>（<code>${esc(id)}</code>）。${
        d.suite_file ? `样本文件 <code>${esc(d.suite_file)}</code> 默认移入 <code>datasets/.trash/</code>，不会直接删除。`
                     : "该集没有样本文件。"}</span></div>
    <div class="field"><label>输入数据集 id 以确认删除：<code>${esc(id)}</code></label>
      <input id="dd-confirm" class="control mono" placeholder="${esc(id)}" autocomplete="off" /></div>
    <label class="check"><input type="checkbox" id="dd-purge" checked /> 同时把样本文件移入回收目录（取消勾选则保留文件，只删登记）</label>
    <p class="hint">catalog 改动前会自动备份为 <code>catalog.json.bak.&lt;时间戳&gt;</code>；误删后可从备份与回收目录回滚。</p>
    <div class="form-actions" style="margin-top:14px">
      <button class="btn danger" id="dd-go" disabled>确认删除</button>
      <button class="btn ghost" id="dd-cancel">取消</button>
      <span class="muted" id="dd-status"></span></div>
  </div>`;
  $("#drawer").classList.remove("hidden");
  $("#dd-cancel").onclick = () => $("#drawer").classList.add("hidden");
  const inp = $("#dd-confirm"), go = $("#dd-go");
  inp.oninput = () => { go.disabled = inp.value.trim() !== id; };
  go.onclick = async () => {
    go.disabled = true; $("#dd-status").textContent = "删除中…";
    try {
      const r = await fetch(`/api/datasets/${encodeURIComponent(id)}?purge_file=${$("#dd-purge").checked}`, { method: "DELETE" });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || "删除失败");
      toast(`已删除 ${id}${j.file_moved_to ? "（文件已移入回收目录）" : ""}`);
      $("#drawer").classList.add("hidden");
      await loadDatasets(true);
    } catch (err) { $("#dd-status").textContent = ""; go.disabled = false; toast("删除失败：" + err.message); }
  };
}

async function openDataset(id, previewN) {
  const n = previewN || 3;
  let d;
  try {
    const r = await fetch(`/api/datasets/${encodeURIComponent(id)}?preview=${n}`);
    d = await r.json();
    if (!r.ok) throw new Error(d.detail || "加载失败");
  } catch (e) { toast("详情加载失败：" + e.message); return; }

  const ds = d.dataset, facts = d.facts, pin = d.pin;
  const lvlTag = { error: "red", warn: "amber", info: "gray" };

  let html = `
    <div class="dw-section">
      <h4>概要</h4>
      <div class="readout">
        <div class="chip"><div class="c-label">档位</div><div class="c-value" style="font-size:15px">${esc(ds.tier_label)}</div></div>
        <div class="chip"><div class="c-label">状态</div><div class="c-value ${ds.runnable ? "green" : "amber"}" style="font-size:15px">${esc(ds.status_label)}</div></div>
        <div class="chip"><div class="c-label">用例数</div><div class="c-value">${ds.cases}</div></div>
        <div class="chip"><div class="c-label">估算耗时</div><div class="c-value">${ds.est_duration_min ?? "—"}<span style="font-size:12px"> min</span></div></div>
        <div class="chip"><div class="c-label">估算 token</div><div class="c-value">${fmtTok(ds.est_tokens)}</div></div>
      </div>
      <div class="readout" style="margin-top:10px">
        <div class="chip"><div class="c-label">推荐评分器</div><div class="c-value mono" style="font-size:13px">${esc(ds.recommended_grader || "—")}</div></div>
        <div class="chip"><div class="c-label">清洗后（CLI 可传）</div><div class="c-value mono ${ds.grader_available ? "green" : "red"}" style="font-size:13px">${esc((ds.graders || []).join(",") || "—")}</div></div>
        <div class="chip"><div class="c-label">许可</div><div class="c-value" style="font-size:13px">${esc(ds.license || "—")}</div></div>
        <div class="chip"><div class="c-label">可商用</div><div class="c-value ${ds.commercial_use ? "green" : "red"}" style="font-size:13px">${ds.commercial_use ? "是" : "否（须法务确认）"}</div></div>
        <div class="chip"><div class="c-label">版本</div><div class="c-value" style="font-size:13px">${esc(ds.version || "—")}</div></div>
      </div>
    </div>`;

  if (d.blockers && d.blockers.length) {
    html += `<div class="dw-section"><h4>阻断项 / 待办（${d.blockers.length}）</h4>` +
      d.blockers.map(b => `<div class="ds-issue ${b.level === "error" ? "err" : b.level === "warn" ? "warn" : ""}">
        <span class="who"><span class="tag ${lvlTag[b.level]}">${b.level}</span> ${esc(b.field)}</span>
        <span class="what">${esc(b.message)}${b.detail ? `　<span class="muted">${esc(b.detail)}</span>` : ""}</span></div>`).join("") +
      `</div>`;
  } else {
    html += `<div class="dw-section"><h4>阻断项</h4><div class="muted">无。文件就位、条数一致、评分器已注册。</div></div>`;
  }

  html += `
    <div class="dw-section">
      <h4>文件事实（catalog 记录 vs 磁盘实际）</h4>
      <table class="tbl"><tbody>
        <tr><td class="muted">用例文件</td><td class="mono">${esc(ds.suite_file || "—")}</td></tr>
        <tr><td class="muted">磁盘路径</td><td class="mono">${esc(facts.file_path || "（未就位）")}</td></tr>
        <tr><td class="muted">文件字节</td><td>${facts.bytes != null ? facts.bytes.toLocaleString("zh-CN") + " B" : "—"}</td></tr>
        <tr><td class="muted">条数</td><td>catalog 记 <b>${facts.recorded_cases}</b> · 磁盘实际 <b>${facts.actual_cases}</b>
          ${facts.recorded_cases === facts.actual_cases ? '<span class="tag green">一致</span>' : '<span class="tag red">不一致</span>'}</td></tr>
        <tr><td class="muted">sha256</td><td class="mono" style="white-space:normal">${esc(facts.sha256 || "—")}</td></tr>
        <tr><td class="muted">修改时间</td><td>${facts.mtime ? new Date(facts.mtime * 1000).toLocaleString("zh-CN") : "—"}</td></tr>
      </tbody></table>
      <div class="ds-issue ${pin.match === false ? "warn" : ""}" style="margin-top:10px">
        <span class="who"><span class="tag ${pin.pinned ? (pin.match === false ? "red" : "green") : "gray"}">版本钉</span></span>
        <span class="what">${pin.pinned
          ? `已钉：version=<b>${esc(pin.version || "—")}</b> · fetched_at=${esc(pin.fetched_at || "—")} · 与当前文件 sha256 ${pin.match === false ? "<b>不一致（文件已被替换）</b>" : "一致"}`
          : `尚未版本钉（未记录 file_sha256）。当前 version 字段是手写的上游版本描述「${esc(pin.version || "—")}」，只能说明「哪一套题」，不能证明「哪一份文件」。`}</span>
      </div>
    </div>`;

  const canPv = !!facts.exists;
  html += `<div class="dw-section">
      <h4>用例内容</h4>
      <div class="pv-open-row">
        <button class="btn ghost" id="dw-pv-btn"${canPv ? "" : " disabled"}>⧉ 在线预览全部 ${ds.cases} 条</button>
        <span class="muted">${canPv
          ? "就地看题目内容：可搜索、可筛选、可翻页，不必先导出 JSONL。"
          : "用例文件未就位，暂无可预览内容。"}</span>
      </div>
      <div class="ds-filters" style="padding:0 0 10px;border:none;margin:0">
        <span class="hint" style="margin-right:2px">速览前</span>
        ${[3, 5, 10].map(k => `<button class="ds-tab${n === k ? " active" : ""}" data-pv="${k}">${k} 条</button>`).join("")}
      </div>`;
  for (const c of d.preview) {
    if (c.error) { html += `<div class="ds-issue err"><span class="who">第 ${c.line} 行</span><span class="what">${esc(c.error)}</span></div>`; continue; }
    html += `<div class="case-row" onclick="this.classList.toggle('open')">
      <div class="cr-head"><span class="cid">${esc(c.id || "（无 id）")}</span>
        ${c.suite ? `<span class="tag gray">${esc(c.suite)}</span>` : ""}
        ${c.grader ? `<span class="tag blue">${esc(c.grader)}</span>` : ""}
        <span class="meta">第 ${c.line} 行 · meta ${c.meta_keys.length} 字段</span></div>
      <div class="cr-detail"><div class="kv">
        <b>输入</b><span>${esc(c.input)}</span>
        <b>标准答案</b><span>${esc(c.gold)}</span>
        <b>meta 字段</b><span class="mono">${esc(c.meta_keys.join(", ") || "—")}</span>
      </div></div></div>`;
  }
  html += `</div>`;

  html += `<div class="dw-section"><h4>元数据</h4>
      <table class="tbl"><tbody>
        ${[["所属域（一级分类）", ds.domain_label],
           ["能力标签（二级分类）", (ds.capability_labels || []).join("、")],
           ["细分说明（category）", ds.category],
           ["应用场景", ds.scenario], ["说明", ds.description],
           ["全量规模", ds.full_scale], ["数据格式", ds.data_format], ["语言", (ds.languages || []).join(", ")],
           ["来源", ds.source], ["官方链接", ds.official_url], ["接入说明", ds.access],
           ["来源类型", ds.source_kind], ["样本说明", ds.sample_note], ["对齐对象", ds.aligned_to],
           ["构建日期", ds.sample_built], ["更新时间", ds.updated], ["环境要求", ds.env_notes]]
          .filter(([, v]) => v).map(([k, v]) => `<tr><td class="muted">${k}</td><td style="white-space:normal">${k === "官方链接" && v ? `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a>` : esc(v)}</td></tr>`).join("")}
      </tbody></table>
    </div>`;

  html += `<div class="dw-section"><h4>复现命令</h4>
      <div class="copy-row"><code>${esc(dsCli(ds))}</code>
        <button class="btn ghost" id="ds-copy-cli">复制</button></div>
      <p class="hint" style="margin-top:8px">mock 提供方零成本；接真实模型把 --provider mock 换成 deepseek / openai / local_agent 等。</p>
    </div>
    <div class="dw-section"><div class="form-actions">
      <button class="btn primary" id="ds-use-btn" ${ds.runnable ? "" : "disabled"}>▶ 用此数据集发起评测</button>
      <button class="btn ghost" id="ds-dl-btn" ${ds.runnable ? "" : "disabled"}>↓ 导出 JSONL</button>
      <button class="btn ghost" id="ds-edit-btn">✎ 编辑元数据</button>
      <button class="btn ghost" id="ds-cases-btn">⇪ 导入用例</button>
      <button class="btn ghost danger" id="ds-del-btn">🗑 删除</button>
      <span class="muted">${ds.runnable ? "" : "该集当前不可运行"}</span>
    </div></div>`;

  $("#drawer-title").textContent = ds.name;
  $("#drawer-body").innerHTML = html;
  $("#drawer").classList.remove("hidden");

  $$("#drawer-body [data-pv]").forEach(b => b.onclick = () => openDataset(id, +b.dataset.pv));
  const dwPv = $("#dw-pv-btn");
  if (dwPv && !dwPv.disabled) dwPv.onclick = () => openCasesPreview(id);
  const useBtn = $("#ds-use-btn");
  if (useBtn && !useBtn.disabled) useBtn.onclick = () => { $("#drawer").classList.add("hidden"); goNewEval(id); };
  const dlBtn = $("#ds-dl-btn");
  if (dlBtn && !dlBtn.disabled) dlBtn.onclick = () => window.open(`/api/datasets/${encodeURIComponent(id)}/download`, "_blank");
  const cp = $("#ds-copy-cli");
  if (cp) cp.onclick = () => copyText(dsCli(ds));
  const eb = $("#ds-edit-btn");
  if (eb) eb.onclick = () => openDatasetEditor(id);
  const cb = $("#ds-cases-btn");
  if (cb) cb.onclick = () => openCaseImporter(id);
  const delb = $("#ds-del-btn");
  if (delb) delb.onclick = () => deleteDatasetFlow(id);
}

function dsCli(ds) {
  const g = (ds.graders || []).join(",") || "code";
  let cmd = `python -m eval_harness ${ds.id} --provider mock --grader ${g}`;
  if ((ds.graders || []).includes("judge") || ds.recommends_judge) {
    cmd += ` --judge-provider mock --judge-provider-kwargs '{"mode":"judge"}'`;
  }
  return cmd;
}

function copyText(t) {
  const done = () => toast("已复制到剪贴板");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(t).then(done).catch(() => fallbackCopy(t, done));
  } else fallbackCopy(t, done);
}
function fallbackCopy(t, done) {
  const ta = document.createElement("textarea");
  ta.value = t; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); done(); } catch (e) { toast("复制失败，请手动选择"); }
  document.body.removeChild(ta);
}

// ---------------- 数据集用例在线预览（只读） ----------------
// 与「导出 JSONL」互补：导出是把整份文件拿走，这里是就地看内容。
// 服务端按行索引分页（app.py::_cases_page），所以搜索/翻页都不会重扫文件。
const PV = { id: null, data: null, sel: 0, page: 1, size: 20, q: "", f: {}, timer: null };

async function openCasesPreview(id) {
  PV.id = id; PV.page = 1; PV.sel = 0; PV.q = ""; PV.f = {};
  $("#pv-q").value = "";
  ["difficulty", "grader", "category"].forEach(k => { $("#pv-f-" + k).value = ""; });
  $("#pv").classList.remove("hidden");
  await pvLoad();
}

function closeCasesPreview() {
  $("#pv").classList.add("hidden");
  clearTimeout(PV.timer);
}

function pvQs() {
  const p = new URLSearchParams({ page: String(PV.page), page_size: String(PV.size) });
  if (PV.q) p.set("q", PV.q);
  Object.entries(PV.f).forEach(([k, v]) => { if (v) p.set(k, v); });
  return p.toString();
}

async function pvLoad() {
  if (!PV.id) return;
  $("#pv-detail").innerHTML = '<div class="muted">加载中…</div>';
  let d;
  try {
    const r = await fetch(`/api/datasets/${encodeURIComponent(PV.id)}/cases?${pvQs()}`);
    d = await r.json();
    if (!r.ok) throw new Error(d.detail || "加载失败");
  } catch (e) {
    toast("用例预览加载失败：" + e.message);
    $("#pv-detail").innerHTML = `<div class="ds-issue err"><span class="who">加载失败</span><span class="what">${esc(e.message)}</span></div>`;
    return;
  }
  PV.data = d; PV.page = d.page;
  if (PV.sel >= (d.cases || []).length) PV.sel = 0;
  pvRenderHead(); pvRenderFilters(); pvRenderList(); pvRenderDetail();
}

function pvRenderHead() {
  const d = PV.data, ds = d.dataset, f = d.file;
  $("#pv-name").textContent = ds.name || ds.id;
  $("#pv-sub").innerHTML = [
    `<span class="mono">${esc(ds.id)}</span>`,
    `<span class="tag domain">${esc(ds.domain_label || "未分类")}</span>`,
    `<span class="tag blue">${esc(ds.tier_label || ds.tier)}</span>`,
    `<span class="muted">${f.lines} 条 · 有效 ${f.valid}${f.bad ? ` · <b class="red">坏行 ${f.bad}</b>` : ""} · ${(f.bytes / 1024).toFixed(1)} KB · ${f.mtime ? new Date(f.mtime * 1000).toLocaleString("zh-CN") : "—"}</span>`,
  ].join(" ");
  // 用真实路径推断归属目录：集文件也可能在 examples/ 下，不能一律写成 datasets/
  $("#pv-path").textContent = String(d.path || "").replace(/^.*\/(datasets|examples)\//, "$1/") || d.path;
  $("#pv-path").title = d.path;
  const use = $("#pv-use");
  use.disabled = !ds.runnable;
  use.onclick = () => { closeCasesPreview(); $("#drawer").classList.add("hidden"); goNewEval(ds.id); };
}

function pvRenderFilters() {
  const F = PV.data.facets || {};
  [["difficulty", "难度"], ["grader", "评分器"], ["category", "分类"]].forEach(([k, label]) => {
    const sel = $("#pv-f-" + k), cur = PV.f[k] || "";
    const opts = Object.entries(F[k] || {});
    sel.innerHTML = `<option value="">全部${label}</option>` + opts.map(([v, n]) =>
      `<option value="${esc(v)}"${v === cur ? " selected" : ""}>${esc(v)}（${n}）</option>`).join("");
    sel.disabled = !opts.length;
  });
  const d = PV.data;
  $("#pv-count").textContent = d.total ? `命中 ${d.total} / ${d.file.valid} 条` : "无命中";
  $("#pv-page").textContent = `${d.page} / ${d.pages}`;
  $("#pv-prev").disabled = d.page <= 1;
  $("#pv-next").disabled = d.page >= d.pages;
}

function pvRenderList() {
  const cs = PV.data.cases || [], box = $("#pv-list");
  if (!cs.length) {
    const filtered = PV.q || Object.values(PV.f).some(Boolean);
    box.innerHTML = `<div class="pv-empty muted">${filtered
      ? "没有命中的用例——换个关键词，或点「重置」清空筛选。"
      : "该文件没有可显示的用例。"}</div>`;
    return;
  }
  const start = (PV.data.page - 1) * PV.data.page_size;
  box.innerHTML = cs.map((c, i) => {
    const d = c.data || {};
    const txt = c.error ? c.error : String(d.input || "").replace(/\s+/g, " ").slice(0, 110);
    return `<div class="pv-item${i === PV.sel ? " active" : ""}" data-i="${i}">
      <div class="pvi-top">
        <span class="mono pvi-id">${esc(c.error ? "坏行" : (d.id || "（无 id）"))}</span>
        <span class="pvi-no">#${start + i + 1}</span>
      </div>
      <div class="pvi-txt">${esc(txt)}</div>
      <div class="pvi-tags">
        ${d.difficulty ? `<span class="tag gray">${esc(d.difficulty)}</span>` : ""}
        ${d.grader ? `<span class="tag blue">${esc(d.grader)}</span>` : ""}
      </div>
    </div>`;
  }).join("");
  $$("#pv-list .pv-item").forEach(el => el.onclick = () => pvSelect(+el.dataset.i));
}

function pvSelect(i) {
  PV.sel = i;
  $$("#pv-list .pv-item").forEach(el => {
    const on = +el.dataset.i === i;
    el.classList.toggle("active", on);
    if (on) el.scrollIntoView({ block: "nearest" });
  });
  pvRenderDetail();
}

function pvStep(d) {
  if (!PV.data) return;
  const n = (PV.data.cases || []).length;
  const next = PV.sel + d;
  if (next < 0) {
    if (PV.data.page > 1) { PV.page = PV.data.page - 1; PV.sel = 0; pvLoad().then(() => pvSelect(Math.max(0, (PV.data.cases || []).length - 1))); }
    return;
  }
  if (next >= n) {
    if (PV.data.page < PV.data.pages) { PV.page = PV.data.page + 1; PV.sel = 0; pvLoad(); }
    return;
  }
  pvSelect(next);
}

function pvMetaHtml(v) {
  if (Array.isArray(v)) {
    if (!v.length) return '<span class="muted">[]</span>';
    return `<div class="pv-arr">${v.map(x =>
      `<span class="tag cap">${esc(typeof x === "object" ? JSON.stringify(x) : x)}</span>`).join("")}</div>`;
  }
  if (v && typeof v === "object") {
    return `<div class="pv-nest">${Object.entries(v).map(([k, x]) =>
      `<div class="pv-nest-row"><b class="mono">${esc(k)}</b><div>${pvMetaHtml(x)}</div></div>`).join("")}</div>`;
  }
  if (v === true) return '<span class="tag green">true</span>';
  if (v === false) return '<span class="tag red">false</span>';
  if (v == null) return '<span class="muted">null</span>';
  return `<span class="pv-scalar">${esc(v)}</span>`;
}

function pvRenderDetail() {
  const c = (PV.data.cases || [])[PV.sel], box = $("#pv-detail");
  if (!c) { box.innerHTML = '<div class="muted">没有选中条目。</div>'; return; }
  if (c.error) {
    box.innerHTML = `<div class="ds-issue err"><span class="who">第 ${c.line} 行</span>
        <span class="what">${esc(c.error)}</span></div>
      <pre class="pv-raw">${esc(c.raw_text || "")}</pre>`;
    return;
  }
  const d = c.data || {};
  const blk = (title, val, cls) => (val == null || val === "") ? "" :
    `<div class="pvd-blk"><h5>${title}</h5><div class="pvd-text ${cls || ""}">${esc(val)}</div></div>`;
  let html = `<div class="pvd-head">
      <span class="pvd-id mono">${esc(d.id || "（无 id）")}</span>
      <span class="muted">第 ${c.line} 行</span>
      ${d.difficulty ? `<span class="tag gray">难度 ${esc(d.difficulty)}</span>` : ""}
      ${d.grader ? `<span class="tag blue">评分器 ${esc(d.grader)}</span>` : ""}
      ${d.suite ? `<span class="tag cap">suite ${esc(d.suite)}</span>` : ""}
      ${d.category && d.category !== d.suite ? `<span class="tag cap">${esc(d.category)}</span>` : ""}
    </div>`;
  html += blk("题目 / 输入（input）", d.input, "pre");
  html += blk("标准答案（gold）", d.gold, "pre");
  if (d.meta && Object.keys(d.meta).length) {
    html += `<div class="pvd-blk"><h5>meta（评分器判据）</h5><div class="pv-meta">${
      Object.entries(d.meta).map(([k, v]) =>
        `<div class="pv-meta-row"><b class="mono">${esc(k)}</b><div>${pvMetaHtml(v)}</div></div>`).join("")}</div></div>`;
  }
  const rest = Object.keys(d).filter(k => !["id", "input", "gold", "meta"].includes(k));
  if (rest.length) {
    html += `<div class="pvd-blk"><h5>其他字段</h5><div class="pv-meta">${
      rest.map(k => `<div class="pv-meta-row"><b class="mono">${esc(k)}</b><div>${pvMetaHtml(d[k])}</div></div>`).join("")}</div></div>`;
  }
  html += `<div class="pvd-blk"><h5>原始 JSON（本条）</h5>
      <div class="pv-rawbar">
        <button class="btn ghost xs" id="pv-copy">复制本条 JSON</button>
        <button class="btn ghost xs" id="pv-copy-in">只复制 input</button>
        <span class="muted">${(c.truncated || []).length
          ? "已截断字段：" + esc(c.truncated.join("、"))
          : "完整未截断"}</span>
      </div>
      <pre class="pv-raw">${esc(JSON.stringify(d, null, 2))}</pre></div>`;
  box.innerHTML = html;
  box.scrollTop = 0;
  const cp = $("#pv-copy"); if (cp) cp.onclick = () => copyText(JSON.stringify(d, null, 2));
  const cpi = $("#pv-copy-in"); if (cpi) cpi.onclick = () => copyText(String(d.input || ""));
}

async function validateDatasets() {
  const btn = $("#ds-validate-btn");
  btn.disabled = true; btn.textContent = "◈ 体检中…";
  try {
    const r = await fetch("/api/datasets/validate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deep: true, include_pin_status: true }),
    });
    renderValidate(await r.json());
  } catch (e) { toast("体检失败：" + e.message); }
  btn.disabled = false; btn.textContent = "◈ 体检目录";
}

function renderValidate(d) {
  $("#ds-validate-card").classList.remove("hidden");
  $("#ds-validate-hint").textContent = `逐行解析 ${d.checked} 条 · 健康 ${d.clean} 条 · 待版本钉 ${d.pin_pending ?? 0} 条`;
  $("#ds-validate-summary").innerHTML = [
    ["检查条数", d.checked, "含环境占位", ""],
    ["健康", d.clean, "无错误 / 无警告", "green"],
    ["错误", d.counts.error, "阻断运行", d.counts.error ? "red" : ""],
    ["警告", d.counts.warn, "需关注", d.counts.warn ? "amber" : ""],
    ["待版本钉", d.pin_pending ?? 0, "未记录 sha256", (d.pin_pending ?? 0) ? "amber" : "green"],
  ].map(([l, v, sub, cls]) =>
    `<div class="chip"><div class="c-label">${l}</div><div class="c-value ${cls}">${v}</div><div class="c-label">${sub}</div></div>`).join("");

  const groups = [["error", "错误（阻断运行）"], ["warn", "警告（需关注）"], ["info", "提示"]];
  const body = groups.map(([lv, title]) => {
    const xs = d.issues.filter(i => i.level === lv);
    if (!xs.length) return "";
    return `<div class="ds-group-title">${title} · ${xs.length}</div>` + xs.map(i =>
      `<div class="ds-issue ${lv === "error" ? "err" : lv === "warn" ? "warn" : ""}">
        <span class="who">${esc(i.id)}</span>
        <span class="what"><span class="tag gray">${esc(i.field)}</span> ${esc(i.message)}</span>
      </div>`).join("");
  }).join("");
  $("#ds-validate-body").innerHTML = body || `<span class="muted">未发现任何问题。</span>`;
}

async function goNewEval(ref) {
  switchView("new");
  await loadNew();
  if (ref) { newSel = new Set([ref]); buildDatasetPicker(); }
  toast(ref ? "已带入数据集：" + ref : "已切到新建评测");
}

// ---------------- 新建评测（多选数据集 → 批量拆跑） ----------------
let newSel = new Set();   // 当前勾选的数据集 id

async function loadNew() {
  const data = await (await fetch("/api/datasets")).json();
  DS = data;
  fillGraderDatalist();
  buildDatasetPicker();
  // 全选可运行 / 清空
  $("#ds-pick-all").onclick = () => { dsAll().filter(d => d.runnable).forEach(d => newSel.add(d.id)); buildDatasetPicker(); };
  $("#ds-pick-clear").onclick = () => { newSel.clear(); buildDatasetPicker(); };
  // 切 trials 时刷新成本预估
  const tEl = $("#f-trials"); if (tEl) tEl.oninput = updatePickSummary;
  refreshJobs();
  await populateProviderSelect();
  await populateJudgeSelect();
  $("#f-provider-manage").onclick = () => switchView("providers");
  $("#f-judge-manage").onclick = () => switchView("judges");
  if (!refreshTimer) refreshTimer = setInterval(() => {
    if ($("#view-new").classList.contains("hidden") === false) refreshJobs();
    if (curBatchId && $("#view-batch").classList.contains("hidden") === false) renderBatchLive();
  }, 2500);
}

function fillGraderDatalist() {
  const dl = $("#f-grader-list");
  if (!dl) return;
  const ch = DS.grader_choices || [];
  const core = ch.filter(c => c.group === "core");
  const auto = ch.filter(c => c.group === "autoeval");
  dl.innerHTML =
    core.map(c => `<option value="${esc(c.name)}">${esc(c.label || "")}</option>`).join("") +
    auto.map(c => `<option value="${esc(c.name)}">AutoEval · ${esc(c.label || "")}</option>`).join("");
}

// 多选数据集选择器：按受控域分组；可运行集可勾选，不可运行集置灰并标注原因
function buildDatasetPicker() {
  const box = $("#ds-picker");
  const groups = dsGrouped(dsAll());
  box.innerHTML = groups.map(g => `
    <div class="ds-pick-group">
      <div class="ds-pick-ghead">${esc(g.label)} <span class="muted">${esc(g.desc || "")}</span> <span class="muted">(${g.items.length})</span></div>
      ${g.items.map(pickerRow).join("")}
    </div>`).join("");
  box.querySelectorAll("input.ds-pick-cb").forEach(cb => {
    cb.onchange = () => { cb.checked ? newSel.add(cb.value) : newSel.delete(cb.value); updatePickSummary(); };
  });
  updatePickSummary();
}

function pickerRow(d) {
  const disabled = !d.runnable;
  const checked = newSel.has(d.id) ? "checked" : "";
  const reason = disabled
    ? `<span class="tag gray">${d.requires_env ? "需环境" : "未就位"}</span>`
    : "";
  return `<label class="ds-pick-row${disabled ? " disabled" : ""}">
    <input type="checkbox" class="ds-pick-cb" value="${esc(d.id)}" ${checked} ${disabled ? "disabled" : ""}/>
    <span class="ds-pick-name">${esc(d.name)}</span>
    ${d.tier_label ? `<span class="tag blue">${esc(d.tier_label)}</span>` : ""}
    <span class="muted">${d.cases} 题 · 约 ${d.est_duration_min ?? "—"} min · ${fmtTok(d.est_tokens)} tok</span>
    ${d.recommends_judge ? '<span class="tag amber">建议 judge</span>' : ""}
    ${reason}
  </label>`;
}

function updatePickSummary() {
  const picked = dsAll().filter(d => newSel.has(d.id));
  const cases = picked.reduce((s, d) => s + (d.cases || 0), 0);
  const mins = picked.reduce((s, d) => s + (d.est_duration_min || 0), 0);
  const toks = picked.reduce((s, d) => s + (d.est_tokens || 0), 0);
  $("#ds-pick-summary").innerHTML = picked.length
    ? `已选 <b>${picked.length}</b> 个数据集 · <b>${cases}</b> 题 · 约 <b>${mins}</b> min · <b>${fmtTok(toks)}</b> tok（trials=${$("#f-trials").value} 时再乘）`
    : "未选择";
  const btn = $("#run-btn");
  if (btn) btn.textContent = picked.length ? `▶ 启动批量评测（${picked.length} 个数据集）` : "▶ 启动批量评测";
}

async function refreshJobs() {
  const { jobs } = await (await fetch("/api/jobs")).json();
  const b = $("#jobs-body");
  if (!jobs.length) { b.innerHTML = `<span class="muted">暂无在途任务。</span>`; return; }
  b.innerHTML = jobs.map(j => {
    const st = j.status === "running" ? '<span class="tag amber">运行中</span>' : j.status === "done" ? '<span class="tag green">完成</span>' : '<span class="tag red">失败</span>';
    const prog = (j.status === "running" && j.total) ? progressBar(j.done || 0, j.total) : "";
    const link = j.run_id ? `<a class="lnk" onclick="openRun('${esc(j.run_id)}')">查看运行 ›</a>` : "";
    return `<div class="chip"><div class="c-label">${esc(j.suite)}</div><div class="c-value" style="font-size:14px">${st}</div>${prog}${link}${j.error ? `<div class="muted" style="font-size:11px">${esc(j.error)}</div>` : ""}</div>`;
  }).join("");
}
$("#new-eval-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ids = [...document.querySelectorAll("#ds-picker input.ds-pick-cb:checked")].map(c => c.value);
  if (!ids.length) { toast("请至少选择一个可运行的数据集"); return; }
  const body = {
    datasets: ids,
    provider: $("#f-provider").value,
    provider_mode: $("#f-mode").value,
    judge_provider: $("#f-judge").value,
    grader: $("#f-grader").value.trim(),   // 留空 = 各数据集用各自推荐评分器
    trials: +$("#f-trials").value,
    trial_policy: $("#f-policy").value,
    three_way: $("#f-threeway").checked,
    model_version: $("#f-model").value,
    dataset_version: $("#f-dataset").value.trim(),  // 留空 = 各数据集用各自版本钉
    run_name_prefix: "批量",
    wb_callback_url: $("#f-wb-callback").value.trim(),
    wb_contract_id: $("#f-wb-contract").value.trim(),
  };
  $("#run-btn").disabled = true;
  $("#run-status").textContent = `启动 ${ids.length} 个数据集…`;
  const r = await (await fetch("/api/runs/batch", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
  $("#run-btn").disabled = false;
  $("#run-status").textContent = "";
  if (r.status === "launched") {
    toast(`已启动 ${r.created} 个数据集评测（跳过 ${r.skipped.length}）`);
    goBatch(r.batch_id);
  } else {
    toast("启动失败：" + (r.message || JSON.stringify(r)));
  }
});

// ---------------- 批量评测覆盖矩阵 ----------------
let curBatchId = null;
async function goBatch(batchId) {
  curBatchId = batchId;
  switchView("batch");
  await renderBatch(batchId);
}
async function renderBatch(batchId) {
  const d = await (await fetch(`/api/runs/batch/${batchId}`)).json();
  renderBatchDom(d);
}
function renderBatchLive() { if (curBatchId) renderBatch(curBatchId); }

function renderBatchDom(d) {
  const s = d.summary || {};
  const st = (k) => k === "done" ? '<span class="tag green">完成</span>'
    : k === "running" ? '<span class="tag amber">运行中</span>'
    : '<span class="tag red">失败</span>';
  const overall = s.overall_pass_rate == null ? "—" : (s.overall_pass_rate * 100).toFixed(1) + "%";
  $("#batch-summary").innerHTML = `
    <div class="kpi-grid">
      <div class="kpi"><div class="k-label">批次</div><div class="k-value" style="font-size:13px">${esc(d.batch_id)}</div></div>
      <div class="kpi"><div class="k-label">已选/完成/运行/失败</div><div class="k-value">${s.selected} / ${s.done} / ${s.running} / ${s.failed}</div></div>
      <div class="kpi"><div class="k-label">总题数</div><div class="k-value">${s.total_cases}</div></div>
      <div class="kpi"><div class="k-label">综合通过率（已完成加权）</div><div class="k-value">${overall}</div></div>
    </div>
    <div class="batch-progress"><div class="batch-progress-bar" id="batch-progress" style="width:${s.selected ? Math.round(s.done / s.selected * 100) : 0}%"></div></div>`;

  const mrows = (d.matrix || []).map(m => {
    const pr = m.domain_pass_rate == null ? "—" : (m.domain_pass_rate * 100).toFixed(1) + "%";
    return `<tr><td>${esc(m.domain)}</td><td>${m.count}</td><td>${m.cases}</td><td>${m.passed}</td><td><b>${pr}</b></td></tr>`;
  }).join("");
  $("#batch-matrix").innerHTML = mrows || `<tr><td colspan="5" class="muted">无</td></tr>`;

  const irows = (d.items || []).map(it => {
    const pr = it.pass_rate == null ? "—" : (it.pass_rate * 100).toFixed(1) + "%";
    const pk = it.avg_pass_at_k == null ? "—" : (it.avg_pass_at_k * 100).toFixed(1) + "%";
    return `<tr>
      <td><b>${esc(it.name || it.dataset_id)}</b></td>
      <td class="mono">${esc(it.dataset_id)}</td>
      <td>${esc(it.grader || "")}</td>
      <td>${esc(it.dataset_version || "")}</td>
      <td>${st(it.status)}${it.error ? `<div class="muted" style="font-size:11px">${esc(it.error)}</div>` : ""}</td>
      <td>${it.total ?? "—"}</td><td>${pr}</td><td>${pk}</td>
      <td>${it.run_id ? `<a class="lnk" data-rid="${esc(it.run_id)}">看运行</a>` : ""}</td>
    </tr>`;
  }).join("");
  $("#batch-items").innerHTML = irows;
  $("#batch-items").querySelectorAll("a[data-rid]").forEach(a => a.onclick = () => openRun(a.dataset.rid));
}

// ---------------- 自定义看板 (N7) ----------------
function widgetTemplate() {
  return `
  <div class="dw-widget" style="border:1px solid var(--line);border-radius:14px;padding:12px;margin-top:10px">
    <div class="row">
      <div class="field"><label>类型</label>
        <select class="control w-type">
          <option value="kpi">指标卡 KPI</option>
          <option value="barchart">柱状图</option>
          <option value="linechart">折线图</option>
          <option value="doughnut">环形图</option>
          <option value="sql">SQL 表</option>
        </select></div>
      <div class="field"><label>标题</label><input class="control w-title" placeholder="组件标题" /></div>
    </div>
    <div class="field" style="margin-top:8px"><label>指标 / SQL</label>
      <input class="control w-metric" placeholder="kpi: avg_pass_rate | chart/sql: SELECT … FROM runs" /></div>
    <div class="field" style="margin-top:8px"><label>过滤(JSON，可选)</label>
      <input class="control w-filter" placeholder='{"provider":"mock"}' /></div>
    <button class="btn ghost w-del" style="margin-top:8px">移除</button>
  </div>`;
}
function bindWidgetDel() { $$("#dash-widgets .w-del").forEach(b => b.onclick = () => b.closest(".dw-widget").remove()); }

async function loadDashboards() {
  $("#dashboards-list").parentElement.classList.remove("hidden");
  $("#dash-builder").classList.add("hidden");
  $("#dash-render-card").classList.add("hidden");
  const { dashboards } = await (await fetch("/api/dashboards")).json();
  const list = $("#dashboards-list");
  if (!dashboards.length) { list.innerHTML = `<div class="muted">暂无看板，点「＋ 新建看板」创建。</div>`; return; }
  list.innerHTML = dashboards.map(d => `
    <div class="kpi" style="cursor:pointer" data-id="${d.id}">
      <div class="k-label">${esc(d.name)}</div>
      <div class="k-value" style="font-size:18px">${d.widgets} 个组件</div>
      <div class="k-sub"><a class="dash-open">打开</a> · <a class="dash-del" style="color:var(--red)">删除</a></div>
    </div>`).join("");
  $$("#dashboards-list .kpi").forEach(el => el.onclick = () => openDashboard(el.dataset.id));
  $$("#dashboards-list .dash-del").forEach(a => a.onclick = async e => {
    e.stopPropagation();
    await fetch("/api/dashboards/" + a.closest(".kpi").dataset.id, { method: "DELETE" });
    toast("已删除"); loadDashboards();
  });
}

async function openDashboard(id) {
  const d = await (await fetch("/api/dashboards/" + id + "/render")).json();
  $("#dashboards-list").parentElement.classList.add("hidden");
  $("#dash-builder").classList.add("hidden");
  $("#dash-render-card").classList.remove("hidden");
  $("#dash-render-title").textContent = d.name;
  const kpis = [], charts = [], sql = [];
  d.widgets.forEach(w => {
    if (w.type === "kpi" && !w.error) kpis.push(w);
    else if (w.type === "sql") sql.push(w);
    else if (!w.error) charts.push(w);
  });
  $("#dash-kpis").innerHTML = kpis.map(w =>
    `<div class="kpi"><div class="k-label">${esc(w.title)}</div><div class="k-value">${w.value}${w.suffix || ""}</div></div>`).join("");
  $("#dash-charts").innerHTML = charts.map((w, i) =>
    `<div class="card chart-card"><div class="card-head"><h3>${esc(w.title)}</h3></div><canvas id="dash-chart-${i}" height="220"></canvas></div>`).join("");
  charts.forEach((w, i) => drawChart("dash-chart-" + i,
    w.type === "doughnut" ? "doughnut" : w.type === "linechart" ? "line" : "bar", {
      labels: w.labels,
      datasets: [{ label: w.title, data: w.values,
        backgroundColor: w.type === "doughnut" ? ["#0071e3", "#5e5ce6", "#34c759", "#ff9f0a", "#ff3b30", "#8e8e93"] : "#0071e3",
        borderColor: "#0071e3", fill: w.type !== "doughnut" }],
    }, w.type === "doughnut" ? { plugins: { legend: { position: "right" } } } : {}));
  if (sql.length) {
    $("#dash-sql-wrap").classList.remove("hidden");
    const c = sql[0].columns;
    $("#dash-sql-table").innerHTML = `<thead><tr>${c.map(x => `<th>${x}</th>`).join("")}</tr></thead><tbody>${sql[0].rows.map(r => `<tr>${c.map(x => `<td>${esc(r[x])}</td>`).join("")}</tr>`).join("")}</tbody>`;
  } else $("#dash-sql-wrap").classList.add("hidden");
}

$("#dash-new-btn").onclick = () => {
  $("#dashboards-list").parentElement.classList.add("hidden");
  $("#dash-builder").classList.remove("hidden");
  $("#dash-name").value = "";
  $("#dash-widgets").innerHTML = widgetTemplate();
  bindWidgetDel();
};
$("#dash-add-widget").onclick = () => { const w = document.createElement("div"); w.innerHTML = widgetTemplate(); $("#dash-widgets").appendChild(w); bindWidgetDel(); };
$("#dash-cancel-btn").onclick = () => loadDashboards();
$("#dash-back-btn").onclick = () => loadDashboards();
$("#dash-save-btn").onclick = async () => {
  const name = $("#dash-name").value.trim();
  if (!name) { $("#dash-status").textContent = "请填看板名称"; return; }
  const widgets = $$("#dash-widgets .dw-widget").map(el => {
    const type = $(".w-type", el).value;
    const title = $(".w-title", el).value;
    const metric = $(".w-metric", el).value.trim();
    let filter = {}; try { filter = JSON.parse($(".w-filter", el).value || "{}"); } catch (e) { filter = {}; }
    const w = { type, title };
    if (type === "kpi") { w.metric = metric || "avg_pass_rate"; w.filter = filter; }
    else { w.sql = metric; if (type !== "sql") w.chart_type = type === "barchart" ? "bar" : type === "linechart" ? "line" : "doughnut"; }
    return w;
  });
  $("#dash-status").textContent = "保存中…";
  const r = await (await fetch("/api/dashboards", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, config: { widgets } }) })).json();
  $("#dash-status").textContent = "";
  toast("看板已保存");
  openDashboard(r.id);
};

// ---------------- 抽屉 / 全局 ----------------
$("#drawer-close").onclick = () => $("#drawer").classList.add("hidden");
$("#drawer-mask").onclick = () => $("#drawer").classList.add("hidden");
$("#refresh-btn").onclick = () => { const v = $(".nav-item.active").dataset.view; switchView(v); toast("已刷新"); };
$$(".nav-item").forEach(b => b.onclick = () => switchView(b.dataset.view));
$("#guide-callout-btn").onclick = () => switchView("guide");

// ---------------- 提供方 / Judge / 汇总报告（R5 / R6 / R2） ----------------
async function populateProviderSelect() {
  const { providers } = await (await fetch("/api/providers")).json().catch(() => ({ providers: [] }));
  const sel = $("#f-provider"); if (!sel) return;
  sel.innerHTML = (providers || []).map(p => {
    const label = p.builtin ? `${esc(p.display_name)}（内置）` : `${esc(p.display_name || p.name)}（自定义）`;
    return `<option value="${esc(p.name)}">${label}</option>`;
  }).join("") || `<option value="mock">mock</option>`;
  if ([...sel.options].some(o => o.value === "mock")) sel.value = "mock";
}
async function populateJudgeSelect() {
  const { judges } = await (await fetch("/api/judges")).json().catch(() => ({ judges: [] }));
  const sel = $("#f-judge"); if (!sel) return;
  const opts = ['<option value="">不启用</option>'].concat((judges || []).map(j => {
    const label = j.builtin ? `${esc(j.display_name)}（内置）` : `${esc(j.display_name || j.name)}（自定义）`;
    return `<option value="${esc(j.name)}">${label}</option>`;
  }));
  sel.innerHTML = opts.join("");
}

async function renderProviders() {
  const { providers, builtins } = await (await fetch("/api/providers")).json().catch(() => ({ providers: [], builtins: [] }));
  const kindSel = $("#pv-kind");
  if (kindSel) kindSel.innerHTML = (builtins || []).map(b => `<option value="${esc(b.name)}">${esc(b.display_name)}（${esc(b.name)}）</option>`).join("");
  const body = $("#pv-body"); if (!body) return;
  body.innerHTML = (providers || []).map(p => `
    <tr>
      <td><b>${esc(p.name)}</b></td>
      <td>${esc(p.display_name || "")}</td>
      <td><span class="tag gray">${esc(p.kind)}</span>${p.builtin ? ' <span class="tag blue">内置</span>' : ''}</td>
      <td>${esc(p.model || "—")}</td>
      <td>${p.api_key_set ? '<span class="tag green">已配置</span>' : '<span class="muted">未设</span>'}</td>
      <td>${p.builtin ? '<span class="muted">—</span>' : `<button class="btn ghost xs" onclick="editProvider('${esc(p.name)}')">编辑</button> <button class="btn ghost xs" onclick="deleteProvider('${esc(p.name)}')">删除</button>`}</td>
    </tr>`).join("") || `<tr><td colspan="6" class="muted">无</td></tr>`;
}
window.editProvider = function (name) {
  fetch("/api/providers").then(r => r.json()).then(d => {
    const p = (d.providers || []).find(x => x.name === name);
    if (!p) return;
    $("#pv-name").value = p.name; $("#pv-name").disabled = true;
    $("#pv-display").value = p.display_name || "";
    $("#pv-kind").value = p.kind;
    $("#pv-model").value = p.model || "";
    $("#pv-base").value = p.base_url || "";
    $("#pv-key").value = "";
    $("#pv-extra").value = p.extra ? JSON.stringify(p.extra) : "";
    $("#pv-form-wrap").classList.remove("hidden");
    $("#pv-status").textContent = "";
  });
};
window.deleteProvider = async function (name) {
  if (!confirm("确认删除提供方 " + name + "？")) return;
  const r = await (await fetch("/api/providers/" + encodeURIComponent(name), { method: "DELETE" })).json().catch(() => ({ ok: false }));
  toast(r.ok ? "已删除 " + name : "删除失败");
  renderProviders(); populateProviderSelect();
};

async function renderJudges() {
  const { judges, builtins } = await (await fetch("/api/judges")).json().catch(() => ({ judges: [], builtins: [] }));
  const kindSel = $("#jg-kind");
  if (kindSel) kindSel.innerHTML = (builtins || []).map(b => `<option value="${esc(b.name)}">${esc(b.display_name)}（${esc(b.name)}）</option>`).join("");
  const body = $("#jg-body"); if (!body) return;
  body.innerHTML = (judges || []).map(p => `
    <tr>
      <td><b>${esc(p.name)}</b></td>
      <td>${esc(p.display_name || "")}</td>
      <td><span class="tag gray">${esc(p.kind)}</span>${p.builtin ? ' <span class="tag blue">内置</span>' : ''}</td>
      <td>${esc(p.model || "—")}</td>
      <td>${p.api_key_set ? '<span class="tag green">已配置</span>' : '<span class="muted">未设</span>'}</td>
      <td>${p.builtin ? '<span class="muted">—</span>' : `<button class="btn ghost xs" onclick="editJudge('${esc(p.name)}')">编辑</button> <button class="btn ghost xs" onclick="deleteJudge('${esc(p.name)}')">删除</button>`}</td>
    </tr>`).join("") || `<tr><td colspan="6" class="muted">无</td></tr>`;
}
window.editJudge = function (name) {
  fetch("/api/judges").then(r => r.json()).then(d => {
    const p = (d.judges || []).find(x => x.name === name);
    if (!p) return;
    $("#jg-name").value = p.name; $("#jg-name").disabled = true;
    $("#jg-display").value = p.display_name || "";
    $("#jg-kind").value = p.kind;
    $("#jg-model").value = p.model || "";
    $("#jg-base").value = p.base_url || "";
    $("#jg-key").value = "";
    $("#jg-extra").value = p.extra ? JSON.stringify(p.extra) : "";
    $("#jg-form-wrap").classList.remove("hidden");
    $("#jg-status").textContent = "";
  });
};
window.deleteJudge = async function (name) {
  if (!confirm("确认删除 Judge 配置 " + name + "？")) return;
  const r = await (await fetch("/api/judges/" + encodeURIComponent(name), { method: "DELETE" })).json().catch(() => ({ ok: false }));
  toast(r.ok ? "已删除 " + name : "删除失败");
  renderJudges(); populateJudgeSelect();
};

async function renderReport() {
  const wrap = $("#report-body"); if (!wrap) return;
  const sel = [...(window._selectedRuns || [])];
  if (!sel.length) { wrap.innerHTML = `<span class="muted">在「评测运行」勾选若干运行，点「生成汇总报告」；或进入某批次后由批量方案自动聚合。</span>`; return; }
  wrap.innerHTML = `<span class="muted">已选 ${sel.length} 个运行，点右上「生成报告」。</span>`;
}
async function genReport() {
  const sel = [...(window._selectedRuns || [])];
  if (!sel.length) { toast("请先在「评测运行」勾选运行"); return; }
  const wrap = $("#report-body");
  wrap.innerHTML = `<span class="muted">生成中…</span>`;
  const d = await (await fetch("/api/runs/summary", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run_ids: sel }) })).json().catch(() => ({ runs: [] }));
  if (!d.count) { wrap.innerHTML = `<span class="tag red">无可汇总运行</span>`; return; }
  const s = d.summary || {};
  const overall = s.overall_pass_rate == null ? "—" : (s.overall_pass_rate * 100).toFixed(1) + "%";
  const mrows = (d.matrix || []).map(m => {
    const pr = m.pass_rate == null ? "—" : (m.pass_rate * 100).toFixed(1) + "%";
    return `<tr><td><span class="tag gray">${esc(m.provider)}</span></td><td>${m.runs}</td><td>${m.cases}</td><td>${m.passed}</td><td><b>${pr}</b></td></tr>`;
  }).join("");
  const rrows = (d.runs || []).map(r => {
    const pr = r.pass_rate == null ? "—" : (r.pass_rate * 100).toFixed(1) + "%";
    return `<tr data-id="${r.id}"><td><b>${esc(r.name)}</b></td><td><span class="tag gray">${esc(r.provider || "")}</span></td>
      <td>${statusBadge(r.status)}</td><td>${r.total}</td><td>${r.passed}</td><td><b>${pr}</b></td>
      <td>${fmtTime(r.start_time)}</td><td>${fmtTime(r.end_time)}</td></tr>`;
  }).join("");
  wrap.innerHTML = `
    <div class="kpi-grid">
      <div class="kpi"><div class="k-label">运行数</div><div class="k-value">${d.count}</div></div>
      <div class="kpi"><div class="k-label">总题数</div><div class="k-value">${s.total_cases}</div></div>
      <div class="kpi"><div class="k-label">综合通过率</div><div class="k-value">${overall}</div></div>
      <div class="kpi"><div class="k-label">不可判</div><div class="k-value">${s.inconclusive}</div></div>
    </div>
    <div class="card"><div class="card-head"><h3>按提供方聚合</h3></div>
      <div class="table-wrap"><table class="tbl"><thead><tr><th>提供方</th><th>运行数</th><th>题数</th><th>通过</th><th>通过率</th></tr></thead><tbody>${mrows || '<tr><td colspan="5" class="muted">无</td></tr>'}</tbody></table></div>
    </div>
    <div class="card"><div class="card-head"><h3>运行明细</h3></div>
      <div class="table-wrap"><table class="tbl"><thead><tr><th>名称</th><th>提供方</th><th>状态</th><th>题数</th><th>通过</th><th>通过率</th><th>开始</th><th>结束</th></tr></thead><tbody>${rrows}</tbody></table></div>
    </div>`;
  wrap.querySelectorAll("tr[data-id]").forEach(tr => tr.onclick = () => openRun(tr.dataset.id));
}

// 管理视图事件绑定（只绑一次）
$("#pv-new-btn").onclick = () => { $("#pv-name").disabled = false; $("#pv-name").value = ""; $("#pv-display").value = ""; $("#pv-model").value = ""; $("#pv-base").value = ""; $("#pv-key").value = ""; $("#pv-extra").value = ""; $("#pv-form-wrap").classList.remove("hidden"); $("#pv-status").textContent = ""; };
$("#pv-cancel-btn").onclick = () => $("#pv-form-wrap").classList.add("hidden");
$("#pv-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("#pv-name").value.trim();
  if (!name) { $("#pv-status").textContent = "标识必填"; return; }
  let extra = {};
  const ex = $("#pv-extra").value.trim();
  if (ex) { try { extra = JSON.parse(ex); } catch (_) { $("#pv-status").textContent = "extra 不是合法 JSON"; return; } }
  const body = { name, display_name: $("#pv-display").value.trim() || name, kind: $("#pv-kind").value,
    base_url: $("#pv-base").value.trim(), api_key: $("#pv-key").value, model: $("#pv-model").value.trim(), extra };
  const r = await (await fetch("/api/providers", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json().catch(() => ({ ok: false }));
  if (r.ok) { toast("已保存 " + name); $("#pv-form-wrap").classList.add("hidden"); $("#pv-name").disabled = false; renderProviders(); populateProviderSelect(); }
  else $("#pv-status").textContent = "保存失败：" + JSON.stringify(r);
});
$("#jg-new-btn").onclick = () => { $("#jg-name").disabled = false; $("#jg-name").value = ""; $("#jg-display").value = ""; $("#jg-model").value = ""; $("#jg-base").value = ""; $("#jg-key").value = ""; $("#jg-extra").value = ""; $("#jg-form-wrap").classList.remove("hidden"); $("#jg-status").textContent = ""; };
$("#jg-cancel-btn").onclick = () => $("#jg-form-wrap").classList.add("hidden");
$("#jg-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("#jg-name").value.trim();
  if (!name) { $("#jg-status").textContent = "标识必填"; return; }
  let extra = {};
  const ex = $("#jg-extra").value.trim();
  if (ex) { try { extra = JSON.parse(ex); } catch (_) { $("#jg-status").textContent = "extra 不是合法 JSON"; return; } }
  const body = { name, display_name: $("#jg-display").value.trim() || name, kind: $("#jg-kind").value,
    base_url: $("#jg-base").value.trim(), api_key: $("#jg-key").value, model: $("#jg-model").value.trim(), extra };
  const r = await (await fetch("/api/judges", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json().catch(() => ({ ok: false }));
  if (r.ok) { toast("已保存 " + name); $("#jg-form-wrap").classList.add("hidden"); $("#jg-name").disabled = false; renderJudges(); populateJudgeSelect(); }
  else $("#jg-status").textContent = "保存失败：" + JSON.stringify(r);
});
$("#run-report-btn").onclick = () => { switchView("report"); genReport(); };
$("#runs-selall").onchange = (e) => {
  window._selectedRuns = window._selectedRuns || new Set();
  if (e.target.checked) (window._runs || []).forEach(r => window._selectedRuns.add(r.id));
  else window._selectedRuns.clear();
  renderRuns($("#run-search").value);
};

// 数据集页筛选项（只绑一次，避免重复渲染时叠加监听）
$("#ds-search").oninput = e => { dsState.q = e.target.value; renderDsTable(); };
$("#ds-groupby").onchange = e => { dsState.groupby = e.target.value; renderDsTable(); };
$("#ds-f-runnable").onchange = e => {
  dsState.runnable = e.target.checked;
  if (dsState.runnable) { dsState.envonly = false; $("#ds-f-env").checked = false; }
  renderDsTable();
};
$("#ds-f-env").onchange = e => {
  dsState.envonly = e.target.checked;
  if (dsState.envonly) { dsState.runnable = false; $("#ds-f-runnable").checked = false; }
  renderDsTable();
};
$("#ds-f-commercial").onchange = e => { dsState.commercial = e.target.checked; renderDsTable(); };
$("#ds-f-nojudge").onchange = e => { dsState.nojudge = e.target.checked; renderDsTable(); };
$("#ds-validate-btn").onclick = validateDatasets;
$("#ds-validate-close").onclick = () => $("#ds-validate-card").classList.add("hidden");
$("#ds-new-btn").onclick = () => openDatasetEditor(null);

// 用例在线预览器（只绑一次）
$("#pv-close").onclick = closeCasesPreview;
$("#pv-mask").onclick = closeCasesPreview;
$("#pv-reset").onclick = () => {
  PV.q = ""; PV.f = {}; PV.page = 1; PV.sel = 0;
  $("#pv-q").value = "";
  ["difficulty", "grader", "category"].forEach(k => { $("#pv-f-" + k).value = ""; });
  pvLoad();
};
$("#pv-q").oninput = e => {
  const v = e.target.value;
  clearTimeout(PV.timer);
  PV.timer = setTimeout(() => { PV.q = v.trim(); PV.page = 1; PV.sel = 0; pvLoad(); }, 280);
};
["difficulty", "grader", "category"].forEach(k => {
  $("#pv-f-" + k).onchange = e => { PV.f[k] = e.target.value; PV.page = 1; PV.sel = 0; pvLoad(); };
});
$("#pv-size").onchange = e => { PV.size = +e.target.value; PV.page = 1; PV.sel = 0; pvLoad(); };
$("#pv-prev").onclick = () => { if (PV.data && PV.data.page > 1) { PV.page = PV.data.page - 1; PV.sel = 0; pvLoad(); } };
$("#pv-next").onclick = () => { if (PV.data && PV.data.page < PV.data.pages) { PV.page = PV.data.page + 1; PV.sel = 0; pvLoad(); } };

document.addEventListener("keydown", e => {
  if ($("#pv").classList.contains("hidden")) return;
  const t = e.target;
  const typing = t && ["INPUT", "SELECT", "TEXTAREA"].includes(t.tagName);
  if (e.key === "Escape") { if (typing) t.blur(); else closeCasesPreview(); return; }
  if (typing) return;
  if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); pvStep(1); }
  else if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); pvStep(-1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); $("#pv-next").click(); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); $("#pv-prev").click(); }
});

// 启动
(async function init() {
  const h = await (await fetch("/api/health")).json();
  $("#backend-badge").textContent = h.backend === "postgresql" ? "PostgreSQL" : "SQLite";
  $("#ver-badge").textContent = "v" + h.harness_version;
  const v = location.hash.slice(1);
  switchView(TITLES[v] ? v : "dashboard");
})();

// hash 变化（手工改地址栏 / 前进后退）时跟随切换；switchView 用 replaceState 写 hash，不会回环
window.addEventListener("hashchange", () => {
  const v = location.hash.slice(1);
  const cur = $(".nav-item.active");
  if (TITLES[v] && (!cur || cur.dataset.view !== v)) switchView(v);
});
