"use strict";
const $ = (id) => document.getElementById(id);
let backendList = [],
  defaults = {},
  selected = null,
  selectedOwner = null,
  activeJob = null,
  pollTimer = null,
  pollDelay = 1000,
  clockTimer = null;
const labels = {
  queued: "等待启动",
  running: "执行中",
  completed: "已完成",
  error: "执行失败",
  empty: "空会话",
};
function node(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}
function notice(text) {
  $("notice").textContent = text || "";
  $("notice").hidden = !text;
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (response.status === 204) return null;
  const body = await response.json();
  if (!response.ok)
    throw new Error(body.error?.message || `HTTP ${response.status}`);
  return body;
}
function settings() {
  return {
    backend: $("backend").value,
    model: $("model").value.trim() || null,
    use_tools: $("useTools").checked,
    reasoning: $("reasoning").value,
    verbosity: $("verbosity").value || null,
    max_output_tokens: $("maxTokens").value
      ? Number($("maxTokens").value)
      : null,
  };
}
function setSettings(config) {
  $("backend").value = config.backend || "openai";
  $("model").value = config.requested_model || config.model || "";
  $("useTools").checked = config.use_tools !== false;
  $("reasoning").value = config.reasoning || "low";
  $("verbosity").value = config.verbosity || "";
  $("maxTokens").value = config.max_output_tokens ?? "";
  updateConfig();
}
function updateConfig() {
  const b = backendList.find((v) => v.name === $("backend").value);
  const frozen = !!selectedOwner && selectedOwner.kind === "conversation";
  for (const id of [
    "backend",
    "model",
    "reasoning",
    "verbosity",
    "maxTokens",
    "useTools",
  ])
    $(id).disabled = frozen;
  if (b) {
    $("backendInfo").textContent =
      `${b.key_set ? "● 已配置密钥" : "○ 未配置密钥"} · ${b.model} · ${b.base_url}`;
    $("model").placeholder = b.model;
    if (b.name === "dashscope") {
      $("reasoning").disabled = true;
      $("verbosity").disabled = true;
      $("capabilityNote").textContent =
        "百炼原生处理思考，不发送推理力度与输出详略参数。";
    } else
      $("capabilityNote").textContent =
        "参数能否使用取决于具体模型，服务端返回将作为最终依据。";
  }
  $("configMode").textContent = frozen ? "会话配置已固定" : "新会话";
  $("run").disabled = !!activeJob || !!selectedOwner?.evidence_limited;
}
function activate(tab) {
  if (tab === "preview") $("previewTask").value = $("task").value;
  for (const pane of ["chat", "preview", "experiment"])
    $(pane + "Pane").hidden = pane !== tab;
  document.querySelectorAll("[data-tab]").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === tab);
    b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  });
}
function remember() {
  if (selected) localStorage.setItem("search-codegen-selected", selected);
  else localStorage.removeItem("search-codegen-selected");
}
async function history() {
  const items = await api("/api/history");
  $("history").replaceChildren();
  for (const item of items) {
    const b = node(
      "button",
      undefined,
      "history-item" + (item.id === selected ? " selected" : ""),
    );
    b.append(
      node("div", item.title),
      node(
        "small",
        `${item.kind === "experiment" ? "实验" : "对话"} · ${labels[item.status] || item.status}`,
      ),
    );
    b.onclick = () => selectOwner(item.id).catch((e) => notice(e.message));
    $("history").append(b);
  }
  if (!items.length)
    $("history").append(node("p", "暂无历史，从一个问题开始。", "muted tiny"));
}
function downloadLink(id, format, label, turnId) {
  const a = node("a", label);
  a.href =
    `/api/exports/${encodeURIComponent(id)}?format=${format}` +
    (turnId ? `&turn_id=${encodeURIComponent(turnId)}` : "");
  a.setAttribute("download", "");
  return a;
}
function detail(title, value) {
  const d = node("details");
  d.append(node("summary", title));
  const raw =
    typeof value === "string"
      ? value
      : JSON.stringify(value ?? "未记录", null, 2);
  d.append(
    node(
      "pre",
      raw.length > 20000
        ? raw.slice(0, 20000) + "\n…展示已截断，完整证据请下载 JSON。"
        : raw,
    ),
  );
  return d;
}
function safeUrl(value) {
  try {
    const u = new URL(value);
    return ["http:", "https:"].includes(u.protocol) ? u.href : null;
  } catch {
    return null;
  }
}
function answer(text) {
  const n = node("div", undefined, "answer");
  const html = DOMPurify.sanitize(marked.parse(text || "（无可用答案）"), {
    FORBID_TAGS: ["img", "style", "iframe", "form", "input", "button"],
    FORBID_ATTR: ["style"],
  });
  n.innerHTML = html;
  n.querySelectorAll("a").forEach((a) => {
    const url = safeUrl(a.getAttribute("href"));
    if (url) {
      a.href = url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    } else a.removeAttribute("href");
  });
  return n;
}
function metrics(result) {
  const row = node("div", undefined, "metrics");
  const usage = result.usage || {};
  const input = usage.input_tokens;
  const reportedCached =
    usage.input_tokens_details?.cached_tokens ??
    usage.prompt_tokens_details?.cached_tokens;
  const inputKnown = Number.isSafeInteger(input) && input >= 0;
  const cachedKnown =
    Number.isSafeInteger(reportedCached) &&
    reportedCached >= 0 &&
    (!inputKnown || reportedCached <= input);
  const cached = cachedKnown ? reportedCached : undefined;
  const uncached = inputKnown && cachedKnown ? input - cached : undefined;
  const fields = [
    [
      "耗时",
      result.elapsed_seconds == null
        ? "未提供"
        : `${Number(result.elapsed_seconds).toFixed(1)} s`,
    ],
    ["总输入", input],
    ["缓存命中", cached],
    ["非缓存输入", uncached],
    ["输出", usage.output_tokens],
    ["思考", usage.output_tokens_details?.reasoning_tokens],
    ["总 Token", usage.total_tokens],
  ];
  for (const [name, value] of fields) {
    const m = node("span", name, "metric");
    m.append(node("strong", value ?? "未提供"));
    row.append(m);
  }
  return row;
}
function requestDetails(request) {
  const wrap = node("div");
  if (!request) {
    wrap.append(node("p", "该记录未保存请求内容", "muted"));
    return wrap;
  }
  wrap.append(
    detail(
      request.tools?.length
        ? `工具定义（${request.tools.length} 个，独立 tools 字段）`
        : "本轮未发送工具定义",
      request.tools || [],
    ),
    detail("系统指令 · instructions", request.instructions),
    detail("增量输入 · input", request.input),
  );
  const rest = { ...request };
  delete rest.tools;
  delete rest.instructions;
  delete rest.input;
  wrap.append(detail("其他请求参数", rest));
  if (request.previous_response_id)
    wrap.append(
      node(
        "p",
        "本轮通过 previous_response_id 续接；历史全文并未重新发送。",
        "muted tiny",
      ),
    );
  return wrap;
}
function resultView(result) {
  const wrap = node("div");
  if (result.error)
    wrap.append(
      detail("错误详情", {
        error: result.error,
        http_status: result.http_status ?? "未提供",
      }),
    );
  wrap.append(answer(result.response), metrics(result));
  wrap.append(
    node(
      "p",
      `后端：${result.provider ?? "未提供"} · 请求模型：${result.requested_model ?? result.request?.model ?? "未提供"} · 返回模型：${result.model ?? "未提供"} · 工具记录：${(result.tool_calls || []).length}`,
      "muted tiny",
    ),
  );
  if (result.base_url) wrap.append(node("p", result.base_url, "muted tiny"));
  const tools = node("div");
  if (!(result.tool_calls || []).length)
    tools.append(node("p", "未返回工具执行记录", "muted"));
  for (const call of result.tool_calls || []) {
    const item = node("div");
    item.append(
      node(
        "span",
        `${call.type} · ${call.status || "状态未提供"}`,
        "badge " + (call.status === "completed" ? "completed" : ""),
      ),
      detail("工具输入与输出", call),
    );
    tools.append(item);
  }
  const toolDetails = node("details");
  toolDetails.append(node("summary", "工具执行证据"), tools);
  wrap.append(toolDetails);
  const refs = node("div", undefined, "citations");
  for (const citation of result.citations || []) {
    const url = safeUrl(citation.url);
    if (!url) continue;
    const a = node("a", citation.title || url);
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    const p = node("p");
    p.append(a);
    refs.append(p);
  }
  if (!refs.children.length)
    refs.append(node("p", "未提供可访问的引用", "muted"));
  const citations = node("details");
  citations.append(node("summary", "引用来源"), refs);
  wrap.append(citations);
  const req = node("details");
  req.append(node("summary", "本轮实发请求"), requestDetails(result.request));
  wrap.append(
    req,
    detail("原始响应与 output_items", {
      raw_response: result.raw_response ?? "未记录",
      output_items: result.output_items ?? "未记录",
    }),
  );
  return wrap;
}
function renderConversation(owner) {
  $("conversation").replaceChildren();
  if (owner.evidence_limited) {
    $("conversation").append(
      node("div", owner.error?.message || "证据未保留，请新建会话", "card"),
    );
    return;
  }
  if (!owner.turns.length)
    $("conversation").append(
      node("div", "从示例开始，或提出你自己的研究问题。", "empty"),
    );
  for (const [index, turn] of owner.turns.entries()) {
    const card = node("article", undefined, "card");
    const title = node("div", undefined, "section-title");
    title.append(
      node("h2", `第 ${index + 1} 轮`),
      node("span", labels[turn.status] || turn.status, "badge " + turn.status),
    );
    card.append(title, node("div", turn.input, "turn-input"));
    if (turn.result) {
      card.append(
        resultView(turn.result),
        detail("本轮 api_turns", turn.api_turns),
      );
      const bar = node("div", undefined, "toolbar");
      if (!owner.active_job_id)
        bar.append(
          downloadLink(owner.id, "json", "本轮 JSON", turn.id),
          downloadLink(owner.id, "md", "答案 Markdown", turn.id),
        );
      card.append(bar);
    } else card.append(node("p", "等待模型响应，可能需要数分钟。", "muted"));
    $("conversation").append(card);
  }
  if (owner.turns.length && !owner.active_job_id) {
    const bar = node("div", undefined, "toolbar");
    bar.append(
      downloadLink(owner.id, "json", "下载完整会话"),
      downloadLink(owner.id, "md", "下载会话 Markdown"),
    );
    $("conversation").append(bar);
  }
}
const checkNames = {
  request_succeeded: "请求成功",
  model_identity_exact: "模型一致",
  web_search_completed: "搜索完成",
  code_interpreter_completed: "代码执行完成",
  url_citations_present: "引用存在",
  closest_pair_matches_independent_reference: "最近配对匹配参考",
  distance_reported: "包含距离单位",
  first_turn_clarified_before_tools: "先澄清再用工具（启发式）",
  continuation_used_previous_response_id: "响应 ID 续接",
  followup_succeeded: "后续请求成功",
  followup_web_search_completed: "后续搜索完成",
  followup_code_interpreter_completed: "后续代码完成",
  followup_citations_present: "后续引用存在",
  followup_reports_ma_rsi_macd: "包含指定指标名称",
};
function checks(title, value) {
  const div = node("div");
  div.append(node("h3", title));
  for (const [key, passed] of Object.entries(value?.checks || {}))
    div.append(
      node(
        "div",
        `${passed ? "✓" : "✕"} ${checkNames[key] || key}`,
        "check " + (passed ? "pass" : "fail"),
      ),
    );
  return div;
}
function renderExperiment(owner) {
  const out = $("experimentOutput");
  out.replaceChildren();
  if (owner.evidence_limited) {
    out.append(node("p", "证据容量超限，完整报告未保留。"));
    return;
  }
  if (owner.acceptance) {
    out.append(
      node("h3", owner.acceptance.passed ? "正式验收通过" : "正式验收未通过"),
    );
    out.append(
      node(
        "p",
        "执行成功与验收通过分别判断。距离单位/问号/指标词检查不代表严格语义或数值正确性证明。",
        "muted tiny",
      ),
    );
  }
  for (const run of owner.runs || []) {
    const box = node("div", undefined, "experiment-row");
    box.append(
      node(
        "h3",
        `${run.backend}${run.backend === "openrouter" ? " · 仅作诊断" : ""}`,
      ),
    );
    if (run.error) box.append(detail("执行错误", run.error));
    box.append(
      checks("首都距离", run.asean_validation),
      checks("澄清与续接", run.clarification?.validation),
    );
    if (run.asean) box.append(metrics(run.asean));
    box.append(detail("后端完整报告", run));
    out.append(box);
  }
  if (owner.usage_summary)
    out.append(detail("累计 Token（缺失字段不按零补齐）", owner.usage_summary));
  if (!owner.active_job_id && !owner.evidence_limited) {
    const bar = node("div", undefined, "toolbar");
    bar.append(downloadLink(owner.id, "json", "下载实验 JSON"));
    out.append(bar);
  }
}
async function selectOwner(id) {
  const owner = await api("/api/history/" + encodeURIComponent(id));
  selected = id;
  selectedOwner = owner;
  remember();
  if (owner.kind === "conversation") {
    setSettings(owner.config);
    renderConversation(owner);
    activate("chat");
  } else {
    selectedOwner = owner;
    updateConfig();
    renderExperiment(owner);
    activate("experiment");
  }
  await history();
}
function resetSession() {
  selected = null;
  selectedOwner = null;
  remember();
  $("conversation").replaceChildren();
  $("task").value = "";
  $("previewTask").value = "";
  $("previewOutput").replaceChildren();
  setSettings(defaults);
  activate("chat");
  history().catch((e) => notice(e.message));
  notice("");
}
function showJob(job) {
  activeJob = job;
  $("jobPanel").hidden = false;
  $("jobTitle").textContent =
    `${job.kind === "experiment" ? "实验验证" : "模型任务"} · ${labels[job.status] || job.status}`;
  const p = job.progress;
  $("jobMessage").textContent =
    job.kind === "experiment"
      ? `已结束 ${p.done} / ${p.total} 个后端${p.current_backend ? " · 当前 " + p.current_backend : ""}；后端内部阶段等待真实结果。`
      : "等待模型响应，可能需要数分钟。工具证据将在返回后显示。";
  const started = job.started_at || job.created_at;
  const info = [
    started ? "开始于 " + new Date(started).toLocaleString() : null,
    job.backend,
    job.model,
  ].filter(Boolean);
  if (info.length)
    $("jobMessage").textContent =
      info.join(" · ") + "。" + $("jobMessage").textContent;
  $("run").disabled = true;
  $("experimentRun").disabled = true;
  let elapsed = job.elapsed_seconds;
  clearInterval(clockTimer);
  function tick() {
    $("elapsed").textContent = `${elapsed.toFixed(0)} s`;
    elapsed++;
  }
  tick();
  clockTimer = setInterval(tick, 1000);
}
async function poll(jobId) {
  try {
    const job = await api("/api/jobs/" + jobId);
    pollDelay = 1000;
    if (["queued", "running"].includes(job.status)) {
      showJob(job);
      pollTimer = setTimeout(() => poll(jobId), 1000);
      return;
    }
    clearInterval(clockTimer);
    activeJob = null;
    $("jobPanel").hidden = true;
    $("experimentRun").disabled = false;
    updateConfig();
    if (selected === job.owner_id) await selectOwner(job.owner_id);
    else await history();
    if (job.status === "error")
      notice(
        `${job.error?.message || "任务执行失败"}${job.error?.class ? " · " + job.error.class : ""}`,
      );
    else notice("任务已完成，结果与执行证据已更新。");
  } catch (e) {
    notice("连接中断，后台任务可能仍在运行：" + e.message);
    pollDelay = Math.min(pollDelay * 2, 10000);
    pollTimer = setTimeout(() => poll(jobId), pollDelay);
  }
}
function follow(job) {
  clearTimeout(pollTimer);
  showJob({
    ...job,
    status: job.status || "queued",
    kind: job.kind || "turn",
    elapsed_seconds: job.elapsed_seconds || 0,
    progress: job.progress || { done: 0, total: 0 },
  });
  poll(job.id || job.job_id);
}
async function run() {
  notice("");
  if (!$("task").value.trim()) throw new Error("请输入任务内容");
  $("run").disabled = true;
  try {
    if (!selectedOwner || selectedOwner.kind !== "conversation") {
      const owner = await api("/api/sessions", {
        method: "POST",
        body: JSON.stringify({ config: settings() }),
      });
      selected = owner.id;
      selectedOwner = owner;
      remember();
      setSettings(owner.config);
    }
    const result = await api(`/api/sessions/${selected}/turns`, {
      method: "POST",
      body: JSON.stringify({ input: $("task").value }),
    });
    $("task").value = "";
    await selectOwner(selected);
    follow({ id: result.job_id, owner_id: selected, kind: "turn" });
  } finally {
    updateConfig();
  }
}
$("previewTask").oninput = () => {
  $("task").value = $("previewTask").value;
};
$("run").onclick = () =>
  run().catch((e) => {
    notice(e.message);
    updateConfig();
  });
$("task").onkeydown = (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !$("run").disabled) {
    e.preventDefault();
    $("run").click();
  }
};
$("toggleSidebar").onclick = () => {
  const expanded = $("sidebar").classList.toggle("expanded");
  $("toggleSidebar").setAttribute("aria-expanded", String(expanded));
  $("toggleSidebar").textContent = expanded ? "收起历史" : "展开历史";
};
$("newSession").onclick = resetSession;
$("backend").onchange = () => {
  updateConfig();
  document
    .querySelectorAll("#experimentBackends input")
    .forEach((n) => (n.checked = n.value === $("backend").value));
};
$("refreshHistory").onclick = () => history().catch((e) => notice(e.message));
$("clearHistory").onclick = async () => {
  try {
    const result = await api("/api/history", { method: "DELETE" });
    if (selected && !result.retained_active_ids.includes(selected))
      resetSession();
    await history();
    notice(
      result.retained_active_ids.length
        ? "已清空结束记录，保留运行中的任务。"
        : "已清空历史。",
    );
  } catch (e) {
    notice(e.message);
  }
};
$("previewRun").onclick = async () => {
  try {
    const input = $("previewTask").value;
    if (!input.trim()) throw new Error("请输入需要预览的任务内容");
    const body =
      selectedOwner?.kind === "conversation"
        ? { input, session_id: selected }
        : { input, config: settings() };
    const value = await api("/api/preview", {
      method: "POST",
      body: JSON.stringify(body),
    });
    $("previewOutput").replaceChildren(
      node(
        "p",
        `未执行 · ${value.provider} · ${value.model} · ${value.base_url}`,
        "muted",
      ),
      requestDetails(value.request),
    );
    notice("");
  } catch (e) {
    notice(e.message);
  }
};
$("experimentRun").onclick = async () => {
  try {
    const backends = [
      ...document.querySelectorAll("#experimentBackends input:checked"),
    ].map((n) => n.value);
    if (!backends.length) throw new Error("请至少选择一个后端");
    const value = await api("/api/experiments", {
      method: "POST",
      body: JSON.stringify({ backends, reasoning: $("reasoning").value }),
    });
    await selectOwner(value.experiment_id);
    follow({
      id: value.job_id,
      owner_id: value.experiment_id,
      kind: "experiment",
      progress: { done: 0, total: backends.length },
    });
    notice("");
  } catch (e) {
    notice(e.message);
  }
};
document
  .querySelectorAll("[data-tab]")
  .forEach((b) => (b.onclick = () => activate(b.dataset.tab)));
async function init() {
  try {
    [backendList, defaults] = await Promise.all([
      api("/api/backends"),
      api("/api/defaults"),
    ]);
    for (const b of backendList) {
      const opt = node(
        "option",
        `${b.name}${b.key_set ? " · 已配置" : " · 无密钥"}`,
      );
      opt.value = b.name;
      $("backend").append(opt);
      const label = node("label");
      const input = node("input");
      input.type = "checkbox";
      input.value = b.name;
      input.checked = b.name === defaults.backend;
      label.append(input, node("span", b.name));
      $("experimentBackends").append(label);
    }
    setSettings(defaults);
    activate("chat");
    const samples = await api("/api/samples");
    for (const sample of samples) {
      const b = node("button", sample.title);
      b.onclick = () => {
        if (
          selectedOwner?.kind === "conversation" &&
          selectedOwner.config.use_tools !== sample.use_tools
        ) {
          notice("当前会话工具设置已固定，请新建会话后使用此示例。");
          return;
        }
        $("task").value = sample.input;
        if (!selectedOwner) $("useTools").checked = sample.use_tools;
        activate("chat");
      };
      $("samples").append(b);
    }
    const saved = localStorage.getItem("search-codegen-selected");
    if (saved) {
      try {
        await selectOwner(saved);
      } catch {
        resetSession();
        notice("旧记录不存在，可能已重启或被回收。");
      }
    }
    await history();
    const jobs = await api("/api/jobs");
    if (jobs.length) {
      if (!selected) await selectOwner(jobs[0].owner_id);
      follow(jobs[0]);
    }
  } catch (e) {
    notice("初始化失败：" + e.message);
  }
}
init();
