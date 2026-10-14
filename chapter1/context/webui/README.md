# Web GUI — 上下文消融实验本地界面

在浏览器中运行实验 1-1：单任务 ReAct 执行（5 种上下文模式）、消融套件后台运行
（进度网格 / 模式×案例矩阵 / Chart.js 图表 / Markdown 报告下载）、provider 切换。

- 需求与验收标准：[SPEC.md](SPEC.md)
- 设计决策与实现约定：[DESIGN.md](DESIGN.md)

## 安装与启动

```bash
# 1. 安装可选依赖（独立于主项目 requirements.txt）
python -m pip install -r webui/requirements.txt

# 2. 在 chapter1/context 目录下启动
python -m webui.app            # 默认 http://127.0.0.1:8000（--port 可改）

# 3. 浏览器访问
#    http://127.0.0.1:8000
```

API Key 配置与 CLI 完全一致（服务端从环境变量 / `.env` 读取，前端不经手）：
按 provider 设置对应变量（如 `ARK_API_KEY`、`MOONSHOT_API_KEY`），
`OPENROUTER_API_KEY` 可作通用兜底。无 key 时界面可浏览，执行会返回
指名缺失变量的中文错误。

## 功能

| 功能 | 说明 |
|---|---|
| 单任务 | 5 种上下文模式可视化卡片；执行后展示最终答案、工具调用时间轴、grounding 数字来源徽章、迭代/耗时统计 |
| 消融套件 | 选模式×案例数（1-4）后台运行；进度网格实时填充（✓/⚠/✗）；完成后展示对比矩阵 + 三组图表；报告 `.md` 下载 |
| Provider | 下拉切换全部注册 provider，实时显示各 key 配置状态 |
| 历史 | 单任务与消融统一入史（内存保存，上限 50 条 FIFO），点击回看，支持清空 |

## 注意事项

- 消融一轮 5 模式 × 2 案例可能需要 10-30 分钟；期间执行入口锁定（重复提交返回 409）。
- 同一时刻只允许一个执行流（单任务或消融），这是设计行为而非缺陷。
- 历史仅存内存，进程重启即清空。
- **仅限本机使用**：`code_interpreter` 工具会执行模型生成的代码，且现有沙箱
  存在已知逃逸路径（工程分析问题 2，暂缓修复），请勿将端口暴露到公网。
- 消融的数字评分 rubric（`run_experiment_1_1.py`）不在 GUI 范围内；
  GUI 展示的是与 CLI 套件一致的 completed/grounding 口径。
