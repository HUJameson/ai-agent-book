# search-codegen WebUI 设计

版本：v0.1 · 2026-10-28 · 用户已确认，已实现（离线验证）
关联：[SPEC.md](SPEC.md)（用户已确认）

## 1. 核心决策

复用 context/webui 的分层和视觉规范，不导入其 runner、会话类型或消融逻辑。所有联网执行交给后台线程；首版不改 agent.py、config.py、main.py 及实验校验算法，不新增实时事件回调。显示真实任务状态与耗时，工具证据在请求结束后更新。

使用单进程、单 worker 启动，默认 127.0.0.1:8001。禁止以多 worker 方式部署：内存状态和互斥锁不跨进程。生产部署与持久化不在范围内。

## 2. 模块职责

| 文件 | 职责 |
|---|---|
| webui/app.py | FastAPI 路由、启动参数、静态文件、错误转换、下载 |
| webui/schemas.py | Pydantic 请求模型、枚举、长度和参数校验 |
| webui/agent_runner.py | Config 与 Agent 对接、dry-run、单轮调用、实验顺序执行 |
| webui/state.py | 会话/历史/任务内存存储、快照、执行占用、容量回收 |
| webui/job_manager.py | 线程启动、状态转换、异常收尾与执行令牌释放 |
| webui/serialization.py | 结构化投影、脱敏、JSON/Markdown 导出、体积限制 |
| webui/static/index.html / app.js / style.css | 中文三页签、会话侧栏、轮询与结果呈现 |
| webui/static/vendor/ | 本地 marked 与 DOMPurify，保留许可证 |
| webui/requirements.txt / README.md | 独立依赖和启动说明 |
| tests/test_webui.py | 离线路由、状态与业务对接测试 |

依赖方向：app → job_manager/state/runner/serialization；runner → 既有 Agent/Config/实验函数。state 不依赖 HTTP、模型库或浏览器。静态资源不使用 CDN；首版实验对比用表格，无需 Chart.js。

## 3. 数据契约

标识符使用随机 UUID 前缀，避免会话 ID、任务 ID、历史 ID 混用。所有时间存 UTC ISO8601，浏览器按用户时区展示；耗时用 monotonic 计算。

- Session：id、created_at、updated_at、固定 config、turns[]、active_job_id；私有 agent 引用不进入序列化。
- Config：backend、requested_model（解析后的真实模型）、base_url、use_tools、reasoning、verbosity、max_output_tokens。Key 不在公共 Config 中；Agent 内部持有服务端密钥。
- Turn：id、job_id、input、status、result、api_turns（本轮新增切片）、started_at、finished_at。result 保留 Agent 现有字段。
- Job：id、kind=turn|experiment、owner_id、status、created_at、started_at、finished_at、elapsed_seconds、progress、error、result_ref。
- History：id（对话取 session ID，实验取 experiment ID）、kind、title、updated_at、status、summary；详情单独获取。
- Experiment：id、backends、reasoning、runs[]、acceptance、usage_summary、status。每个 run 保留原函数输出；不会把多后端共享成一个 Agent。

任务快照不包含整份原始响应，只返回摘要、状态、result_ref 和 progress={done,total,current_backend}。单轮不返回百分比；实验的 done 为已结束后端数（含失败），total 为选定后端数。

## 4. 执行权与状态机

state 用一个短时 RLock 保护元数据和 current_job_id。提交时在同一临界区检查空闲、预留 job、登记 session.active_job_id；仅成功预留者启动线程。第二次提交返回 409（附当前 job_id），不阻塞排队。锁不覆盖网络调用、JSON 渲染或长时间导出。

状态机：queued → running → completed|error。线程启动失败也必须清除占用并落为 error。worker 在 finally 释放执行权、清除会话忙标记；result.success=false 归为 error。实验若有执行失败，整体为 error，但仍返回已收集报告；仅验收不通过而请求均正常完成时任务 completed，报告 acceptance.passed=false。

后台线程为 daemon，退出时不保证请求收尾。无取消 API；页面不提供误导性的“停止调用”按钮。断开浏览器不停止请求。所有状态读取返回锁内生成的不可变副本，避免前端读到半更新记录。

## 5. 会话与请求

创建会话时用 Config.resolve(backend, model) 一次解析配置并验证 Key，构造 GPT5NativeAgent(..., backend=backend)。后端/地址/模型固定至会话结束；页面不接受 Base URL 或 Key。启动之后 .env 的修改需重启加载。

提交轮次仅接受 input，不接受重新覆盖配置。记录调用前 api_turns 长度，完成后只取新增轮次。调用前保留 previous_response_id 与 conversation_history 快照；若结果失败，恢复二者到上一成功状态，保留本次失败 api_turns 和 Turn。这防止核心 Agent 在个别非 completed 响应中更新了续接 ID。

preview 不复用并修改运行 Agent：用同配置临时 Agent 和占位 Key，将上一成功 response_id 复制到临时对象，再调用 process_request(dry_run=True)。不发送 API，不追加历史。会话忙时会话预览返回 409；无会话预览可在后台任务期间执行。新会话预览不要求 Key。

限制：输入去除首尾空白后 1–20000 字符，模型名最长 200 字符，max_output_tokens 为正整数且不大于 100000（仅为本地输入上限，不代表模型支持）；reasoning/verbosity 沿用 CLI 枚举。百炼接收控件默认空值，runner 不传与其无关的覆盖参数。无 temperature 字段。

## 6. 实验执行与用量

POST /api/experiments 接受 {backends:[...], reasoning:"low"}，后端去重且数量 1–3。提交前对全部 Key 做本地存在性预检，任一缺失返回 400，未开始时不消耗调用。每个 backend 依次调用既有 run_backend，结束后追加结果并更新 done。

异常捕获在每个后端边界，保存 structured error，再继续下一后端。最终复用 acceptance(runs)。OpenRouter 的诊断资格从既有函数输出读取。UI 将执行状态、各项校验和正式验收分开展示。

用量只累加真实返回值：全部缺失时为 null，部分缺失时标记 partial=true，避免既有 total_usage 将缺失计为零而误导页面。不推算费用；仅在提供真实 cost 且币种可确定时标注来源，首版默认只展示 Token。实验沿用固定任务与默认模型，不提供自定义实验输入或模型覆盖，避免与现有 run_backend 接口不一致。

## 7. 接口细化

| 接口 | 输入/返回与规则 |
|---|---|
| GET /api/defaults | 默认 backend/model、参数、轮询间隔、容量说明 |
| GET /api/backends | 固定三后端及 key_set、key_var、model、base_url、reasoning/verbosity 能力；无 Key 值 |
| GET /api/samples | id/title/input/use_tools；连通性示例 use_tools=false |
| POST /api/preview | {config?,session_id?,input}，config 与 session_id 二选一；200 返回 request/provider/model/base_url/dry_run=true |
| POST /api/sessions | {config} → 201 Session 摘要；本地 Key 校验，不做远程鉴权 |
| POST /api/sessions/{id}/turns | {input} → 202 {job_id,session_id} |
| POST /api/experiments | {backends,reasoning} → 202 {job_id,experiment_id} |
| GET /api/jobs | active 列表，刷新恢复使用；已结束列表从 history 取得 |
| GET /api/jobs/{id} | 200 轻量快照；完成后通过 result_ref 获取详情 |
| GET /api/history | 摘要列表，倒序；新建空会话可见，带 empty 标记 |
| GET /api/history/{id} | 全部对话轮次或实验报告的脱敏详情 |
| DELETE /api/history/{id} | 运行中 409，其余 204；同时释放 owner、jobs、Agent |
| DELETE /api/history | 200 {deleted,retained_active_ids}，保留运行记录 |
| GET /api/exports/{id} | format=json|md；JSON 默认完整 owner 记录，turn_id 可选导出单轮；实验 md 返回 422 |

所有接口使用 {error:{code,message,details?}} 表达 HTTP 错误。参数错误 422、缺 Key 400、未知/已回收 ID 404、忙或会话冲突 409，接收任务 202。任务业务错误存在 job.error 中，轮询仍返回 200。未预期 500 不回传堆栈。

下载仅从记录 ID 生成，不接受任意路径；Content-Disposition 使用固定前缀和安全 ID。API 对缓存设置 no-store。

## 8. 生命周期、容量与完整证据

最多 50 个非活动 owner（会话含空会话/实验），活动 owner 不参与淘汰。对话每完成一轮成为最新记录；创建新 owner 或任务结束后按 FIFO 访问更新时间回收旧 owner 及其 jobs、Agent。jobs 不另外无限保存。

持久化到内存前按 UTF-8 JSON 字节计量：全局已保存证据预算 128 MiB，单 owner 预算 16 MiB。必要时先回收旧的非活动记录。已保留证据在预算内可完整导出；UI 单个折叠字段超过 20000 字符时仅展示预览并提供完整 JSON 下载。

单次结果或累计会话超出单 owner 预算时，生成 evidence_limit_exceeded 终态，释放该 owner 的原始载荷与 Agent，仅保留输入摘要、耗时和“证据未保留，无法完整导出”标记；不得将截断 JSON 当完整记录。该会话不可续接，应新建会话。这个预算限制保存量，不声称限制 requests 或模型 SDK 在解析响应期间的瞬时内存峰值。

## 9. 脱敏与浏览器安全

公共投影显式排除 Agent 私有字段、认证头和环境变量字典。序列化器递归去除 api_key、authorization 等敏感键，并替换服务端已配置的非空真实 Key（包含错误 message/raw_text 中的值）。脱敏覆盖状态、详情、导出及服务日志；不打印请求 headers。

Markdown 使用本地 marked + DOMPurify，禁用危险 HTML、事件属性与 javascript:/data: 链接；JSON/工具描述用 textContent。外链只允许 http/https，使用 rel=noopener noreferrer。没有引用不从正文伪造 citations；没有真实工具完成记录不打 completed 徽章。

FastAPI 仅本地绑定；不启用宽泛 CORS。变更接口要求 application/json（DELETE 同样附该头），校验存在的 Origin 与当前本地站点一致；限制 Host 为本地地址，防止恶意网页跨站控制或 DNS rebinding。默认不添加访问外站的代理接口。

## 10. 前端状态与交互

三个页签：任务对话、请求预览、实验验证。共享设置卡片在会话固定后只读；新建会话可调整。历史点击只切换展示，不发模型请求。每轮详情顺序为工具定义、instructions、input、其他请求参数、响应证据，保持请求与响应分离。

轮询用递归 setTimeout，每次请求结束后安排下一次，避免重叠；正常 1 秒，失败后 2/4/8 秒退避至上限 10 秒，显示“连接中断，后台任务可能仍在运行”。终态拉取一次详情后停止。初次加载 GET /api/jobs 恢复活动任务；localStorage 仅保存选中记录 ID。

运行时禁用全部联网提交入口，仍可看历史或无会话预览。首次实验点击明确列出所选后端及多次调用提示，由用户按“开始实验”提交。前端 elapsed 由服务端时间基准展示，不将计时器当成模型活跃信号。

## 11. 验证与交付顺序

先实现 schemas/state/runner 与离线测试，再接 API，最后接页面。所有测试 mock 模型调用，不改 .env、不自动运行付费实验。

- 单元：显式 backend 与专属域名请求、预览不改状态、失败恢复 response_id、api_turns 切片、Token 缺失、脱敏和容量回收。
- 并发：用线程 Event 控制假调用，验证抢占原子性、409、轮询不阻塞、启动失败/异常释放令牌。
- 路由：TestClient 验证参数、缺 Key、状态机、历史/导出、404、运行记录保护、同源限制。
- 实验：两个后端一成功一失败仍完成报告、真实 done、既有 acceptance 语义与诊断资格一致。
- 前端：离线假接口验证恢复轮询、历史回看、工具状态缺失、XSS、Markdown、折叠及窄屏。不得把 UI 测试替身做成产品的演示模式。
- 回归：运行既有后端/Responses/配置测试与新增 WebUI 测试；验证 CLI dry-run 行为未变。

用户已确认本 DESIGN；实现与离线验证已完成，见 VALIDATION.md。不自动提交、推送或合并。

## 12. 输入缓存展示补充（2026-10-29）

每轮与实验单项复用 metrics：读取 usage.input_tokens_details.cached_tokens，兼容 prompt_tokens_details.cached_tokens；非缓存输入为 input_tokens 减缓存命中。仅接受非负安全整数，并检查命中数不超过总输入；字段缺失或异常时不推算，不将未知当零。此展示不更改原始 usage、缓存策略或计费规则，缓存命中已包含在总输入中。
