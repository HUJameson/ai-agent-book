# web-search-agent Web GUI 设计决策记录（DESIGN）

| 项 | 值 |
|---|---|
| 版本 | v0.1（2026-10-13） |
| 关联文档 | [SPEC.md](SPEC.md)（需求与验收）· [../README.md](../README.md)（CLI 用法） |
| 维护约定 | 改设计 → 先改本文对应条目 → 再改代码 → 测试守护；本文保持 <150 行，超限说明应升级为详细设计（见第 8 节） |

## 1. 文档定位

三层分工：**SPEC 管"做什么/验收"，本文管"怎么做/为什么这么做"，微观实现交给代码 docstring 与测试。** 只记录代码里看不出来的决策与契约；能从代码直接读出的内容一律不写，避免文档腐烂。

## 2. 模块职责与依赖方向

| 文件 | 职责 | 明确不做 |
|---|---|---|
| `webui/app.py` | FastAPI 路由、静态文件挂载、uvicorn 入口、全局锁 | 业务逻辑、直接 import `agent` |
| `webui/agent_runner.py` | **唯一**与 `agent.py`/`config.py` 对接的模块：按参数构建 `WebSearchAgent`、执行问答、组装响应、错误转换 | HTTP 概念（只返回数据，状态码由 app 层映射） |
| `webui/static/` | 展示层，仅通过 `fetch` 访问 `/api/*` | 任何业务判断 |

依赖方向：`app.py → agent_runner.py → agent.py（既有，不改动）`。禁止反向引用与跨层 import。

## 3. 核心数据契约

历史条目（内存结构，同时是 `/api/ask` 与 `/api/history/{id}` 的响应体）：

```json
{
  "id": "q_20261013T024051Z_ab12",
  "question": "...",
  "answer": "...",
  "status": "success | error",
  "trace": [{"iteration": 1, "type": "thought|action|observation|answer", "...": "..."}],
  "params": {"provider": "kimi", "model": "kimi-k3", "max_steps": 8,
             "temperature": 0.6, "timeout": 180},
  "elapsed_seconds": 96.3,
  "created_at": "2026-10-13T02:40:51Z"
}
```

- `id`：UTC 时间戳 + 4 位随机后缀，进程内唯一即可。
- `trace` **直接复用** `agent.get_trace()` 的结构，不做字段转换——保持"轨迹单一事实来源"，CLI 与 GUI 展示语义一致。

## 4. 关键设计决策（ADR）

| # | 决策 | 为什么 | 被否方案 | 后果/代价 |
|---|---|---|---|---|
| D1 | 每请求新建 `WebSearchAgent` 实例 + 模块级 `threading.Lock` 串行执行 | 实例含可变状态（`conversation_history`/`trace`），共享会互踩；本地单用户串行延迟可接受 | 单例复用；异步改造 | 高并发不可能；换取零状态竞争与最简实现 |
| D2 | 历史仅内存：`list`（时序）+ `dict`（id 索引），FIFO 上限 100 条 | 教学场景无持久化需求（YAGNI）；上限防长回答撑爆内存 | SQLite/JSON 落盘 | 进程重启即失；>100 条丢最旧 |
| D3 | 同步阻塞 API（`POST /api/ask` 等到完成才返回），非 SSE/WebSocket | Spec 已定"完成后一次性显示"；agent 无流式接口，改动 agent.py 违反范围约束 | SSE 增量推送 | 前端需 loading 提示 1-3 分钟；将来做流式时此接口可保留为降级路径 |
| D4 | vendor `marked.min.js`（单文件入 `static/`） | 离线可用、答案含链接/列表需真 Markdown 渲染、零构建链 | CDN 引入（断网失效）；纯文本（阅读体验差） | 需在升级时手动换文件 |
| D5 | 参数（timeout 等）通过"重建实例"生效 | `Config.SEARCH_TIMEOUT` 在 `__init__` 读取，是既有代码约束；不改 agent.py | setter 透传（需改 agent.py） | 每次提问多一次实例构造（廉价，无 IO） |
| D6 | 错误双轨制：参数错→HTTP 4xx+`detail`；agent 业务兜底（超时/迭代上限/无信息）→HTTP 200+`status=error` | 业务兜底文案是 agent 的正常返回值（`is_failure_answer` 可判），硬套 5xx 会让前端无法区分"服务坏了"与"这次没答好" | 全部 500 | 前端必须同时处理两轨（见第 5 节） |
| D7 | API Key 只存在于服务端环境变量/.env，前端永不经手、不回显 | 复用 `config.py` 既有解析（含 OpenRouter 兜底）；防 key 泄漏进浏览器历史/日志 | 表单输入 key | GUI 无法临时注入 key，只能改环境后重启 |
| D8 | 零构建前端：原生 HTML/CSS/JS | 教学仓库可读性优先，避免 node 工具链进 Python 仓库 | Vue/React + Vite | 复杂交互（虚拟滚动等）实现成本高；出现该需求即触发第 8 节升级 |

## 5. 错误处理映射表（实现时的权威对照）

| 源头 | 识别方式 | HTTP | `status` | 前端呈现 |
|---|---|---|---|---|
| 参数校验失败 | pydantic/FastAPI | 422 | — | 红色 toast 显示 `detail` |
| `provider=kimi` 但无任何可用 key | `agent_runner` 预检环境变量（MOONSHOT/KIMI/OPENROUTER） | 400 | — | 提示"配置 key 或切换 offline-demo" |
| agent 业务兜底（超时/迭代上限/无信息） | `is_failure_answer(answer)` | 200 | `error` | 答案区红框显示 answer 原文 |
| 速率限制/超时增强提示 | 复用 `agent._error_hint` 语义 | 200 | `error` | 同上，含"调大 timeout"建议 |
| 未预期异常 | `app.py` 统一 exception handler | 500 | — | toast + 后端日志 traceback |

## 6. 请求生命周期（`POST /api/ask`）

```
校验参数 → provider 预检(key/offline-demo) → 构建参数 dict
  → acquire(lock) → 新建 WebSearchAgent → search_and_answer / run_offline_demo
  → release(lock) → is_failure_answer 判 status → 写入历史(list+dict, FIFO 淘汰)
  → 返回完整 HistoryEntry
```

历史查询：`GET /api/history` 只回摘要字段；详情按 id 查 `dict` 索引。

## 7. 测试策略

- `TestClient` 全程**不触网**：offline-demo 全链路、参数校验 422、无 key 预检 400、历史增删查、FIFO 淘汰。
- `kimi` 在线路径用 monkeypatch 替换 `agent_runner` 内的 agent 构建函数，断言参数透传与 status 判定。
- 既有 CLI 测试零回归是硬门槛。

## 8. 演进触发点（出现任一项 → 停止追加本文，新写详细设计）

1. 前端多页面/路由化，或引入前端框架
2. 历史持久化（磁盘/数据库）或多用户
3. 流式推送（SSE/WebSocket）
4. `/api/*` 契约需提供给第三方前端复用
5. 本文超过 150 行或 ADR 表出现相互矛盾条目
