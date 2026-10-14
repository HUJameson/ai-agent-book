# context 工程 Web GUI 需求规格（Spec v0.1）

## 1. 背景与目标

`chapter1/context`（实验 1-1：上下文消融）现有 CLI（`main.py`）支持单任务、消融套件与交互模式。目标是在不破坏现有 CLI 的前提下，新增一个**视觉上炫酷**的本地 Web GUI：暗色玻璃拟态风格、图表化消融对比，并让长时间运行的消融套件具备后台执行与实时进度能力。

与 web-search-agent Web GUI（实验 1-2）的关系：**复用其工程模式**（FastAPI server + 原生 JS client 分离、Spec/Design 双文档、内存历史、全局串行锁），**升级其体验**（暗色玻璃拟态 + Chart.js 图表 + 后台任务）。

## 2. 需求澄清记录（已确认）

| 决策点 | 选择 |
|---|---|
| 功能范围 | **全功能对齐**：单任务问答 + 模式切换 + 完整消融套件（矩阵/图表/报告下载） |
| 长任务执行 | **后台任务 + 轮询**：消融套件后台运行，前端实时轮询进度 |
| 离线体验 | **不做 mock**：必须配置可用 API Key（与 CLI 行为一致） |
| 视觉风格 | **暗色玻璃拟态**：深色底 + 玻璃卡片 + 渐变高亮 + 动效 + Chart.js 图表（不做粒子背景） |
| Provider | **GUI 内可切换**：下拉选择全部注册 provider，显示 key 状态与默认模型 |
| 技术栈 | FastAPI + 原生 HTML/JS/CSS（前后端分离，零构建，延续 web-search-agent 模式） |
| 界面语言 | 中文 |
| 代码组织 | `chapter1/context/webui/` 子目录（server/client 分离） |
| 依赖管理 | 可选依赖，独立 `webui/requirements.txt`，不动主 requirements |

## 3. 范围

**做**：本地单用户 Web 界面；单任务 ReAct 执行（5 种上下文模式）；消融套件后台运行（模式子集 × 案例数）；结果表格 + 模式×案例对比矩阵 + Chart.js 图表；Markdown 报告下载；provider 切换与会话历史。

**不做**：mock/回放模式、鉴权/多用户、HTTPS、公网部署、WebSocket（用轮询）、数据库持久化、修改 `agent.py`/`main.py`/`grounding.py` 现有行为、matplotlib 服务端出图（图表职责移到前端）。

## 4. 架构与代码组织

```
chapter1/context/webui/
├── app.py              # FastAPI 路由 + 静态挂载 + uvicorn 入口
├── agent_runner.py     # 唯一 agent 对接层：单任务执行 + 消融套件封装 + 历史存储
├── job_manager.py      # 后台任务：线程 worker + 进度状态 + 单任务队列
├── requirements.txt    # fastapi、uvicorn（可选依赖）
├── SPEC.md / DESIGN.md # 本文档与设计决策记录
└── static/
    ├── index.html      # 单页应用（暗色玻璃拟态）
    ├── style.css       # 渐变/毛玻璃/动效
    ├── app.js          # fetch + 轮询 + Chart.js 渲染
    ├── chart.umd.min.js# vendor Chart.js v4（约 200KB，离线可用）
    └── marked.min.js   # vendor marked v12（答案 Markdown 渲染）
```

- 启动：`python -m webui.app`（默认 `127.0.0.1:8000`，`--port` 可改）。
- 复用边界：单任务直接用 `ContextAwareAgent`；消融套件复用 `main.py` 的 `AblationTestSuite.run_ablation_study` / `analyze_results` / `generate_report`（**程序化调用**，不落 ablation_*.png/json/md 到工作目录——报告文本作为下载响应返回，图表职责移至前端 Chart.js）。
- API Key 只在服务端环境变量/.env（复用 `config.py` + `agentbook.providers` 注册表），前端永不经手、不回显。
- 执行串行化：全局锁串行单任务与消融 job（Agent 实例含可变状态 + 本地单用户 + 速率限制友好）。

## 5. API 设计

| Method | Path | 说明 |
|---|---|---|
| `GET` | `/` | 返回 `static/index.html` |
| `GET` | `/api/providers` | provider 列表：名称、默认模型、key 变量名、key 是否已设置、是否经 OpenRouter 兜底（不含任何 key 值） |
| `GET` | `/api/samples` | 示例任务列表（复用 `get_sample_tasks()`） |
| `POST` | `/api/run` | 单任务同步执行：`{task, context_mode, provider, model?}` → 返回结果记录（final_answer、轨迹、iterations、tool_calls、grounding verdict、耗时、api_turns 计数）。同步等待（单任务通常 10s-3min） |
| `POST` | `/api/ablation` | 启动消融后台任务：`{context_modes[], num_cases, provider, model?}` → `{job_id}`；已有任务运行中时返回 `409` |
| `GET` | `/api/jobs/{job_id}` | 轮询：`{status, progress:{done,total,current}, results[], analysis, matrix}` |
| `GET` | `/api/report/{job_id}` | 下载该消融任务的 Markdown 报告（复用 `generate_report` 文本） |
| `GET` | `/api/history` · `GET /api/history/{id}` · `DELETE /api/history` | 会话历史（单任务与消融任务统一入史），内存保存 FIFO 上限 50 条 |
| `GET` | `/api/defaults` | 默认参数初始化前端 |

- 错误约定：参数错 → `422`（pydantic，中文 detail）；无 key/预检失败 → `400`（中文提示具体缺哪个环境变量）；业务层失败 → `200` + `status=error`；未预期异常 → `500`。
- 轨迹结构直接复用 `AgentTrajectory` 的序列化形态（reasoning_steps / tool_calls / api_turns 摘要），保持与 CLI/验证证据同源。

## 6. 功能需求

**F1 单任务问答**：任务输入框（Ctrl/⌘+Enter 提交）+ 5 个上下文模式**可视化卡片**（图标 + 名称 + 一句话说明，选中态高亮发光）+ 执行按钮动效；结果区分 `completed` / `error`；答案 Markdown 渲染；轨迹按**垂直时间轴**呈现（迭代节点脉冲动效，工具调用卡片显示名称/参数/结果，reasoning 折叠）；显示 iterations、tool_calls、grounding 判定徽章（`UNSUPPORTED` 红色警示）；提供**每轮实发上下文面板**——逐轮展开该次请求实际发给模型的消息列表（角色徽标着色，空 tool 消息红色标注"未回传"，助手消息标注工具调用决策（工具名与输入参数）与是否含思考，长内容截断可辨），使 full / no_tool_results / no_history / no_tool_calls 的模式差异直接可见。面板包含**思考内容**：请求侧回放的 reasoning_content（no_reasoning 模式下不可见，模式差异直接可辨）+ 响应侧的本轮模型思考折叠块。

**F2 消融套件**：模式多选卡片（默认全选 5 种）+ 案例数选择（1-4，来自示例任务）+ provider 选择；启动后进入**进度视图**：模式×案例网格单元实时填充（运行中脉冲动画 → ✓/⚠/✗ 落定弹入）；完成后展示：对比矩阵表、Chart.js 柱状图组（iterations / tool_calls / 耗时 按模式对比，完成率）、grounding 统计；提供报告 `.md` 下载。

**F3 Provider 面板**：下拉切换 provider，实时显示各 provider 的 key 配置状态（✅/❌/“经 OpenRouter 兜底”）；model 可选覆盖输入框。

**F4 历史**：侧栏按时间倒序，单任务与消融任务统一列表（类型徽章区分），点击回看完整结果；支持清空。进程存活期内有效。

**F5 视觉基线（炫酷要求）**：深色底（近黑蓝）+ 玻璃拟态卡片（半透明 + backdrop-filter 模糊 + 1px 渐变描边）+ 主渐变（靛蓝→青→紫）贯穿标题/按钮/高亮 + 悬停浮起 + 关键数字渐变描边 + 加载态动效；零构建，仅 vendor `chart.umd.min.js` 与 `marked.min.js`。

## 7. 非功能需求

1. 仅监听 `127.0.0.1`，无鉴权。
2. 零前端构建；第三方仅 vendor 的 Chart.js 与 marked。
3. 后台任务：线程 worker + 单任务队列（第二个消融任务返回 409）；应用退出不强求任务收尾（本地工具）。
4. 既有 CLI 与 52 个测试零回归；`agent.py`/`main.py`/`grounding.py` 不改动（预期 GUI 层纯增量）。
5. 消融任务与单任务共用全局锁；轮询接口无锁竞争（只读快照）。

## 8. 依赖与测试

- `webui/requirements.txt`：`fastapi`、`uvicorn`（安装说明写入 webui/README）。
- 新增 `chapter1/context/tests/test_webui.py`：TestClient 全程不触网——providers/samples/defaults、单任务（monkeypatch runner）、消融 job 生命周期（启动→轮询进度→完成→报告下载）、409 并发拒绝、422 校验、无 key 400、历史增删查与 FIFO。
- 注意：本工程不在上游 CI 矩阵内，测试以本地 `pytest tests/` 为准。

## 9. 验收标准

1. `pip install -r webui/requirements.txt && python -m webui.app` 后访问 `http://127.0.0.1:8000`，呈现暗色玻璃拟态界面。
2. 无 key 时任意执行路径给出中文错误提示（指明缺哪个环境变量），UI 不崩溃。
3. 有 key 时单任务可执行，轨迹时间轴、grounding 徽章、耗时/迭代统计正确显示。
4. 消融套件后台运行：进度网格实时更新，完成后矩阵 + 三组 Chart.js 图表 + 报告下载可用；运行中重复提交返回 409 且 UI 拦截。
5. provider 切换后单任务/消融任务使用所选 provider（api_turns 证据中 provider/base_url 一致）。
6. 历史增删查正常，刷新页面可回看（进程存活期间）。
7. `pytest tests/` 全部通过（52 既有 + 新增 webui 测试），CLI 行为零变化。

## 10. 已确认的默认决定

1. 历史仅内存、FIFO 上限 50 条（消融结果体量大，较 web-search-agent 的 100 收紧）。
2. 单任务同步等待（与 CLI 体感一致）；消融套件才走后台任务。
3. 图表仅前端 Chart.js；服务端不再产出 PNG（main.py 的 matplotlib 路径保持原样供 CLI 使用）。
4. 默认端口 8000。
