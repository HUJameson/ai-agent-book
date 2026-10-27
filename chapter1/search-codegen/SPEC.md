# search-codegen 后端选择需求规格

版本：v0.1 · 2026-10-27 · 已确认，已实施

## 1. 问题与目标

当前 Agent 按 Base URL 的字符串推断 provider。百炼共享域名包含 `dashscope`，能进入百炼分支；业务空间专属域名形如 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`，被识别为 `custom`。CLI 的 `--backend dashscope` 只选择凭据、地址和模型，没有传入 Agent。

专属域名已经实际调用成功，因此不能表述为“专属域名不可用”或“非流式必定超时”。缺陷是明确选择的后端未决定请求格式，导致百炼的流式及工具配置未被采用；本次不承诺消除所有网络或服务端超时。

目标：明确选择的 backend 优先决定请求格式；URL 推断只作为旧调用方式的兼容回退。

## 2. 行为要求

- Agent 新增可选 backend 参数，支持 `openai`、`openrouter`、`dashscope`；未提供时保留原 URL 推断规则。
- 显式 backend 优先于 URL，包括二者表面不一致的情况；无效 backend 立即抛出清晰的 ValueError，不发送请求。空字符串视为无效值，只有 None 表示未提供。
- CLI 单次、dry-run、交互入口以及多后端实验入口，须把最终选定的 backend 传给 Agent。未显式传入交互入口时，采用 Config.BACKEND。
- quickstart 和手动用例中明确使用 OpenRouter 配置的调用点，须显式传入 openrouter。
- 选择 dashscope 后，共享域名和业务空间域名采用相同的百炼请求格式：stream=true；工具为 web_search 和 code_interpreter 的百炼结构；不发送 OpenAI 分支的 reasoning、background、store、text.verbosity 字段。
- 结果中的 provider 记录最终采用的后端；base_url 保留用户设置的实际地址。显式 dashscope 配合专属域名时，provider 必须为 dashscope。
- 不改变现有参数位置、Config.resolve 的三元组返回契约、密钥及模型选择、请求 URL 拼接方式。

## 3. 范围边界

本次只修复后端选择和传递、补充相关说明及回归测试。不自动改写 Base URL，不修改 .env，不新增网络重试、进度显示、超时策略或域名白名单，不运行付费联网实验。使用方仍应提供匹配地域的 Key 和正确的 /compatible-mode/v1 地址。

## 4. 验收条件

| 场景 | 预期 |
|---|---|
| 显式 dashscope + 共享域名 | provider=dashscope，采用百炼请求配置 |
| 显式 dashscope + 专属业务域名 | 与共享域名请求配置一致，保留实际 URL |
| 显式 backend 与 URL 推断冲突 | 以 backend 为准 |
| 未提供 backend | 原 URL 推断行为保持一致，未知域名仍为 custom |
| 非法 backend 或空字符串 | 构造时 ValueError，无网络请求 |
| CLI/交互/实验入口 | 正确传递选择的 backend，包括配置默认值 |
| 原 OpenAI/OpenRouter 调用 | 保持各自原请求配置 |

验证以离线单元测试和 dry-run 为主；用测试占位 Key，不输出真实密钥。真实域名调用成功与否受账号、模型能力、服务端及网络状态影响，不以一次调用结果替代请求格式验收。

## 5. 确认门槛

用户已确认本规格与 DESIGN；实现、离线回归测试及 README/env.example 已同步，不执行付费联网实验。
