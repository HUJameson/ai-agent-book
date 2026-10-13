# ai-agent-book Git 管理方案与实施记录

| 项 | 值 |
|---|---|
| 版本 | v1.0（2026-10-13） |
| 仓库 | https://github.com/hujameson/ai-agent-book（fork 自 bojieli/ai-agent-book） |
| 文档性质 | 管理方案 + 本次实施记录 + 提交清单 |

---

## 第一部分：管理方案

### 1. 现状诊断（实施前）

| 项 | 实际状态 |
|---|---|
| Git 仓库 | 已存在，clone 自上游 bojieli/ai-agent-book，本地 main 与 origin/main 同步 |
| 远程 fork | hujameson/ai-agent-book（公开仓库），落后上游 1021 个提交，历史为上游真子集（可 fast-forward） |
| 本地变更 | 11 处：5 个修改 + 6 个未跟踪文件 |
| 大文件 | 被跟踪文件中无 >10M 文件；上游 .gitignore 已覆盖 *.mp4、results/、__pycache__ 等 |
| CI | 上游已有 7 个 GitHub Actions workflows（含 web-search-agent-tests.yml） |
| 认证 | gh CLI token 失效；push 无 HTTPS 凭证，后切换为 SSH |
| 身份 | 全局 git 邮箱为公司邮箱，公开仓库存在隐私风险 |

### 2. 远程对接：双 remote 模式

```
origin   = git@github.com:hujameson/ai-agent-book.git   （推送目标）
upstream = git@github.com:bojieli/ai-agent-book.git     （只读：同步上游）
```

- 同步方向：upstream/main → 本地 main → origin/main
- **永不向 upstream push**
- fork 落后时 fast-forward 即可，禁止 force push

### 3. 身份与认证

1. 认证方式：SSH key（gh auth login 时选择 SSH 协议）
2. 邮箱隐私：仓库级配置 noreply 邮箱（不动全局配置，不影响公司项目）：
   `git config user.email "hujameson@users.noreply.github.com"`
3. 已有提交中的公司邮箱不受影响（本次实施前的历史不含本人提交）

### 4. 分支模型（精简 trunk-based）

- `main`：唯一长期分支，始终可用，受分支保护
- `feature/*`：短生命周期功能分支，走 PR 合入 main（单人项目也走 PR，让 CI 门禁生效）
- 上游同步：`git fetch upstream && git rebase upstream/main`
- 里程碑打 tag（如 ch1-webui-v1.0）

### 5. 提交规范（Conventional Commits）

```
<类型>(<scope>): <中文描述>

类型：feat / fix / docs / test / refactor / chore / ci
scope：章节或模块名（如 ch1、webui、deps）
一次提交只做一件事
```

### 6. CI 与仓库保护

- Actions：fork 上手动启用，7 个上游 workflow 全部激活
- main 分支保护：禁止 force push、禁止删除、要求线性历史
- 新增 secrets 扫描（gitleaks），见第 9 节

### 7. 日常 SOP

```bash
# 同步上游（建议每周）
git fetch upstream && git rebase upstream/main && git push origin main

# 新功能
git checkout -b feature/xxx    # 开发 → push → PR（CI 门禁）→ 合并

# 提交前
gitleaks detect --source .     # 本地密钥扫描（已安装 v8.30.1）
```

---

## 第二部分：实施记录（2026-10-13）

| 步骤 | 结果 |
|---|---|
| gh 认证 | HUJameson，SSH 协议 |
| 双 remote 配置 | origin → hujameson / upstream → bojieli（SSH） |
| 邮箱保护 | 仓库级 noreply |
| .gitignore 增补 | .opencode、opencode.json、*.code-workspace、demo/result.json、!.gitleaks.toml |
| 分组提交 | 5 个提交（见第三部分），工作区清零 |
| push 同步 | fork 从落后 1021 提交 fast-forward 至 27aee3f8 |
| Actions | 已启用，7 个 workflow 全部激活 |
| 分支保护 | 禁 force push / 禁删除 / 线性历史 |
| secrets 扫描 | gitleaks 增量扫描上线，CI 验证通过（run 38354622093） |

### secrets 扫描的关键决策

1. **增量扫描策略**：上游全历史命中 2141 处（占位 key 与服务端标识符，属上游公开仓库现状），fork 不对历史负责；push 只扫 before..after 区间，PR 扫 origin/base..HEAD，手动触发只扫 HEAD。
2. **误报放行**：本人提交命中 6 处，均为 validation/latest.json 中 Moonshot 响应返回的 access_token_id（ak- 前缀服务端会话标识符，非 sk- 账户密钥，无法调用 API）。以 .gitleaks.toml 精确 allowlist 放行，保留证据文件完整。
3. **版本坑记录**（已写入 .gitleaks.toml 注释）：.gitignore 的 .* 规则会吞掉 .gitleaks.toml（需白名单）；gitleaks ≥ 8.21 在 extend 模式下必须用 [[allowlists]] 数组语法，且 regexes 匹配的是提取出的 secret 值而非整行。

---

## 第三部分：提交记录（5 个）

| # | 提交 | 类型 | 日期 | 说明 | 变更规模 |
|---|---|---|---|---|---|
| 1 | aa6993aa | chore | 2026-10-13 13:44 | 忽略本地工具配置与一次性运行产物 | 1 文件，+7 |
| 2 | 6299e5fb | fix(ch1) | 2026-10-13 13:44 | 去重 config.py 中重复的 load_dotenv 调用 | 2 文件，+437/-1036 |
| 3 | 07deb86a | feat(ch1) | 2026-10-13 13:44 | 新增 web-search-agent 本地 Web GUI（FastAPI + 原生 JS） | 14 文件，+1215 |
| 4 | 7dde46ce | chore(deps) | 2026-10-13 13:46 | 升级 numpy 至 >=2.1.0 并同步 uv.lock | 2 文件，+3975/-2426 |
| 5 | 27aee3f8 | ci | 2026-10-13 14:03 | 新增 gitleaks 增量密钥扫描（含 access_token_id 误报放行） | 3 文件，+58 |

**合计**：21 文件变更，+5692 / -3462 行。

### 提交 3（feat）附注：Web GUI 交付内容

- webui/ 目录：app.py（FastAPI 路由）、agent_runner.py（agent 对接层 + 历史存储）、static/（原生 HTML/CSS/JS + vendored marked v12.0.2）、requirements.txt（可选依赖）、SPEC.md/DESIGN.md 及 Word 版
- tests/test_webui.py：15 个 API 层测试（不触网），项目测试 59 个全部通过，既有测试零回归
- 设计要点：每请求新建 Agent 实例 + 全局锁串行；历史内存保存 FIFO 上限 100；错误双轨制（参数错 4xx / 业务兜底 200+status=error）；API Key 只在服务端环境变量
- 配套文档：需求规格（SPEC）、设计决策记录（DESIGN），均已输出 Word 版

### 验收记录（CI run 38354622093）

- 触发：push 27aee3f8 至 main
- 扫描区间：7dde46ce..27aee3f8
- 结论：success，no leaks found
