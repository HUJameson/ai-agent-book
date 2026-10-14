# context 工程 Web GUI 设计决策记录（DESIGN）

| 项 | 值 |
|---|---|
| 版本 | v0.1（2026-10-13） |
| 关联文档 | [SPEC.md](SPEC.md)（需求与验收）· [../../web-search-agent/webui/DESIGN.md](../../web-search-agent/webui/DESIGN.md)（姊妹工程模式） |
| 维护约定 | 改设计 → 先改本文 → 再改代码 → 测试守护；<150 行红线 |

## 1. 文档定位

与 web-search-agent 的 webui 同构：SPEC 管"做什么/验收"，本文管"怎么做/为什么"，微观实现交给代码与测试。仅记录代码看不出的决策。

## 2. 模块职责与依赖方向

| 文件 | 职责 | 明确不做 |
|---|---|---|
| `webui/app.py` | FastAPI 路由、静态挂载、uvicorn 入口、错误映射 | 业务逻辑、直接 import agent/main |
| `webui/agent_runner.py` | 唯一对接层：单任务执行、消融套件程序化调用、历史存储 | HTTP 概念、线程管理 |
| `webui/job_manager.py` | 后台任务：JobStore、线程 worker、进度快照 | 直接 import agent（经由 agent_runner） |
| `webui/static/` | 展示层（暗色玻璃拟态 + Chart.js） | 业务判断 |

依赖方向：`app.py → job_manager.py → agent_runner.py → agent.py / main.py（既有，零改动）`。

**关键点**：消融套件**复用 `main.AblationTestSuite`**（`run_ablation_study` / `analyze_results` / `generate_report`），不重写第二套消融逻辑——消融语义单一事实来源在 main.py，GUI 与 CLI 永远同义。`agent_runner` 对 `main` 的 import 延迟到函数内（main 顶层 import tabulate/matplotlib，避免拖累 app 启动失败面）。

## 3. 核心数据契约

```json
// HistoryEntry（kind 区分两类，统一入史）
{
  "id": "r_20261013T101500Z_a1b2",
  "kind": "single | ablation",
  "created_at": "ISO8601",
  "params": {"provider": "doubao", "model": null, "context_mode": "full",
             "context_modes": ["full","no_history"], "num_cases": 2},
  // single 专属
  "task": "...", "final_answer": "...", "context_mode": "full",
  "trajectory": {"tool_calls": [{"tool_name","arguments","result"}],
                 "reasoning_steps_count": 3, "api_turns_count": 4,
                 "api_turns": [{"iteration": 1, "model": "...", "tools_included": true,
                                "response_reasoning": "本轮模型的思考（截断）",
                                "messages": [{"role": "assistant",
                                              "reasoning": "回放进上下文的思考（截断）",
                                              "content": null, "truncated": false}]}]},
  "grounding": {"verdict": "grounded|ungrounded|...", "unsupported_quantities": []},
  "elapsed_seconds": 42.1, "status": "success | error", "error": null,  // ablation 专属
  "job_id": "job_...", "results_summary": [{"case_name","context_mode",
             "completed","grounding_verdict","iterations","num_tool_calls",
             "execution_time"}],
  "analysis": {"total_tests": 10, "completed_tests": 8,
               "unsupported_number_tests": 1, "context_mode_impact": {}}
}
```

- `results_summary` 是 `AblationTestSuite` 原始 results 的**裁剪视图**（剔除 final_answer_preview/reasoning 等大字段；完整字段经 `/api/report/{job_id}` 的 MD 报告承载）。
- 轨迹不落完整 `api_turns`（体量大）；只回传计数。与 CLI 的 `task_result_*.json` 全量落盘互补，不重复。

## 4. 关键设计决策（ADR）

| # | 决策 | 为什么 | 被否方案 | 后果 |
|---|---|---|---|---|
| D1 | 消融执行 = 后台线程 + 内存 JobStore + 前端轮询（1.5s 间隔） | 一轮 5 模式×2 案例可达 10-30 分钟，同步 HTTP 必超时 | WebSocket/SSE（引入双向复杂度，轮询足够）| 进度粒度 = 模式×案例级；刷新页面后可继续轮询 |
| D2 | 单飞任务：同一时刻至多一个执行流（单任务或消融 job），后来者 `409` | 全局锁若只串行不拒绝，单任务会排在 20 分钟的 job 后面，前端表现成"假死"；本地单用户下显式拒绝比排队诚实 | 请求排队（等待不可预期）| UI 需在 job 运行时禁用全部执行按钮并显示运行信息 |
| D3 | Job 内部不再嵌套全局锁；job 持有"执行权"直到结束 | 套件顺序执行模式×案例，本就是单线程 | 每测试粒度持锁 | 单任务/消融互斥由 app 层保证，runner 层无锁 |
| D4 | 消融复用 `main.AblationTestSuite`，报告复用 `generate_report` 文本，**不落盘 PNG/JSON/MD** | 消融语义单一来源；图表职责移交 Chart.js；避免污染工作目录（CLI 的落盘行为原样保留给 CLI） | GUI 自建套件（两套语义漂移） | 套件内部 `print` 输出与 `time.sleep(2)` 节流被原样继承——print 经 `redirect_stdout` 捕获进 job 日志缓冲，作为进度页"运行日志"展示 |
| D5 | 无 mock：执行前 `resolve_backend` 预检，失败返回 400 并**指名缺失的环境变量** | 与 CLI 行为一致（用户明确选择）；注册表的报错信息已足够精确 | 回放样板轨迹 | 无 key 时 UI 仍可浏览（providers/samples 可拉取），仅执行被拒 |
| D6 | 视觉系统：CSS design tokens + 玻璃拟态 + Chart.js 暗色预设；动效仅 CSS（渐变位移/悬停浮起/脉冲/弹入），零 JS 动画库 | "炫酷"约束下的最小技术面；Chart.js 单文件 vendor 保离线 | 粒子背景（用户已否）；Tailwind/构建链（违反零构建） | 需维护一份 ~40 行 token 清单；Chart.js v4 约 200KB |
| D7 | `providers` 端点完全派生自 `agentbook.providers` 注册表（key 变量名、是否已设、默认模型、requires_key） | 新增 provider 零改动出现在 GUI（与 CLI 的 `providers` 命令同源） | 硬编码清单 | 不回传任何 key 值，只回传布尔 |
| D8 | 已知风险继承：`code_interpreter` 的 exec 沙箱可逃逸（工程分析问题 2，暂缓修复） | GUI 不改变威胁模型——仍是本地用户、自己的 key、自己的任务；与 CLI 暴露面一致 | 在 GUI 层单独禁用该工具（会造成 GUI/CLI 行为分叉，违背"全功能对齐"） | README 需提示：GUI 仅限本机使用 |
| D9 | 单任务回传**逐轮实发上下文**（每轮请求的完整消息列表，逐消息截断 4000 字符、上限 20 轮） | 实验 1-1 的本体是"模型决策时看到了什么"——只回传 api_turns 计数会让 no_tool_results 的空观察、no_history 的消息裁剪、no_tool_calls 的无工具声明全部不可见，GUI 便无法展示模式间的真实差异（首版教训） | 只回传计数（首版做法，已被用户指出违背实验目的）；全量 api_turns（单条 tool result 可达整份 PDF 文本） | 消融套件仍不回传逐轮上下文——main.py 的 run_single_test 不保留轨迹对象，回传需改 main.py，违反零改动约束；模式差异的细查走单任务路径 |
| D10 | 上下文视图包含**思考内容**，分请求侧与响应侧 | 思考在两处出现，含义不同：① assistant 消息回放进上下文的 `reasoning_content`——它是"模型这轮看到的上一轮思考"，no_reasoning 模式下该字段消失，模式差异直接可辨；② `api_turns.response` 里模型本轮刚产出的思考——完整阅读每轮推理过程的材料。只展示其一都会被问"思考在哪" | 只回放请求侧（第 1 轮无历史思考，面板仍空白）；依赖 trajectory.reasoning_steps 计数（无内容） | 两侧均截断（请求侧随消息 4000、响应侧 2000）；消融套件不回传思考（同 D9 边界） |

## 5. 错误处理映射表（实现权威对照）

| 源头 | 识别方式 | HTTP | 前端呈现 |
|---|---|---|---|
| 参数校验失败 | pydantic | 422 | toast 显示中文 detail |
| 无 key / provider 不可用 | `resolve_backend` 抛 `ValueError`（含变量名）| 400 | 执行面板红色提示，附"去 xxx 设置后重启"指引 |
| 已有任务运行中 | JobStore 状态查询 | 409 | 提示当前 job 的进度位置，禁用执行入口 |
| 单任务业务失败 | `result.error` / `completed=False` | 200 + `status=error` | 答案区红框 + error 原文 |
| 消融 job 内单测试失败 | results 中该条 `completed=false` | 200（job 继续） | 矩阵单元格 ✗ + 悬停显示原因 |
| 未预期异常 | 统一 exception handler | 500 | toast + 后端日志 |
| 轮询不存在的 job | JobStore 查询 | 404 | 前端停止轮询并提示 |

## 6. 生命周期

**单任务**：校验 → 预检 key → acquire 执行权 → `ContextAwareAgent.execute_task` → grounding 评估（复用 `assess_groundedness` + `observation_quantities`，输入取自 api_turns 实发消息）→ 释放 → 写入历史 → 同步返回。

**消融 job**：`POST /api/ablation` 校验+预检 → 占用执行权 → 创建 JobRecord(running) + 启动线程 → 线程内：`redirect_stdout` 捕获套件输出 → `run_ablation_study(modes, cases=get_ablation_cases(n))` → `analyze_results` → JobRecord 置 completed，results/analysis/报告文本入 store → 释放执行权。每个测试完成后更新 `progress`（套件无回调钩子，**进度通过轮询端点重读 results 列表长度**推导，不改 main.py）。

**轮询**：只读 JobStore 快照（浅拷贝 results），无锁竞争。

## 7. 测试策略

- TestClient 不触网：providers/samples/defaults 契约；单任务 monkeypatch `agent_runner.run_single_task`（参数透传 + status 映射 + grounding 字段）；job 生命周期用 fake 套件（毫秒级、2 案例断言进度 done/total 递增、报告端点返回 MD 文本）；409 并发拒绝；404 轮询；历史 FIFO。
- 既有 52 测试零回归 + CLI 行为不变为硬门槛。

## 8. 演进触发点（任一出现 → 另写详细设计）

1. 前端需要多页/路由或组件框架
2. 消融并行化（多 provider 同时跑）或持久化历史
3. WebSocket 推送替代轮询
4. 上游把 context 收进 CI 矩阵（webui 测试需符合其 lint 门禁）
5. 本文超 150 行
