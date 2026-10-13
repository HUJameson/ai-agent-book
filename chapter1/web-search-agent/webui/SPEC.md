# web-search-agent Web GUI 需求规格（Spec v0.1）

## 1. 背景与目标

现有工程仅支持命令行交互（`main.py`）。目标是在**不破坏现有 CLI 行为**的前提下，新增一个本地运行的 Web GUI，让用户在浏览器中提问、查看 ReAct 轨迹（💭 思考 → 🔧 行动 → 👀 观察 → ✅ 答案）、调整参数并回看历史问答。

## 2. 需求澄清记录（已确认）

| 决策点 | 选择 |
|---|---|
| 技术栈 | FastAPI + 原生 HTML/JS（前后端分离，无前端构建链） |
| 功能 | ReAct 轨迹展示、参数设置面板、会话历史列表（**不含** JSON 导出） |
| 轨迹展示方式 | 完成后一次性渲染（不做流式，不改 agent.py 现有接口） |
| 代码组织 | server + client 分离（独立 `webui/` 目录） |
| 依赖管理 | 可选依赖，不进主 `requirements.txt` |
| 界面语言 | 中文 |

> 说明：需求澄清时同时勾选了"ReAct 轨迹实时展示"和"完成后一次性显示"。按后者理解：**轨迹内容完整展示（不省略步骤），但渲染时机为完成后一次性**（非流式推送）。

## 3. 范围

**做**：本地单用户 Web 界面、复用 `WebSearchAgent`、支持 `kimi` 与 `offline-demo` 两种 provider。

**不做**：鉴权/多用户、HTTPS、公网部署、流式 SSE、数据库持久化、修改 `agent.py`/`main.py` 现有逻辑。

## 4. 架构与代码组织

```
webui/
├── app.py              # FastAPI 应用（API 路由 + 挂载静态文件 + uvicorn 入口）
├── agent_runner.py     # 封装：按参数构建 WebSearchAgent、执行问答、组装响应
├── requirements.txt    # 仅 fastapi、uvicorn（可选依赖）
├── SPEC.md             # 本文档
├── DESIGN.md           # 设计决策记录（怎么做/为什么）
└── static/
    ├── index.html      # 单页应用
    ├── style.css
    ├── app.js          # fetch 调用 API、渲染
    └── marked.min.js   # vendor 的 markdown 渲染（离线可用，零构建）
```

- 启动：`python -m webui.app`（内部 `uvicorn.run`，默认 `127.0.0.1:8000`），启动后打印访问地址。
- API Key 一律由服务端从环境变量 / `.env` 读取（复用 `config.py` 的 `load_dotenv` 与解析逻辑），**不经过前端传输**。
- 每次问答请求新建独立 `WebSearchAgent` 实例（实例含可变状态，避免共享），后端加全局锁串行执行，本地单用户足够。

## 5. API 设计

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/` | 返回 `static/index.html` |
| `POST` | `/api/ask` | 提问。请求体 `{question, options:{provider, model, max_steps, temperature, timeout}}`；同步等待，返回 `{id, question, answer, trace, status, elapsed_seconds, params, created_at}`。`trace` 结构与 `agent.get_trace()` 一致 |
| `GET` | `/api/history` | 会话历史列表（摘要：id/question/status/created_at） |
| `GET` | `/api/history/{id}` | 单条详情（含完整 trace 与 answer） |
| `DELETE` | `/api/history` | 清空历史 |
| `GET` | `/api/defaults` | 返回默认参数（来自 `Config`），用于初始化面板 |

- 错误约定：非 200 时返回 `{"detail": "<中文错误>"}`；业务层失败兜底文案（超时/迭代上限/无信息）复用 `is_failure_answer` 判定，`status` 标为 `error` 而非 500。
- `provider=offline-demo` 时走 `run_offline_demo()`，无需 API Key。

## 6. 功能需求

**F1 问答**：顶部问题输入框 + "提问"按钮；请求期间按钮禁用并显示加载状态（提示"kimi-k3 单次可能需要 1-3 分钟"）；答案以 Markdown 渲染（链接可点击）。

**F2 ReAct 轨迹展示**：答案下方按迭代顺序渲染步骤卡片（icon + 标签 + 迭代号，样式对齐 CLI 的 `format_trace_step`）；`observation`（常含大段加密块）与 `thought` 默认折叠、点击展开；`answer` 高亮置底。

**F3 参数设置面板**：侧栏表单，含 provider（kimi / offline-demo）、model（默认 `kimi-k3`）、max_steps（1-10）、temperature（0-2，推理模型自动强制 1 的逻辑由 agent 内部处理）、timeout 秒。修改后对下一次提问生效；从 `/api/defaults` 初始化。

**F4 会话历史列表**：侧栏按时间倒序显示历史条目，点击回看该次的答案+轨迹；支持一键清空。历史保存在服务端内存（进程重启即失，不落盘）。

## 7. 非功能需求

1. 仅监听 `127.0.0.1`，无鉴权（本地教学用途）。
2. 零前端构建：原生 HTML/CSS/JS，唯一第三方为 vendor 的 `marked.min.js`。
3. 后端对 agent 异常统一捕获，转中文提示（含 429/超时区分，语义对齐 `_error_hint`）。
4. 现有 CLI 与全部既有测试不受影响；`agent.py` 若需改动仅允许新增可选参数且不改变默认行为（预期无需改动）。

## 8. 依赖与测试

- `webui/requirements.txt`：`fastapi`、`uvicorn`（安装说明写入 webui README 段落，不动主依赖文件）。
- 新增 `webui/test_app.py`（或 `tests/test_webui.py`）：用 FastAPI `TestClient` 覆盖 `/api/ask`（offline-demo）、历史增删查、参数校验；不触网。

## 9. 验收标准

1. `pip install -r webui/requirements.txt && python -m webui.app` 后访问 `http://127.0.0.1:8000` 正常显示界面。
2. 无 Key 时选 `offline-demo` 可完整走通提问→轨迹→答案。
3. 有 Key 时 `kimi` 模式提问，完成后一次性显示答案与完整轨迹，内容与 CLI 版一致。
4. 参数面板修改 max_steps/temperature/provider 后，下次提问生效（轨迹中迭代次数受 max_steps 约束）。
5. 历史列表正确增删查、刷新页面后可回看（进程存活期间）。
6. 无 Key 直接用 kimi 模式提问 → 界面显示中文错误提示，不崩溃。
7. `pytest` 全部通过（含新增 webui 测试），既有测试零回归。

## 10. 已确认的默认决定

1. 历史仅内存保存、不持久化磁盘 —— 已接受。
2. Markdown 渲染 vendor `marked.min.js`（约 40KB 单文件）—— 已接受。
3. 默认端口 8000 —— 已接受。
