# search-codegen 后端选择设计

版本：v0.1 · 2026-10-27 · 已确认，已实施
关联：[SPEC.md](SPEC.md)

## 1. 决策：显式配置优先

在 GPT5NativeAgent 构造函数现有参数末尾增加 `backend: Optional[str] = None`，保留旧位置参数调用的兼容性。backend 不为 None 时校验枚举并直接赋给 self.provider；否则执行当前 URL 推断逻辑。

```python
GPT5NativeAgent(api_key, base_url=base_url, model=model, backend=backend)
```

self.provider 继续作为工具结构、请求构造和结果元数据的唯一后端依据，不额外维护另一份可能不一致的后端状态。显式 backend 是协议声明，不自动更改目标地址、模型或密钥。

为何不只扩展 URL 匹配：URL 仅表示访问位置，业务空间和代理域名可能变化；明确的后端选择已包含协议信息，不应在下游被域名猜测覆盖。本次兼容回退保持旧逻辑，未传 backend 的专属域名调用仍推断为 custom；程序化使用者应显式传入 dashscope。

## 2. 入口传递

| 文件/入口 | 拟议调整 |
|---|---|
| main.py / InteractiveCLI | 先归一为 backend if backend is not None else Config.BACKEND；同一值用于 Config.resolve 和 Agent |
| main.py / _run_single | 将 args.backend 同时传给 Config.resolve 与 Agent，覆盖 single/dry-run/经该入口执行的模式 |
| run_experiment_1_3.py | 同一实验 backend 传给各个 Agent，避免两条任务路径分歧 |
| quickstart.py | 已硬编码使用 OpenRouter 凭据和地址的 Agent 显式传入 openrouter |
| tests/manual/agent_cases.py | 已明确使用 OpenRouter 的 Agent 显式传入 openrouter |
| config.py | 保持 resolve 返回契约；不在返回三元组中增加第四项 |

实施时复查所有 GPT5NativeAgent 构造点，保留用于验证兼容回退的无 backend 测试。

## 3. 请求与输出

复用既有 `_tools`、`_build_responses_request` 和 `_post_responses` 分支，不新增专属域名专用请求实现。显式 dashscope 进入已有百炼分支，启用 stream=true，并使用简化工具定义；OpenAI/OpenRouter 分支保持现状。

结果 provider 随最终选择纠正为 dashscope；base_url 仍保存实际访问地址。继续保留现有流事件解析，不将此次修复扩展为实时终端输出。

## 4. 离线验证设计

- 构造参数测试：显式 backend 的枚举校验、空字符串拒绝、冲突时优先级，以及旧位置参数兼容性。
- URL 回退测试：官方 OpenAI、OpenRouter、DashScope 共享域名、未知/专属业务域名，保持原推断行为。
- 百炼请求测试：用虚构工作空间域名和占位 Key 比较共享/专属地址请求体，确认 stream=true、百炼工具结构及 OpenAI 专属字段缺席。
- 入口传递测试：mock Agent/配置或使用 dry-run，验证单次与交互的 CLI/Config 默认后端以及实验入口的两个 Agent；不发真实网络请求。
- 运行现有相关配置、Responses 和验收逻辑测试，确认 OpenAI/OpenRouter 及工具完成判定未回归。

## 5. 文档与风险

同步 README/env.example：解释 BACKEND/--backend 的协议选择作用、业务空间专属地址格式和程序化 backend 参数；不写入真实 Key 或私人工作空间 ID。

风险：显式配置错误会发送不匹配的请求格式，因此非法值立即失败，合法值不被 URL 静默覆盖。无 backend 的第三方调用保持旧行为，但需要主动传入 dashscope 才能享受专属域名修复。服务端可能仍超时，本设计不对响应时间作保证。

## 6. 当前状态

用户已确认设计并完成实现。配置测试禁用本地 dotenv 加载，避免开发者 .env 影响离线断言；默认地址测试改为校验 Config 实际配置。测试结果以本次执行记录为准，不含真实 API 调用。
