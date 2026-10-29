# 搜索与代码计算 WebUI

从 `chapter1/search-codegen` 目录启动，沿用已激活的项目 Python 环境：

```bash
python -m pip install -r webui/requirements.txt
python -m webui.app
# 端口可改：python -m webui.app --port 8002
```

访问 http://127.0.0.1:8001 。仅单进程、单 worker、本地使用。请勿开启多个 uvicorn worker。

在服务端 `.env` 或环境变量中配置 `BACKEND`、对应 Key、模型与 Base URL。百炼专属地址示例：

```dotenv
BACKEND=dashscope
DASHSCOPE_API_KEY=替换为自己的Key
DASHSCOPE_BASE_URL=https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
```

替换工作空间 ID，Key/地域应匹配。修改配置后重启。页面的“已配置”仅检查 Key 是否存在，未调用 API 验证。不要把真实 Key 提交到 Git。

- 任务对话：新建会话选择配置，首轮后固定；后续任务通过上一成功响应 ID 续接。
- 请求预览：先在任务对话中填写输入，再打开预览页；不联网、无需 Key、不修改历史。
- 实验验证：对选定后端执行多次真实调用，使用各后端默认模型与教材固定样例。OpenRouter 仅作诊断；校验器的启发式检查不是严格正确性证明。
- 所有联网任务后台执行，页面显示耗时。首版不显示实时工具阶段或逐字答案；真实工具证据在返回后显示。关闭页面不会取消调用，刷新可恢复。
- 不自动重试。失败也可能产生服务端费用。历史只在进程内保存（最多 50 个非活动记录、单记录 16 MiB、合计 128 MiB），重启丢失。超过单记录预算时证据不会保留，并明确报错；不声称限制响应解析期间的瞬时内存。
- 可下载会话、单轮 JSON/Markdown 以及实验 JSON。下载前需等待任务结束。密钥不回传浏览器。

测试（离线，不调用模型）：

```bash
python -m pytest -q tests/test_webui.py test_backend_selection.py test_responses_agent.py test_config_and_usage.py
```

前端依赖 vendored marked 和 DOMPurify；见 `static/vendor/` 的许可证。

前端 DOM 单元测试（可选开发依赖，不影响页面运行）：

```bash
npm install --prefix /tmp/search-codegen-ui-test --no-audit --no-fund jsdom@26.1.0
NODE_PATH=/tmp/search-codegen-ui-test/node_modules node --test tests/test_webui_frontend.cjs
```

这些测试使用离线接口替身，覆盖预览、Markdown/XSS、后台轮询和刷新恢复；不是产品中的模拟模式。

输入用量显示“总输入 / 缓存命中 / 非缓存输入”。缓存命中已包含在总输入中；接口未返回缓存明细时显示“未提供”，不按零命中处理。历史记录保留了原始 usage，可直接回看拆分结果。
