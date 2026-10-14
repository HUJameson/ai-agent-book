/* Context Ablation GUI 前端：fetch + 轮询 + Chart.js 渲染（见 SPEC 第 6 节）。 */

const $ = (id) => document.getElementById(id);

const MODE_META = {
  full: { icon: "✨", name: "完整上下文", desc: "基线：历史 + 思考 + 工具全部在场" },
  no_history: { icon: "🕳️", name: "无历史消息", desc: "每轮失忆，倾向重复同一动作" },
  no_reasoning: { icon: "🙈", name: "无思考过程", desc: "看不到推理链，缺少规划" },
  no_tool_calls: { icon: "🚫", name: "无工具定义", desc: "不能调用工具，只能凭记忆" },
  no_tool_results: { icon: "🛑", name: "无工具结果", desc: "执行了工具但对结果失明" },
};

const GROUNDING_LABEL = {
  grounded: { text: "数字来自任务", cls: "ok" },
  ungrounded: { text: "⚠ UNSUPPORTED 数字", cls: "fail" },
  not_assessable: { text: "有工具观察（不可判）", cls: "warn" },
  no_quantities: { text: "答案无数值", cls: "" },
  no_answer: { text: "无答案", cls: "fail" },
};

const CELL_OUTCOME = {
  ok: { mark: "✓", cls: "done-ok", mcls: "m-ok" },
  warn: { mark: "⚠", cls: "done-warn", mcls: "m-warn" },
  fail: { mark: "✗", cls: "done-fail", mcls: "m-fail" },
};

let charts = [];
let polling = null;
let selectedMode = "full";
let ablationModes = new Set(Object.keys(MODE_META));
let providers = [];

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = String(text ?? "");
  return div.innerHTML;
}

function renderMarkdown(text) {
  if (window.marked && typeof window.marked.parse === "function") {
    return window.marked.parse(String(text ?? ""));
  }
  return `<p>${escapeHtml(text).replace(/\n/g, "<br>")}</p>`;
}

function showError(msg, boxId = "error-box") {
  const box = $(boxId);
  box.textContent = "❌ " + msg;
  box.classList.remove("hidden");
}

// ---------- 初始化 ----------
async function loadProviders() {
  const res = await fetch("/api/providers");
  providers = await res.json();
  const sel = $("opt-provider");
  sel.innerHTML = "";
  for (const p of providers) {
    const opt = document.createElement("option");
    opt.value = p.name;
    opt.textContent = `${p.name} · ${p.default_model}`;
    sel.append(opt);
  }
  renderProviderStatus();
  sel.onchange = renderProviderStatus;
}

function renderProviderStatus() {
  const current = $("opt-provider").value;
  const box = $("provider-status");
  box.innerHTML = providers.map((p) => {
    if (!p.requires_key) {
      return `<div>${p.name === current ? "▸ " : "· "}${escapeHtml(p.name)}：无需 key（本地）</div>`;
    }
    const state = p.key_set
      ? '<span class="k-set">✓ key 已配置</span>'
      : `<span class="k-miss">✗ 缺 ${escapeHtml(p.key_vars.join(" / "))}</span>`;
    return `<div>${p.name === current ? "▸ " : "· "}${escapeHtml(p.name)}：${state}</div>`;
  }).join("");
}

async function loadDefaults() {
  const d = await (await fetch("/api/defaults")).json();
  $("opt-provider").value = d.provider;
  selectedMode = d.context_mode;
  renderModeCards();
}

async function loadSamples() {
  const samples = await (await fetch("/api/samples")).json();
  const box = $("sample-chips");
  box.innerHTML = "";
  samples.forEach((s, i) => {
    const chip = document.createElement("span");
    chip.className = "sample-chip";
    chip.textContent = s.name;
    chip.onclick = () => { $("task").value = s.task; };
    box.append(chip);
  });
}

// ---------- 模式卡片 ----------
function renderModeCards() {
  const single = $("mode-cards");
  const ablation = $("ablation-mode-cards");
  single.innerHTML = "";
  ablation.innerHTML = "";
  for (const [value, meta] of Object.entries(MODE_META)) {
    const card = document.createElement("div");
    card.className = "mode-card" + (value === selectedMode ? " selected" : "");
    card.innerHTML = `<div class="mc-icon">${meta.icon}</div>
      <div class="mc-name">${meta.name}</div>
      <div class="mc-desc">${meta.desc}</div>`;
    card.onclick = () => {
      selectedMode = value;
      renderModeCards();
    };
    single.append(card);

    const acard = card.cloneNode(true);
    acard.classList.toggle("selected", ablationModes.has(value));
    acard.onclick = () => {
      if (ablationModes.has(value)) {
        if (ablationModes.size === 1) return; // 至少保留一个模式
        ablationModes.delete(value);
      } else {
        ablationModes.add(value);
      }
      acard.classList.toggle("selected", ablationModes.has(value));
    };
    ablation.append(acard);
  }
}

// ---------- 单任务 ----------
async function runSingle() {
  const task = $("task").value.trim();
  if (!task) { showError("请输入任务内容"); return; }
  const btn = $("run-btn");
  btn.disabled = true;
  $("status-hint").textContent = "🧠 Agent 执行中（ReAct 循环，单次 10s-3min）…";
  $("error-box").classList.add("hidden");
  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task,
        context_mode: selectedMode,
        provider: $("opt-provider").value,
        model: $("opt-model").value.trim() || null,
      }),
    });
    const data = await res.json();
    if (!res.ok) { showError(data.detail || `请求失败（HTTP ${res.status}）`); return; }
    renderSingleResult(data);
    $("task").value = "";
    refreshHistory();
  } catch (e) {
    showError("网络错误：无法连接本地服务。");
  } finally {
    btn.disabled = false;
    $("status-hint").textContent = "";
    refreshState();
  }
}

function renderSingleResult(entry) {
  $("result").classList.remove("hidden");

  const badgeMode = $("badge-mode");
  badgeMode.className = "badge";
  badgeMode.textContent = `${MODE_META[entry.context_mode]?.icon || ""} ${MODE_META[entry.context_mode]?.name || entry.context_mode}`;

  const badgeStatus = $("badge-status");
  badgeStatus.className = "badge " + (entry.status === "success" ? "ok" : "fail");
  badgeStatus.textContent = entry.status === "success" ? "✓ 已完成" : "✗ 出错";

  const g = GROUNDING_LABEL[entry.grounding?.verdict];
  const badgeG = $("badge-grounding");
  badgeG.className = "badge " + (g ? ` ${g.cls}` : "badge-dim");
  badgeG.textContent = g ? `数字来源：${g.text}` : "数字来源：-";

  const badgeStats = $("badge-stats");
  badgeStats.textContent =
    `${entry.iterations} 轮 · ${entry.trajectory?.tool_calls?.length ?? 0} 次工具 · ` +
    `${entry.trajectory?.reasoning_steps_count ?? 0} 段思考 · ${entry.elapsed_seconds}s`;

  $("answer").innerHTML = entry.status === "error"
    ? `<p class="m-fail">${escapeHtml(entry.error)}</p>`
    : renderMarkdown(entry.final_answer || "（模型未给出明确答案）");

  const tl = $("timeline");
  tl.innerHTML = "";
  const calls = entry.trajectory?.tool_calls || [];
  calls.forEach((call, i) => {
    const item = document.createElement("details");
    item.className = "tl-item";
    const summary = document.createElement("summary");
    summary.className = "tl-head";
    summary.innerHTML = `<b>🔧 ${escapeHtml(call.tool_name)}</b> · 第 ${i + 1} 次工具调用`;
    const body = document.createElement("div");
    body.className = "tl-body";
    body.textContent =
      `参数: ${JSON.stringify(call.arguments, null, 2)}\n结果: ` +
      JSON.stringify(call.result, null, 2).slice(0, 2000);
    item.append(summary, body);
    tl.append(item);
  });
  if (!calls.length) {
    tl.innerHTML = '<div class="tl-item"><span class="tl-head">（本次没有工具调用）</span></div>';
  }

  renderContextTurns(entry);

  $("meta").textContent =
    `provider=${entry.params?.provider ?? "?"} · mode=${entry.context_mode} · ` +
    `${entry.created_at}`;
}

// ---------- 每轮实发上下文（实验 1-1 的本体证据） ----------
const ROLE_LABEL = { system: "系统", user: "用户", assistant: "助手", tool: "工具" };

function renderContextTurns(entry) {
  const box = $("ctx-turns");
  box.innerHTML = "";
  const turns = entry.trajectory?.api_turns || [];
  if (!turns.length) {
    box.innerHTML = '<div class="tl-item"><span class="tl-head">（无请求记录）</span></div>';
    return;
  }
  turns.forEach((turn, idx) => {
    const details = document.createElement("details");
    details.className = "ctx-turn";
    if (idx === turns.length - 1) details.open = true;

    const toolFlag = turn.tools_included
      ? '<span class="ctx-flag ok">工具定义 ✓</span>'
      : '<span class="ctx-flag fail">无工具定义</span>';
    const summary = document.createElement("summary");
    summary.className = "tl-head";
    summary.innerHTML =
      `<b>第 ${escapeHtml(turn.iteration ?? idx + 1)} 轮</b>` +
      ` · ${turn.messages?.length ?? 0} 条消息 ` +
      `<span class="ctx-flag">${escapeHtml(turn.model || "")}</span> ${toolFlag}`;

    const body = document.createElement("div");
    body.className = "ctx-body";
    // 响应侧：模型本轮刚产出的思考（DESIGN D10）
    if (turn.response_reasoning) {
      const turnReasoning = document.createElement("details");
      turnReasoning.className = "turn-reasoning";
      turnReasoning.innerHTML =
        '<summary>💭 本轮模型思考</summary><pre></pre>';
      turnReasoning.querySelector("pre").textContent = turn.response_reasoning;
      body.append(turnReasoning);
    }
    (turn.messages || []).forEach((m) => {
      const row = document.createElement("div");
      row.className = "msg-row";
      const isEmptyTool = m.role === "tool" && !m.content;
      const role = document.createElement("span");
      role.className = `msg-role role-${m.role}`;
      role.textContent = ROLE_LABEL[m.role] || m.role;
      const col = document.createElement("div");
      col.className = "msg-col";
      // 请求侧：回放进上下文的思考（no_reasoning 模式下不存在，DESIGN D10）
      if (m.reasoning) {
        const rpre = document.createElement("pre");
        rpre.className = "msg-reasoning";
        rpre.textContent = "💭 " + m.reasoning + (m.truncated ? "\n…（已截断）" : "");
        col.append(rpre);
      }
      const pre = document.createElement("pre");
      pre.className = "msg-content" + (isEmptyTool ? " msg-empty" : "");
      if (isEmptyTool) {
        pre.textContent = "（空 —— 本轮工具结果未回传给模型）";
      } else {
        pre.textContent = (m.content || "") + (m.truncated ? "\n…（已截断）" : "");
      }
      col.append(pre);
      // 助手消息附带的工具调用与思考标记
      const extra = [];
      if (m.tool_calls?.length) {
        extra.push("🔧 " + m.tool_calls.map((t) => t.name).join(", "));
      }
      if (m.has_reasoning) extra.push("💭 含思考过程");
      if (extra.length) {
        const tag = document.createElement("span");
        tag.className = "msg-extra";
        tag.textContent = extra.join(" · ");
        col.append(tag);
      }
      row.append(role, col);
      body.append(row);
    });
    details.append(summary, body);
    box.append(details);
  });
}

// ---------- 消融套件 ----------
async function startAblation() {
  const box = $("ablation-error");
  box.classList.add("hidden");
  const btn = $("ablation-btn");
  btn.disabled = true;
  try {
    const res = await fetch("/api/ablation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        context_modes: [...ablationModes],
        num_cases: Number($("opt-num-cases").value),
        provider: $("opt-provider").value,
        model: $("opt-model").value.trim() || null,
      }),
    });
    const data = await res.json();
    if (!res.ok) { showError(data.detail || `请求失败（HTTP ${res.status}）`, "ablation-error"); return; }
    $("ablation-progress").classList.remove("hidden");
    $("ablation-results").classList.add("hidden");
    buildProgressGrid(data.params || { context_modes: [...ablationModes], num_cases: Number($("opt-num-cases").value) });
    startPolling(data.job_id);
  } catch (e) {
    showError("网络错误：无法连接本地服务。", "ablation-error");
  } finally {
    btn.disabled = false;
  }
}

function buildProgressGrid(params) {
  const modes = params.context_modes;
  const cases = params.num_cases;
  const grid = $("progress-grid");
  grid.innerHTML = "";
  for (const mode of modes) {
    const row = document.createElement("div");
    row.className = "pg-row";
    const label = document.createElement("div");
    label.className = "pg-mode";
    label.textContent = `${MODE_META[mode]?.icon || ""} ${MODE_META[mode]?.name || mode}`;
    const cells = document.createElement("div");
    cells.className = "pg-cells";
    for (let c = 1; c <= cases; c++) {
      const cell = document.createElement("div");
      cell.className = "pg-cell";
      cell.id = `cell-${mode}-${c}`;
      cell.textContent = "…";
      cells.append(cell);
    }
    row.append(label, cells);
    grid.append(row);
  }
  $("progress-text").textContent = "0 / " + (modes.length * cases);
}

function startPolling(jobId) {
  stopPolling();
  polling = setInterval(async () => {
    const res = await fetch(`/api/jobs/${jobId}`);
    if (res.status === 404) { stopPolling(); showError("任务不存在（服务可能已重启）", "ablation-error"); return; }
    const snap = await res.json();
    renderProgress(snap);
    if (snap.status === "completed") { stopPolling(); renderAblationResults(snap); refreshHistory(); refreshState(); }
    if (snap.status === "failed") {
      stopPolling();
      showError(`消融任务失败：${snap.error}`, "ablation-error");
      refreshState();
    }
  }, 1500);
}

function stopPolling() { if (polling) { clearInterval(polling); polling = null; } }

function renderProgress(snap) {
  const { done = 0, total = 0, current = null } = snap.progress || {};
  $("progress-text").textContent = `${done} / ${total}`;
  const grid = $("progress-grid");
  const params = snap.params;
  (params?.context_modes || []).forEach((mode) => {
    for (let c = 1; c <= (params?.num_cases || 1); c++) {
      const cell = $(`cell-${mode}-${c}`);
      if (!cell) continue;
      if (current && current.context_mode === mode) {
        // 当前运行的案例号无法从套件直接得知（套件按模式顺序逐个跑），
        // 用"该模式下尚未落定的最靠前单元"近似高亮。
        if (cell.classList.contains("pg-cell")) {
          if (!/done-/.test(cell.className) && !cell.dataset.done) {
            cell.className = "pg-cell running";
            cell.textContent = "⟳ 运行中";
          }
        }
      }
    }
  });
  // 已完成的结果逐个落定
  (snap.results || []).forEach((r) => {
    const caseIdx = (params?.context_modes || []).indexOf(r.context_mode) >= 0
      ? countDoneFor(params, r) : 1;
    const cell = $(`cell-${r.context_mode}-${caseIdx}`);
    if (!cell || cell.dataset.done) return;
    cell.dataset.done = "1";
    const outcome = !r.completed ? CELL_OUTCOME.fail
      : (r.grounding_verdict === "ungrounded" ? CELL_OUTCOME.warn : CELL_OUTCOME.ok);
    cell.className = `pg-cell ${outcome.cls}`;
    cell.textContent = `${outcome.mark} ${r.iterations}it/${r.num_tool_calls}tc`;
    cell.title = r.error || "";
  });
  const log = $("job-log");
  if (snap.log) { log.textContent = snap.log; log.scrollTop = log.scrollHeight; }
}

function countDoneFor(params, result) {
  // 同一模式可能跑多个案例：按 results 顺序给同模式的第 n 个完成结果编号。
  // 这里用 dataset 计数器简化：每次落定自增。
  const key = `count-${result.context_mode}`;
  const grid = $("progress-grid");
  const n = (Number(grid.dataset[key] || 0)) + 1;
  grid.dataset[key] = String(n);
  return n;
}

function outcomeOf(r) {
  if (!r.completed) return CELL_OUTCOME.fail;
  return r.grounding_verdict === "ungrounded" ? CELL_OUTCOME.warn : CELL_OUTCOME.ok;
}

function renderAblationResults(snap) {
  $("ablation-results").classList.remove("hidden");
  const results = snap.results || [];

  // 矩阵
  const cases = [...new Set(results.map((r) => r.case_name))];
  const modes = [...new Set(results.map((r) => r.context_mode))];
  const byKey = {};
  results.forEach((r) => { byKey[`${r.context_mode}|${r.case_name}`] = r; });
  let html = '<table><tr><th>Context Mode</th>' +
    cases.map((c) => `<th>${escapeHtml(c)}</th>`).join("") + "</tr>";
  for (const mode of modes) {
    html += `<tr><td>${MODE_META[mode]?.icon || ""} ${MODE_META[mode]?.name || mode}</td>`;
    for (const c of cases) {
      const r = byKey[`${mode}|${c}`];
      if (!r) { html += "<td>-</td>"; continue; }
      const o = outcomeOf(r);
      html += `<td class="${o.mcls}">${o.mark} ${r.iterations}it / ${r.num_tool_calls}tc` +
        `${r.grounding_verdict === "ungrounded" ? " · UNSUPPORTED" : ""}</td>`;
    }
    html += "</tr>";
  }
  html += "</table>";
  $("matrix").innerHTML = html;

  const reportLink = $("report-link");
  reportLink.href = `/api/report/${snap.job_id}`;

  renderCharts(results, cases);
}

function renderCharts(results, cases) {
  charts.forEach((c) => c.destroy());
  charts = [];
  const modes = [...new Set(results.map((r) => r.context_mode))];
  const palette = ["#6366f1", "#22d3ee", "#a78bfa", "#f472b6", "#fbbf24"];
  const datasetsFor = (field) => cases.map((c, ci) => ({
    label: c,
    data: modes.map((m) => {
      const r = results.find((x) => x.context_mode === m && x.case_name === c);
      return r ? (r[field] ?? 0) : 0;
    }),
    backgroundColor: palette[ci % palette.length] + "cc",
    borderRadius: 6,
  }));
  const common = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { labels: { color: "#8b93a8", font: { size: 10 } } } },
    scales: {
      x: { ticks: { color: "#8b93a8", font: { size: 10 } }, grid: { color: "rgba(148,163,184,0.08)" } },
      y: { ticks: { color: "#8b93a8" }, grid: { color: "rgba(148,163,184,0.08)" }, beginAtZero: true },
    },
  };
  const mk = (id, field, title) => {
    charts.push(new Chart($(id), {
      type: "bar",
      data: { labels: modes.map((m) => MODE_META[m]?.name || m), datasets: datasetsFor(field) },
      options: { ...common, plugins: { ...common.plugins, title: { display: true, text: title, color: "#e6e9f2", font: { size: 12 } } } },
    }));
  };
  mk("chart-iterations", "iterations", "迭代次数");
  mk("chart-toolcalls", "num_tool_calls", "工具调用次数");
  mk("chart-time", "execution_time", "耗时（秒）");
}

// ---------- 历史 ----------
async function refreshHistory() {
  const items = await (await fetch("/api/history")).json();
  const ul = $("history-list");
  ul.innerHTML = "";
  if (!items.length) { ul.innerHTML = '<li class="empty">暂无记录</li>'; return; }
  for (const it of items) {
    const li = document.createElement("li");
    const kind = document.createElement("span");
    kind.className = "badge-kind";
    kind.textContent = it.kind === "ablation" ? "消融" : "单任务";
    const q = document.createElement("span");
    q.className = "q";
    q.textContent = it.title;
    li.append(kind, q);
    li.title = it.created_at;
    li.onclick = async () => {
      const r = await fetch(`/api/history/${it.id}`);
      if (!r.ok) { showError((await r.json()).detail || "历史加载失败"); return; }
      const entry = await r.json();
      if (entry.kind === "single") {
        switchTab("single");
        $("result").classList.remove("hidden");
        renderSingleResult(entry);
      } else {
        switchTab("ablation");
        $("ablation-progress").classList.add("hidden");
        renderAblationResults({
          job_id: entry.job_id, results: entry.results_summary,
        });
      }
    };
    ul.append(li);
  }
}

// ---------- 状态 / tabs ----------
async function refreshState() {
  try {
    const s = await (await fetch("/api/state")).json();
    const busy = s.busy;
    $("run-btn").disabled = busy;
    $("ablation-btn").disabled = busy;
    if (busy && s.current_job && !polling) {
      $("status-hint").textContent = "消融任务运行中，执行入口已锁定";
      $("ablation-progress").classList.remove("hidden");
      buildProgressGrid({ context_modes: ablationModes, num_cases: 1 });
      startPolling(s.current_job);
    }
  } catch (e) { /* 服务不可达时静默 */ }
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name));
  $("tab-single").classList.toggle("hidden", name !== "single");
  $("tab-ablation").classList.toggle("hidden", name !== "ablation");
}

document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => switchTab(t.dataset.tab);
});
$("run-btn").onclick = runSingle;
$("ablation-btn").onclick = startAblation;
$("task").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) runSingle();
});
$("clear-history").onclick = async () => {
  await fetch("/api/history", { method: "DELETE" });
  refreshHistory();
};

loadProviders().then(() => loadDefaults()).then(() => { renderModeCards(); loadSamples(); refreshHistory(); refreshState(); });
