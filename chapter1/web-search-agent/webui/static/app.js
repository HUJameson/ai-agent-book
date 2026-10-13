/* Web GUI 前端逻辑：fetch 调用 /api/*，渲染答案与 ReAct 轨迹（见 SPEC 第 6 节）。 */

const $ = (id) => document.getElementById(id);

// 与 agent.py STEP_LABELS 对齐的展示元数据；open 控制默认折叠
const TRACE_META = {
  thought: { icon: "💭", label: "思考", open: false },
  action: { icon: "🔧", label: "行动", open: false },
  observation: { icon: "👀", label: "观察", open: false },
  answer: { icon: "✅", label: "最终答案", open: true },
};

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

async function loadDefaults() {
  try {
    const res = await fetch("/api/defaults");
    const d = await res.json();
    $("opt-provider").value = d.provider;
    $("opt-model").value = d.model;
    $("opt-max-steps").value = d.max_steps;
    $("opt-temperature").value = d.temperature;
    $("opt-timeout").value = d.timeout;
  } catch (e) {
    showError("无法从服务端读取默认参数，请确认服务已启动。");
  }
}

function currentOptions() {
  return {
    provider: $("opt-provider").value,
    model: $("opt-model").value.trim() || "kimi-k3",
    max_steps: Number($("opt-max-steps").value),
    temperature: Number($("opt-temperature").value),
    timeout: Number($("opt-timeout").value),
  };
}

function showError(message) {
  const box = $("error-box");
  box.textContent = "❌ " + message;
  box.classList.remove("hidden");
}

function renderEntry(entry) {
  $("error-box").classList.add("hidden");
  $("result").classList.remove("hidden");

  const answerEl = $("answer");
  answerEl.innerHTML = renderMarkdown(entry.answer);
  answerEl.classList.toggle("answer-error", entry.status === "error");

  const traceEl = $("trace");
  traceEl.innerHTML = "";
  for (const step of entry.trace || []) {
    const meta = TRACE_META[step.type] || { icon: "•", label: step.type, open: false };
    const details = document.createElement("details");
    details.className = `trace-step trace-${step.type}`;
    if (meta.open) details.open = true;

    const content = String(step.content ?? "");
    let summaryText;
    if (step.type === "action") {
      summaryText = `调用工具 ${step.tool} 参数=${JSON.stringify(step.args ?? {})}`;
    } else {
      const flat = content.replace(/\s+/g, " ").trim();
      summaryText = flat.length > 120 ? flat.slice(0, 120) + "…" : flat;
    }

    const summary = document.createElement("summary");
    summary.innerHTML =
      `<span>${meta.icon}</span>` +
      `<span>[${escapeHtml(step.iteration ?? "-")}]</span>` +
      `<b>${meta.label}</b>` +
      `<span class="preview"></span>`;
    summary.querySelector(".preview").textContent = summaryText;

    const body = document.createElement("pre");
    body.textContent = step.type === "action"
      ? `工具: ${step.tool}\n参数: ${JSON.stringify(step.args ?? {}, null, 2)}`
      : content;

    details.append(summary, body);
    traceEl.append(details);
  }

  const p = entry.params || {};
  $("meta").textContent =
    `provider=${p.provider} · model=${p.model} · max_steps=${p.max_steps} · ` +
    `temperature=${p.temperature} · timeout=${p.timeout}s · ` +
    `用时 ${entry.elapsed_seconds}s · ${entry.created_at}`;
}

async function refreshHistory() {
  const res = await fetch("/api/history");
  const items = await res.json();
  const ul = $("history-list");
  ul.innerHTML = "";
  if (!items.length) {
    ul.innerHTML = '<li class="empty">暂无记录</li>';
    return;
  }
  for (const item of items) {
    const li = document.createElement("li");
    li.title = item.created_at;
    const q = document.createElement("span");
    q.className = "q";
    q.textContent = item.question;
    const tag = document.createElement("span");
    tag.className = `tag ${item.status}`;
    tag.textContent = item.status === "error" ? "失败" : "成功";
    li.append(q, tag);
    li.onclick = async () => {
      const r = await fetch(`/api/history/${item.id}`);
      if (r.ok) renderEntry(await r.json());
      else showError((await r.json()).detail || "历史记录加载失败");
    };
    ul.append(li);
  }
}

async function ask() {
  const question = $("question").value.trim();
  if (!question) {
    showError("请输入一个问题");
    return;
  }
  $("ask-btn").disabled = true;
  $("status-hint").textContent =
    "🔍 搜索中（kimi-k3 单次可能需要 1-3 分钟，请耐心等待）…";
  $("error-box").classList.add("hidden");
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, options: currentOptions() }),
    });
    const data = await res.json();
    if (!res.ok) {
      showError(data.detail || `请求失败（HTTP ${res.status}）`);
      return;
    }
    renderEntry(data);
    $("question").value = "";
    refreshHistory();
  } catch (e) {
    showError("网络错误：无法连接本地服务，请确认 webui 已启动。");
  } finally {
    $("ask-btn").disabled = false;
    $("status-hint").textContent = "";
  }
}

$("ask-btn").onclick = ask;
$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) ask();
});
$("clear-history").onclick = async () => {
  await fetch("/api/history", { method: "DELETE" });
  refreshHistory();
};

loadDefaults().then(refreshHistory);
