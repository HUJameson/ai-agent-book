/* Offline DOM tests: node --test tests/test_webui_frontend.cjs (jsdom required). */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");
const staticDir = path.resolve(__dirname, "../webui/static");
const workspace =
  "https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1";
const config = {
  backend: "dashscope",
  requested_model: "model",
  base_url: workspace,
  use_tools: true,
  reasoning: "low",
  verbosity: null,
  max_output_tokens: null,
};
const result = {
  success: true,
  response:
    '## 答案\n\n**安全结果**\n\n<img src=x onerror="window.pwned=1"><script>window.pwned=1</script>\n\n[危险链接](javascript:alert(1))',
  request: {
    tools: [{ type: "web_search", description: "<script>evil</script>" }],
    instructions: "instructions",
    input: "task",
    stream: true,
  },
  tool_calls: [{ type: "web_search_call", status: "completed" }],
  provider: "dashscope",
  base_url: workspace,
  requested_model: "model",
  model: "model",
  usage: { input_tokens: 7 },
  elapsed_seconds: 1,
  citations: [
    { url: "javascript:alert(1)" },
    { url: "https://example.com/source", title: "来源" },
  ],
};
const record = () => ({
  id: "session-unit",
  kind: "conversation",
  title: "离线任务",
  config: { ...config },
  active_job_id: null,
  status: "completed",
  turns: [
    {
      id: "turn-unit",
      input: "task",
      job_id: "job-unit",
      status: "completed",
      result,
      api_turns: [],
    },
  ],
});

async function until(check) {
  for (let i = 0; i < 150; i++) {
    if (check()) return;
    await new Promise((r) => setTimeout(r, 5));
  }
  assert.fail("DOM state did not settle");
}
async function boot({ owner = null, active = false, sse = false } = {}) {
  const dom = new JSDOM(
    fs.readFileSync(path.join(staticDir, "index.html"), "utf8"),
    { url: "http://127.0.0.1:8001", runScripts: "outside-only" },
  );
  const streams = [];
  if (sse)
    dom.window.EventSource = class {
      constructor(url) {
        this.url = url;
        this.listeners = {};
        streams.push(this);
      }
      addEventListener(name, fn) {
        this.listeners[name] = fn;
      }
      close() {
        this.closed = true;
      }
      emit(name, id, payload) {
        this.listeners[name]?.({
          lastEventId: String(id),
          data: JSON.stringify(payload),
        });
      }
    };
  const w = dom.window,
    calls = [];
  let stored = owner,
    current = active,
    polls = 0;
  if (owner) w.localStorage.setItem("search-codegen-selected", owner.id);
  const job = () => ({
    id: "job-unit",
    owner_id: "session-unit",
    kind: "turn",
    status: "running",
    elapsed_seconds: 2,
    progress: { done: 0, total: 0, current_backend: null },
  });
  const nativeTimeout = w.setTimeout.bind(w);
  w.setTimeout = (callback, ms) => nativeTimeout(callback, Math.min(ms, 10));
  w.fetch = async (url, options = {}) => {
    const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push({ url, method, body });
    let value,
      status = 200;
    if (url === "/api/backends")
      value = [
        {
          name: "dashscope",
          key_set: true,
          model: "model",
          base_url: workspace,
        },
      ];
    else if (url === "/api/defaults")
      value = { ...config, model: null, poll_interval_ms: 1000 };
    else if (url === "/api/samples")
      value = [{ title: "离线示例", input: "示例任务", use_tools: true }];
    else if (url === "/api/history")
      value = stored ? [{ ...stored, updated_at: "2026-10-28" }] : [];
    else if (url === "/api/jobs") value = current ? [job()] : [];
    else if (url === "/api/sessions" && method === "POST") {
      stored = record();
      stored.turns = [];
      value = stored;
      status = 201;
    } else if (url === "/api/sessions/session-unit/turns") {
      current = true;
      value = { job_id: "job-unit", session_id: "session-unit" };
      status = 202;
    } else if (url === "/api/jobs/job-unit") {
      polls++;
      if (polls < 2) value = job();
      else {
        current = false;
        stored = record();
        value = { ...job(), status: "completed" };
      }
    } else if (url === "/api/history/session-unit") value = stored;
    else if (url === "/api/preview")
      value = {
        provider: "dashscope",
        model: "model",
        base_url: workspace,
        dry_run: true,
        request: { ...result.request, input: body.input },
      };
    else throw new Error("unexpected mocked endpoint: " + url);
    return {
      ok: status < 400,
      status,
      json: async () => structuredClone(value),
    };
  };
  for (const file of ["vendor/marked.min.js", "vendor/purify.min.js", "app.js"])
    w.eval(fs.readFileSync(path.join(staticDir, file), "utf8"));
  await until(() => w.document.querySelector("#samples button"));
  return { dom, w, calls, streams };
}

test("backend capabilities and dry-run preview use real input without submitting a turn", async () => {
  const { dom, w, calls } = await boot();
  try {
    assert.equal(w.document.querySelector("#reasoning").disabled, true);
    w.document.querySelector("#toggleSidebar").click();
    assert.equal(
      w.document.querySelector("#toggleSidebar").getAttribute("aria-expanded"),
      "true",
    );
    w.document.querySelector("#toggleSidebar").click();
    assert.equal(
      w.document.querySelector("#sidebar").classList.contains("expanded"),
      false,
    );
    w.document.querySelector("#samples button").click();
    assert.equal(w.document.querySelector("#task").value, "示例任务");
    w.document.querySelector("[data-tab=preview]").click();
    assert.equal(w.document.querySelector("#previewPane").hidden, false);
    w.document.querySelector("#previewRun").click();
    await until(() => w.document.querySelector("#previewOutput pre"));
    assert.match(
      w.document.querySelector("#previewOutput").textContent,
      /未执行/,
    );
    assert.match(
      w.document.querySelector("#previewOutput").textContent,
      /stream/,
    );
    assert.equal(
      calls.find((c) => c.url === "/api/preview").body.input,
      "示例任务",
    );
    assert.equal(
      calls.some((c) => c.method === "POST" && c.url !== "/api/preview"),
      false,
    );
  } finally {
    dom.window.close();
  }
});

test("history safely renders Markdown, tools, citations and missing usage", async () => {
  const { dom, w } = await boot({ owner: record() });
  try {
    await until(() => w.document.querySelector("#conversation .answer"));
    const view = w.document.querySelector("#conversation");
    assert.equal(view.querySelector("script,img"), null);
    assert.equal(w.pwned, undefined);
    assert.equal(view.querySelector('a[href^="javascript:"]'), null);
    assert.ok(view.querySelector(".answer strong"));
    assert.match(view.textContent, /web_search_call · completed/);
    assert.match(view.textContent, /未提供/);
    assert.match(view.textContent, /<script>evil<\/script>/);
    assert.equal(
      view.querySelector(".citations a").href,
      "https://example.com/source",
    );
    assert.equal(w.document.querySelector("#backend").disabled, true);
  } finally {
    dom.window.close();
  }
});

test("submission and polling finish without duplicate model submissions", async () => {
  const { dom, w, calls } = await boot();
  try {
    w.document.querySelector("#task").value = "unit task";
    w.document.querySelector("#run").click();
    await until(() =>
      w.document.querySelector("#notice").textContent.includes("任务已完成"),
    );
    assert.equal(w.document.querySelector("#jobPanel").hidden, true);
    assert.equal(w.document.querySelector("#run").disabled, false);
    assert.equal(calls.filter((c) => c.url.endsWith("/turns")).length, 1);
    assert.equal(
      calls.filter((c) => c.url === "/api/sessions" && c.method === "POST")
        .length,
      1,
    );
    assert.match(
      w.document.querySelector("#conversation").textContent,
      /安全结果/,
    );
  } finally {
    dom.window.close();
  }
});

test("refresh discovers an existing running job and resumes polling", async () => {
  const owner = record();
  owner.active_job_id = "job-unit";
  owner.status = "running";
  const { dom, w, calls } = await boot({ owner, active: true });
  try {
    await until(() =>
      w.document.querySelector("#notice").textContent.includes("任务已完成"),
    );
    assert.ok(calls.some((c) => c.url === "/api/jobs/job-unit"));
    assert.equal(calls.filter((c) => c.method === "POST").length, 0);
    assert.equal(w.document.querySelector("#jobPanel").hidden, true);
  } finally {
    dom.window.close();
  }
});

test("input metrics split cache hits, preserve zero, and do not invent missing counts", async () => {
  const cases = [
    [
      { input_tokens: 93685, input_tokens_details: { cached_tokens: 66304 } },
      "66304",
      "27381",
    ],
    [
      { input_tokens: 100, input_tokens_details: { cached_tokens: 0 } },
      "0",
      "100",
    ],
    [{ input_tokens: 100 }, "未提供", "未提供"],
    [
      { input_tokens: 100, input_tokens_details: { cached_tokens: 101 } },
      "未提供",
      "未提供",
    ],
    [
      { input_tokens: 100, prompt_tokens_details: { cached_tokens: 25 } },
      "25",
      "75",
    ],
    [{ input_tokens_details: { cached_tokens: 25 } }, "25", "未提供"],
  ];
  for (const [usage, cached, uncached] of cases) {
    const owner = structuredClone(record());
    owner.turns[0].result.usage = usage;
    const { dom, w } = await boot({ owner });
    try {
      await until(() => w.document.querySelector("#conversation .metrics"));
      const values = Object.fromEntries(
        [...w.document.querySelectorAll("#conversation .metric")].map((m) => [
          m.firstChild.textContent,
          m.querySelector("strong").textContent,
        ]),
      );
      assert.equal(values["总输入"], String(usage.input_tokens ?? "未提供"));
      assert.equal(values["缓存命中"], cached);
      assert.equal(values["非缓存输入"], uncached);
    } finally {
      dom.window.close();
    }
  }
});

test("SSE previews arrive before completion, replace snapshots and ignore duplicate IDs", async () => {
  const owner = record();
  owner.status = "running";
  owner.active_job_id = "job-unit";
  const { dom, w, calls, streams } = await boot({
    owner,
    active: true,
    sse: true,
  });
  try {
    await until(() => streams.length === 1);
    const source = streams[0];
    source.emit("answer.snapshot", 1, {
      owner_id: owner.id,
      item_key: "answer",
      text: "第一段",
      backend: "dashscope",
      stage: "turn",
    });
    assert.match(w.document.querySelector("#liveOutput").textContent, /第一段/);
    assert.equal(calls.filter((c) => c.url === "/api/jobs/job-unit").length, 0);
    source.emit("answer.snapshot", 1, {
      owner_id: owner.id,
      item_key: "answer",
      text: "重复",
    });
    assert.doesNotMatch(
      w.document.querySelector("#liveOutput").textContent,
      /重复/,
    );
    source.emit("answer.snapshot", 2, {
      owner_id: owner.id,
      item_key: "answer",
      text: "第一段第二段",
    });
    assert.match(
      w.document.querySelector("#liveOutput").textContent,
      /第一段第二段/,
    );
    source.emit("tool.state", 3, {
      owner_id: owner.id,
      call_id: "tool",
      type: "web_search_call",
      status: "in_progress",
    });
    assert.match(
      w.document.querySelector("#liveOutput").textContent,
      /in_progress/,
    );
    source.emit("job.done", 4, { owner_id: owner.id, status: "completed" });
    await until(() =>
      w.document.querySelector("#notice").textContent.includes("任务已完成"),
    );
    assert.equal(source.closed, true);
    assert.equal(calls.filter((c) => c.method === "POST").length, 0);
    assert.match(
      w.document.querySelector("#conversation").textContent,
      /安全结果/,
    );
  } finally {
    dom.window.close();
  }
});

test("SSE reset replaces stale blocks and reconnect carries cursor without another submission", async () => {
  const owner = record();
  owner.status = "running";
  owner.active_job_id = "job-unit";
  const { dom, w, calls, streams } = await boot({
    owner,
    active: true,
    sse: true,
  });
  try {
    await until(() => streams.length === 1);
    streams[0].emit("answer.snapshot", 1, {
      owner_id: owner.id,
      item_key: "a",
      text: "旧内容",
    });
    streams[0].emit("stream.reset", 5, {
      owner_id: owner.id,
      status: "running",
      blocks: [{ item_key: "a", text: "恢复内容" }],
      tools: [],
    });
    assert.match(
      w.document.querySelector("#liveOutput").textContent,
      /恢复内容/,
    );
    assert.doesNotMatch(
      w.document.querySelector("#liveOutput").textContent,
      /旧内容/,
    );
    streams[0].onerror();
    await until(() => streams.length === 2);
    assert.match(streams[1].url, /after=5/);
    assert.equal(calls.filter((c) => c.method === "POST").length, 0);
    w.dispatchEvent(new w.Event("pagehide"));
    assert.equal(streams[1].closed, true);
  } finally {
    dom.window.close();
  }
});
