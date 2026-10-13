# Web GUI — Kimi Web Search Agent 本地图形界面

在浏览器中体验 ReAct 循环（💭 思考 → 🔧 行动 → 👀 观察 → ✅ 答案），
功能与 CLI（`python main.py`）完全一致：联网搜索、离线演示、参数设置、历史回看。

- 需求与验收标准：[SPEC.md](SPEC.md)
- 设计决策与实现约定：[DESIGN.md](DESIGN.md)

## 安装与启动

```bash
# 1. 安装可选依赖（独立于主项目 requirements.txt）
python -m pip install -r webui/requirements.txt

# 2. 在 chapter1/web-search-agent 目录下启动
python -m webui.app

# 3. 浏览器访问
#    http://127.0.0.1:8000
```

API Key 配置方式与 CLI 相同（服务端读取，前端不经手）：

```bash
export MOONSHOT_API_KEY='your-api-key'   # 或 KIMI_API_KEY；OPENROUTER_API_KEY 为通用兜底
```

## 功能

| 功能 | 说明 |
|---|---|
| 问答 | 输入问题（Ctrl/⌘+Enter 提交），完成后一次性展示 Markdown 答案；kimi-k3 单次可能需要 1-3 分钟 |
| ReAct 轨迹 | 按迭代展示 💭→🔧→👀→✅ 步骤卡片，思考/行动/观察默认折叠、点击展开 |
| 参数设置 | provider（kimi / offline-demo）、model、max_steps、temperature、timeout，对下一次提问生效 |
| 会话历史 | 服务端内存保存（上限 100 条，FIFO 淘汰），刷新页面可回看，支持一键清空 |

**没有 API Key？** 在参数面板选择 `offline-demo` 即可离线体验完整流程（示例轨迹，非真实搜索）。

## 运行测试

```bash
# webui API 层测试已并入项目测试（tests/test_webui.py，不触网）
python -m pytest tests/ -q
```

## 注意事项

- 服务仅监听本机 `127.0.0.1`，无鉴权，请勿暴露到公网。
- 历史仅存内存，进程重启即清空（见 DESIGN.md D2）。
- 修改参数面板中的 timeout 需通过重建 Agent 实例生效，属预期行为（DESIGN.md D5）。
