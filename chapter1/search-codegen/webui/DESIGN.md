# search-codegen WebUI 设计

版本：v0.2 · 2026-11-01 · SSE 扩展已确认、已实现（离线验证）；v0.1 已实现
关联：[SPEC.md](SPEC.md)（v0.1 已确认；v0.2 SSE 已确认）

## 1. 核心决策

复用 context/webui 的分层和视觉规范，不导入其 runner、会话类型或消融逻辑。所有联网执行仍交给后台线程。v0.2 在 Agent 与实验函数增加可选事件回调，WebUI 使用 SSE 订阅；config.py、main.py 和实验校验语义保持不变。旧 CLI 默认行为兼容，最终结果仍为历史与导出的权威记录。

使用单进程、单 worker 启动，默认 127.0.0.1:8001。禁止以多 worker 方式部署：内存状态和互斥锁不跨进程。生产部署与持久化不在范围内。

## 2. 模块职责

| 文件 | 职责 |
|---|---|
| webui/app.py | FastAPI 路由、SSE StreamingResponse、启动参数、静态文件、错误转换、下载 |
| webui/schemas.py | Pydantic 请求模型、枚举、长度和参数校验 |
| webui/agent_runner.py | Config 与 Agent 对接、流回调绑定、dry-run、单轮调用、实验顺序执行 |
| webui/state.py | 会话/历史/任务内存存储、终态与事件发布顺序、执行占用、容量回收 |
| webui/streaming.py（新增） | 供应商事件归一化、脱敏流快照、有界事件日志、SSE 编码与游标回放 |
| webui/job_manager.py | 线程启动、状态转换、异常收尾与执行令牌释放 |
| webui/serialization.py | 结构化投影、脱敏、JSON/Markdown 导出、体积限制 |
| webui/static/index.html / app.js / style.css | 中文三页签、会话侧栏、EventSource 生命周期、增量与最终结果呈现 |
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

任务快照不包含整份原始响应，只返回摘要、状态、result_ref、stream_preview、last_event_id 和 progress={done,total,current_backend}。stream_preview 是有界、脱敏的当前预览，不等同最终完整证据。单轮不返回百分比；实验的 done 为已结束后端数（含失败），total 为选定后端数。

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
| GET /api/jobs/{id} | 200 轻量快照及增量预览；完成后通过 result_ref 获取详情 |
| GET /api/jobs/{id}/events | text/event-stream；支持 after 与 Last-Event-ID；详见第 13 节 |
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

前端以 EventSource 为主，终态拉取一次最终详情并关闭订阅。初次加载 GET /api/jobs 恢复活动任务，然后订阅当前快照与后续事件。SSE 连续失败时才降级到递归 setTimeout 状态查询，避免 SSE 与高频轮询并行；重连、游标与去重规则见第 13 节。localStorage 仍只保存选中记录 ID，刷新后通过流快照重建。

运行时禁用全部联网提交入口，仍可看历史或无会话预览。首次实验点击明确列出所选后端及多次调用提示，由用户按“开始实验”提交。前端 elapsed 由服务端时间基准展示，不将计时器当成模型活跃信号。

## 11. 验证与交付顺序

先实现 schemas/state/runner 与离线测试，再接 API，最后接页面。所有测试 mock 模型调用，不改 .env、不自动运行付费实验。

- 单元：显式 backend 与专属域名请求、预览不改状态、失败恢复 response_id、api_turns 切片、Token 缺失、脱敏和容量回收。
- 并发：用线程 Event 控制假调用，验证抢占原子性、409、轮询不阻塞、启动失败/异常释放令牌。
- 路由：TestClient 验证参数、缺 Key、状态机、历史/导出、404、运行记录保护、同源限制。
- 实验：两个后端一成功一失败仍完成报告、真实 done、既有 acceptance 语义与诊断资格一致。
- 前端：离线假接口验证恢复轮询、历史回看、工具状态缺失、XSS、Markdown、折叠及窄屏。不得把 UI 测试替身做成产品的演示模式。
- 回归：运行既有后端/Responses/配置测试与新增 WebUI 测试；验证 CLI dry-run 行为未变。

v0.1 已确认并实现，见 VALIDATION.md。v0.2 SSE 已确认并实现，离线验证见 VALIDATION.md，不自动提交、推送或合并。

## 12. 输入缓存展示补充（2026-10-29）

每轮与实验单项复用 metrics：读取 usage.input_tokens_details.cached_tokens，兼容 prompt_tokens_details.cached_tokens；非缓存输入为 input_tokens 减缓存命中。仅接受非负安全整数，并检查命中数不超过总输入；字段缺失或异常时不推算，不将未知当零。此展示不更改原始 usage、缓存策略或计费规则，缓存命中已包含在总输入中。

## 13. SSE 增量链路（v0.2 已实施）

### D13.1 兼容接口与改动范围

数据方向：供应商 SSE → Agent 帧解析 → 可选 on_event 回调 → runner 绑定 job/阶段 → streaming 归一化与缓冲 → GET events → EventSource → 页面。

- agent.py：process_request 和内部请求构造增加末尾可选关键字 stream: Optional[bool]=None、on_event: Optional[Callable]=None；旧参数位置和返回结果契约不变。stream=None 保持原有默认（百炼流式，其他现状）；WebUI 显式 stream=True，preview/dry-run 也使用该值。回调只用于观察，不负责执行工具或网络重发。
- run_experiment_1_3.py：run_backend 在原两个参数之后增加仅关键字 stream=None、on_event=None；三次可能的调用分别标注阶段 asean / clarification_first / clarification_followup。不改固定任务、模型选择、验收函数或原返回结构。
- webui/agent_runner.py、job_manager.py、state.py、app.py：绑定回调、登记预览、发布原子终态、开放订阅；新增 webui/streaming.py 集中承载事件转换与缓冲。
- static/app.js / index.html / style.css：EventSource 管理、流式正文和工具状态区、重连与未完成标记。新增 Agent 帧解析、SSE 路由和前端离线测试；同步 README/VALIDATION 留到实现阶段。
- 不导入 context 工程正在开发的 streaming 模块，不变更其文件；这两套 Agent 的协议和数据结构不同。

### D13.2 上游 SSE 解析与事件归一化

Agent 按 SSE 帧解析，不能假定一次 TCP chunk 等于一个事件。支持 UTF-8 中文、CRLF、注释、event 字段及多行 data，在空行时提交完整帧；忽略空/不识别帧但保留已识别事件计数。读取 chunk 大小应足以低延迟投递，避免 requests.iter_lines 默认缓冲将小片段长期攒住。

以 response.output_text.delta 累积正文，按 response_id（可空）+ item_id/output_index + content_index 分块；response.output_text.done 用于校准对应块，不能在已有 delta 后重复追加完整文本。response.output_item.added/done 及供应商真实工具状态用于更新调用卡片，不把 item added 一律视为工具已执行。工具参数/输出碎片在结构完成前不透传浏览器。

response.completed 获取最终 response 后即可结束读取，不再等待连接自然关闭；response.failed、response.incomplete、流内 error 或读取超时按失败收尾。[DONE] 或 EOF 没有可用终态响应不得视为成功。已有流计数、raw_response、api_turns 与结构化错误保持兼容。

未识别的供应商事件不能作为界面事实；最终响应里出现但中途未提供的工具项在结束时补齐。百炼基础映射依据[官方 Responses 流式事件说明](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-responses)；其他后端映射在实施时对照各供应商官方事件与离线 fixture 验证，不假设所有后端帧完全相同。

回调异常不重试模型请求；记录脱敏的 observer_error 并关闭该流的增量发布，继续读取最终模型响应。不得因展示层异常破坏已经产生费用的调用；最终 job 仍根据真实结果判定。

### D13.3 本地事件协议

GET /api/jobs/{job_id}/events?after={seq}，Content-Type 为 text/event-stream; charset=utf-8，Cache-Control=no-cache, no-transform，X-Accel-Buffering=no；不设置 Content-Length，不对该路由应用可能缓冲 SSE 的压缩。SSE 路由不能被通用 no-store 赋值覆盖冲突（no-store 也可保留为附加指令）。

标准业务帧使用 id: 单调递增的 job 内整数、event: 名称、data: 单行 JSON，最后空行。序号由本地 job 分配，不使用上游 sequence_number 作为跨阶段游标。payload 含 job_id、owner_id、turn_id（就绪后提供）、backend、stage、timestamp；文本项附 item_key 和 revision。

| event | payload 与语义 |
|---|---|
| job.state | queued/running、开始时间及已用时；不是模型成功信号 |
| answer.snapshot | 当前阶段、输出项及其有界全文预览 text、revision、truncated；是 delta 累积后的替换快照，不能重复 append |
| tool.state | call/item 标识、真实 type/status、已完成且可公开的脱敏字段；不携带未解析参数碎片 |
| experiment.progress | done/total/current_backend/stage；保持既有后端计数语义 |
| stream.reset | 当前有界流快照、through_seq、job 状态及 result_ref；替换客户端预览，用于首次订阅和游标过期恢复 |
| job.done | status=completed 或 error、result_ref、脱敏 error；此时完整最终结果已可读取 |

不使用名为 error 的业务事件，避免混淆 EventSource 的传输 onerror。流式期间不猜测 Token 数，最终 usage 保留缓存命中与非缓存输入拆分。heartbeat 为每 15 秒一次 SSE 注释（无 id），不修改正文、不证明模型仍在计算。

### D13.4 发布顺序、游标与订阅

提交与模型执行仍由原后台 job 驱动。事件 GET 是纯订阅，不能创建 Agent 或启动 process_request。即使模型先于订阅完成，客户端也能取得终态与 result_ref。

同一 job 的流状态和序号在锁内提交。最终流程固定为：保存完整结果/失败证据 → 应用容量规则 → 更新 job 与会话终态及续接状态 → 发布一次 job.done → 释放执行占用；SSE 读者不能先看到 done 再读到 running 的历史。上游 response.completed 不直接发布 job.done。

- after 为初次/手动重建连接的游标，Last-Event-ID 用于浏览器自动重连；同时存在时优先有效的 Last-Event-ID。游标必须为非负整数，格式无效返回 422，均不得触发模型重发。
- 有效且仍保留的游标：按 seq > cursor 顺序回放，再订阅实时事件。无游标、游标落后日志起点或大于当前 seq：发 stream.reset，包含当前 through_seq（同时作为该帧的 id），随后只发更高 seq。reset 是状态覆盖，不是可追加的正文片段；客户端必须接受同游标的恢复快照。
- 在一个临界区内取回放边界与快照，然后根据 seq 持续补读，避免“回放结束到注册订阅”之间丢事件。客户端按 job_id/seq 去重；按 item_key/revision 替换正文块。
- 已结束 job 在回放/快照之后发送或确认终态并关闭连接；前端得知终态后主动 close，避免 EventSource 因正常 EOF 再连接。终态事件未被接收时可用状态 GET 判定并加载完整结果。
- job 不存在、服务重启或被淘汰时在响应开始前返回 404；订阅开始后被回收则停止连接，客户端查询 404 后停止重试并提示记录已失效。

SSE 使用异步生成器；每 50 ms 异步检查有界事件日志（asyncio.sleep），不创建每客户端无界队列，不阻塞 ASGI 事件循环，不在 yield/网络写入时持有 Store 锁。finally 注销订阅；取消订阅不取消 worker。每 job 最多 4 个订阅、全局最多 8 个，超限返回 429，不改变执行状态。

### D13.5 有界缓冲、快照与脱敏

每 job 维护最多 512 条且合计不超过 2 MiB 的脱敏事件环形日志，超限丢弃最旧事件，不阻塞模型读取。正文快照按每 100 ms 至多一次合并发布，状态切换与终态及时发布，终态前冲刷待发布预览。慢消费者按自己的游标补读；已错过缓冲则 reset，不维护无界的每客户端队列。

每 job 流预览总预算 1 MiB、单帧不超过 128 KiB；answer.snapshot 超限只保留明确标注 truncated 的可展示前缀，仍继续读取上游并保留既有容量内的最终完整证据。工具/阶段数量超出流预览预算同样明确标记 preview_truncated，终态以完整报告校准。正文私有工作区最多 16 块、每块 12000 字符（UTF-8 总量小于 1 MiB），达到上限后停止追加该块并明确标记截断；被截断的尾部持续保留脱敏安全窗口。活动 job 的环形日志、预览和正文工作区合计最多增加 4 MiB；结束后清除私有正文，缓冲最多 3 MiB/job，结束后只保留最近 4 个 job 的实时缓冲；更早但历史仍存在的 job 用终态摘要和 result_ref 恢复。owner 删除、FIFO 淘汰时释放对应流资源，不另建无界存储。

脱敏必须先于进入公共事件日志。对正文逐块累计后进行安全投影，已配置密钥按最长匹配处理；为避免密钥被 delta 拆开后逐片泄露，每个文本块保留至少“最长已配置密钥长度减一”的未发布尾部，在完成/失败时经完整替换后冲刷。累计正文中的秘密仍整体替换，不能只对独立 delta 做字符串替换。测试须包含密钥跨两段/多段、中文和多行。

工具状态使用字段白名单；原始供应商事件、认证头、未解析代码/参数片段不直接发布。未识别事件只增加内部统计，最终原始证据仍走既有脱敏规则。流式预览不包含推测的思考内容。前端生成中用 textContent；终态 Markdown 继续使用 marked + DOMPurify。

### D13.6 前端连接与最终校准

同一页面最多订阅一个 active job。提交获 job_id 后先插入当前用户任务及生成中容器，再建立 EventSource；刷新后从 jobs 获取 owner 并订阅 reset。查看其他历史时保留当前 job 订阅，但只更新对应 job 的后台状态，切回时用其快照恢复，不将文本写入错误会话。

answer.snapshot 覆盖对应输出块，按 output/content 索引排序；不在每个 delta 重建整页或整份 Markdown，页面重绘节流至约 100 ms。若用户正在向上看历史，不强制滚回底部。断线保留已有预览，并显示“连接中断，后台任务可能仍在运行”。

重连由前端统一管理：onerror 先 close 旧 EventSource，再按 2/4/8 秒（上限 10 秒）带最后游标重建；不再同时依赖原连接的自动重连。收到合法 reset/业务帧后清空失败计数，仅触发 onopen 不视为恢复成功。连续 3 次失败或浏览器不支持 EventSource 时启用每秒状态 GET，并显示“流连接不可用，已降级”；降级期间每 10 秒至多一次 SSE 探测，恢复后停止高频 GET。SSE 正常时无每秒状态轮询。

收到 job.done 或 reset 中终态后 close 流，获取最终详情替换预览。成功时渲染完整 Markdown、Token、引用和工具证据；失败时将已接收正文保留为“未完成”预览，不将预览合并到成功答案字段或复用失败 response_id。失败预览仅作为受容量限制的诊断信息记录在 Turn.stream_preview，既有 JSON 导出可包含该字段；Markdown 答案导出对失败轮只写失败标记与错误，不导出未完成正文为成功答案。

正常仅在任务结束后开放完整证据下载。流预览截断与最终 evidence_limit_exceeded 分开显示：前者不代表完整结果丢失，后者遵循原有不可续接/不可完整导出规则。

### D13.7 实施验收

新增离线 fixture 覆盖文本 delta/done、交错工具事件、缺少终态、response.failed/incomplete、中文与多行、碎片/合并帧、未知事件、observer 异常。旧 CLI 默认请求、WebUI dry-run 的 stream=true、实验三阶段标记分别验证。

SSE 路由测试必须证明首个正文帧早于 worker 终态，不可只检查 HTTP 200 或 Content-Type；用可控 producer/消费者验证帧分隔、序号、重放/重置、极快完成、重启 404、心跳、连接上限与慢客户端。测试客户端若会缓冲响应，采用可控异步传输或本机短生命周期测试服务，禁止为通过测试改成最终一次性输出。

前端用可控 EventSource 替身测试去重、覆盖快照、刷新恢复、阶段隔离、失败预览、最终替换、退避、查询降级与连接清理；既有 54 项 Python/5 项前端基线作为历史参考，实施后的数量与结果另行记录，不能预先标记通过。所有验证不发起真实模型或付费调用。
