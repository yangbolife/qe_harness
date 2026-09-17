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

// ---------------- 导航 ----------------
const TITLES = {
  dashboard: ["概览", "交付验收全景"],
  runs: ["评测运行", "历次评测与明细"],
  traces: ["生产轨迹", "创新⑥ · 上线真实轨迹复评"],
  casesets: ["用例集", "创新④ · 数据集版本钉"],
  dashboards: ["自定义看板", "N7 · 保存你的指标视图"],
  new: ["新建评测", "复用引擎发起一次评测"],
};
function switchView(v) {
  $$(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.view === v));
  $$(".view").forEach(s => s.classList.add("hidden"));
  $("#view-" + v).classList.remove("hidden");
  $("#view-title").textContent = TITLES[v][0];
  $("#view-sub").textContent = TITLES[v][1];
  if (v === "dashboard") loadDashboard();
  else if (v === "runs") loadRuns();
  else if (v === "traces") loadTraces();
  else if (v === "casesets") loadCaseSets();
  else if (v === "dashboards") loadDashboards();
  else if (v === "new") loadNew();
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
async function loadRuns() {
  const { runs } = await (await fetch("/api/runs?limit=500")).json();
  window._runs = runs;
  renderRuns("");
}
function renderRuns(q) {
  const runs = window._runs || [];
  const f = q.trim().toLowerCase();
  const body = $("#runs-body");
  const list = runs.filter(r =>
    !f || [r.name, r.model_version, r.provider, r.judge_provider].filter(Boolean).join(" ").toLowerCase().includes(f));
  if (!list.length) { body.innerHTML = `<tr><td colspan="8" class="muted">暂无运行记录。</td></tr>`; return; }
  body.innerHTML = list.map(r => `
    <tr data-id="${r.id}">
      <td><b>${r.name}</b></td>
      <td><span class="tag gray">${r.provider}</span></td>
      <td>${r.model_version || "—"}</td>
      <td>${rateBar(r.pass_rate)}</td>
      <td>${r.total}</td>
      <td>${r.inconclusive || 0}</td>
      <td>${r.three_way_json ? '<span class="tag blue">有</span>' : '<span class="muted">—</span>'}</td>
      <td class="muted">${fmtTime(r.created_at)}</td>
    </tr>`).join("");
  $$("#runs-body tr").forEach(tr => tr.onclick = () => openRun(tr.dataset.id));
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

// ---------------- 用例集 ----------------
async function loadCaseSets() {
  const { case_sets } = await (await fetch("/api/case_sets")).json();
  const body = $("#cs-body");
  if (!case_sets.length) { body.innerHTML = `<tr><td colspan="4" class="muted">暂无用例集版本记录（发起评测时可写入版本钉）。</td></tr>`; return; }
  body.innerHTML = case_sets.map(c => `
    <tr><td><b>${c.name}</b></td><td><span class="tag blue">${c.version}</span></td><td class="muted">${c.hash || "—"}</td><td class="muted">${c.source_path || "—"}</td></tr>`).join("");
}

// ---------------- 新建评测 ----------------
async function loadNew() {
  const { datasets } = await (await fetch("/api/datasets")).json();
  const sel = $("#f-suite");
  sel.innerHTML = datasets.map(d => `<option value="${d.name}">${d.name}（${d.cases} 题）</option>`).join("");
  refreshJobs();
  if (!refreshTimer) refreshTimer = setInterval(() => { if ($("#view-new").classList.contains("hidden") === false) refreshJobs(); }, 2500);
}
async function refreshJobs() {
  const { jobs } = await (await fetch("/api/jobs")).json();
  const b = $("#jobs-body");
  if (!jobs.length) { b.innerHTML = `<span class="muted">暂无在途任务。</span>`; return; }
  b.innerHTML = jobs.map(j => {
    const st = j.status === "running" ? '<span class="tag amber">运行中</span>' : j.status === "done" ? '<span class="tag green">完成</span>' : '<span class="tag red">失败</span>';
    return `<div class="chip"><div class="c-label">${esc(j.suite)}</div><div class="c-value" style="font-size:14px">${st}</div>${j.error ? `<div class="muted" style="font-size:11px">${esc(j.error)}</div>` : ""}</div>`;
  }).join("");
}
$("#new-eval-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    suite: $("#f-suite").value,
    provider: $("#f-provider").value,
    provider_mode: $("#f-mode").value,
    judge_provider: $("#f-judge").value,
    grader: $("#f-grader").value,
    trials: +$("#f-trials").value,
    trial_policy: $("#f-policy").value,
    three_way: $("#f-threeway").checked,
    model_version: $("#f-model").value,
    dataset_version: $("#f-dataset").value,
    run_name: "",
    wb_callback_url: $("#f-wb-callback").value.trim(),
    wb_contract_id: $("#f-wb-contract").value.trim(),
  };
  $("#run-btn").disabled = true;
  $("#run-status").textContent = "启动中…";
  const r = await (await fetch("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
  $("#run-btn").disabled = false;
  $("#run-status").textContent = "";
  toast(r.status === "launched" ? "评测已启动，后台执行中" : "启动失败");
  refreshJobs();
});

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

// 启动
(async function init() {
  const h = await (await fetch("/api/health")).json();
  $("#backend-badge").textContent = h.backend === "postgresql" ? "PostgreSQL" : "SQLite";
  $("#ver-badge").textContent = "v" + h.harness_version;
  switchView("dashboard");
})();
