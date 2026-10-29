# WebUI 验证记录

日期：2026-10-28。实现基于用户已确认的 SPEC/DESIGN。

## 自动验证

- Python：54 项通过，包含 25 项 WebUI 路由/状态/runner 测试与 29 项既有回归测试。
- 前端 DOM：4 项通过，使用 jsdom 和离线接口替身，覆盖后端能力、输入预览、侧栏折叠、Markdown/XSS、工具证据、用量缺失、单次提交与轮询、刷新恢复。
- Ruff F 类检查、JavaScript 语法检查和 git diff --check 通过。
- WebUI 文件目录 gitleaks 扫描未发现密钥。
- Python 测试环境有一条 Starlette 对 httpx 测试客户端的弃用提示，不影响测试通过；未为此修改共享依赖环境。

## 浏览器验证

- Chrome 中首屏正常，后端列表、样例输入、页签切换和离线预览正常。
- 百炼专属业务域名预览的 provider 为 dashscope，stream=true，包含 web_search 和 code_interpreter 工具定义，百炼不适用的参数控件禁用。
- 390px 视口下 documentWidth=viewport=390，无横向溢出。
- 运行/历史/Markdown 的更多状态由离线测试替身验证，不将其描述为真实模型实验结果。

## 边界

未执行真实模型请求或付费实验，未修改 .env 或核心 Agent/CLI。历史只在进程内保存，关闭浏览器不取消后台请求。未提交、推送或合并本次改动。启动与测试命令见 README.md。

## 2026-10-29 输入缓存拆分

前端 DOM 测试 5 项通过，新增覆盖真实用量样例（93685/66304/27381）、零命中、缺失字段、异常数值及兼容字段。JavaScript 语法与差异格式检查通过。仅调整展示，不重新调用模型、不更改缓存配置。
