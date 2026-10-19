# issuepilot

[English](README.md)

issuepilot 把一份需求文档拆成 GitHub issue，再用编程 agent 开发、审查、合并。它在你自己的机器上运行，可以无人值守。

- **planner**（默认 Claude Code）：把需求拆成 issue；分诊 agent 在开发和审查中发现的新问题。
- **builder**（Claude Code）：在每个 issue 专用的 git worktree 里实现功能，按审查意见修复。
- **reviewer**（默认 Codex）：只读审查每个 PR，输出结构化意见。

调度器本身不让 LLM 决定流程。所有状态都存在 GitHub 标签里，由普通的 Python 代码做状态转换。agent 不接触 GitHub：push、开 PR、发评论、改标签都由 issuepilot 完成。

## 流程

```
plan ─▶ issue: agent:planned ──(approve)──▶ agent:ready ──▶ agent:building
                                                              │ builder 实现，检查通过
                                                              ▼
                              PR: agent:reviewing  round:0 ◀──────────┐
                                      │ CI 通过后 reviewer 审查         │
                     没有 blocker/major│        有 blocker/major       │
                                      ▼              ▼                 │
                                    合并        轮数 < 上限？──是──▶ agent:fixing
                                                   │否                   │ builder 修复
                                                   ▼                   │ round+1
                                      planner 仲裁：合并 / 重写 issue   └───────┘
```

- **审查前有两道检查。** builder 的改动先要在本地通过你的检查命令（issuepilot 自己运行检查，失败就把输出交回 builder）。push 之后，PR 的 GitHub CI 也要通过。CI 在干净的机器上运行，能发现只在本地存在的文件之类的问题。CI 失败算一轮打回。
- **依赖。** issue 正文里的 `Depends on: #3, #5` 一行表示依赖，这些 issue 关闭后才开始开发。planner 会自动写这一行。
- **并行。** `max_in_flight` 设为 2 或更大时，互不依赖的 issue 同时开发、审查、修复，每个 issue 有自己的线程和 worktree。两个 PR 改到同一处时，后合并的会出现冲突，由 builder 在修复轮里解决。
- **卡住的 PR。** 修满 `max_fix_rounds` 轮后，planner 看完全部记录再决定：`merge`（剩下的意见不是真问题），或者 `replan`（关掉 PR，把 issue 重写得更小、更清楚，从头开发）。每个 issue 只能重写一次，第二次卡住就打 `agent:needs-human`。
- **出错**（超时、断网、agent 崩溃）自动重试一次。再出错就给 issue 或 PR 打 `agent:needs-human` 并留评论。
- **可以随时停。** 重启时，停在 `agent:building` 的 issue 改回 `agent:ready`，worktree 里已有的提交都保留。

## 需要什么

- Linux 或 macOS，Python 3.11+，git
- 已登录的 [GitHub CLI](https://cli.github.com/)（`gh auth login`），对仓库有写权限
- 已安装并登录的 [Claude Code](https://docs.claude.com/en/docs/claude-code)（`claude`）和/或 [Codex CLI](https://github.com/openai/codex)（`codex`）。每个角色用哪个可以配置。
- 项目的检查命令，负责跑测试和类型检查，比如 `./scripts/check.sh`
- 建议：一个在 PR 上运行同一个检查命令的 GitHub Actions workflow

## 安装

```bash
uv tool install 'issuepilot[dashboard] @ git+https://github.com/heiyan-2020/issuepilot'
# 或者
pipx install 'issuepilot[dashboard] @ git+https://github.com/heiyan-2020/issuepilot'
```

## 快速开始

```bash
cd your-repo
issuepilot init --language zh  # 生成 .issuepilot.toml；没有 AGENTS.md 时也生成一个
$EDITOR .issuepilot.toml AGENTS.md docs/spec.md
issuepilot doctor              # 检查工具、登录、远程仓库、标签、检查命令
issuepilot setup               # 在 GitHub 上创建 agent:* 标签
issuepilot plan                # planner 创建 issue，打 agent:planned
# 在 GitHub 上看一遍 issue，需要的话直接修改，然后：
issuepilot approve 1 2 3
issuepilot run --once          # 先只跑一轮，观察效果
issuepilot run                 # 循环运行，Ctrl-C 停止
issuepilot dashboard           # 在另一个终端里看进度
```

把 `.issuepilot.toml` 和 `AGENTS.md` 提交到仓库。日志和 worktree 放在 `.issuepilot/`，issuepilot 通过 `.git/info/exclude` 让 git 忽略这个目录。

需求有变化时，改完需求文档再运行一次 `issuepilot plan`。planner 会读现有代码和 issue，只为差距部分建 issue。

## 配置

配置文件是仓库根目录的 `.issuepilot.toml`，所有项都可以不写。

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `spec` | `docs/spec.md` | 需求文档，planner 按它拆 issue |
| `conventions` | `AGENTS.md` | 项目约定，每个 agent 先读它 |
| `check` | `./scripts/check.sh` | 开 PR 前必须通过的 shell 命令 |
| `base_branch` | `main` | PR 的目标分支 |
| `language` | `en` | `en` 或 `zh`：提示词、GitHub 评论、日志和看板的语言 |
| `state_dir` | `.issuepilot` | 日志和 worktree 的目录 |
| `prompts_dir` | （无） | 放自定义 `<名字>.md` 提示词的目录，同名的会替换内置提示词 |
| `automation.auto_merge` | `false` | 审查通过后直接 squash 合并 |
| `automation.auto_ready` | `false` | 分诊新建的 issue 不用确认就开始开发 |
| `automation.max_in_flight` | `1` | 同时推进几个 issue |
| `automation.max_fix_rounds` | `3` | 修复几轮后交给 planner 仲裁 |
| `automation.max_check_retries` | `2` | 检查失败后 builder 最多重试几次 |
| `automation.max_triaged_issues` | `8` | 分诊最多自动放行几个 issue |
| `automation.poll_seconds` | `60` | 空闲时多久查一次 GitHub |
| `automation.agent_timeout` | `3600` | 单次 agent 调用的超时（秒） |
| `automation.check_timeout` | `1800` | 单次检查的超时（秒） |
| `automation.ci_timeout` | `1800` | 等最新提交的 CI 结果最多等多久（秒） |
| `agents.<角色>` | 见下文 | `planner`、`builder`、`reviewer` 各自的 `{ cli = "claude" \| "codex", model = "..." }` |
| `notes.<提示词>` | （无） | 追加到某个提示词末尾的项目说明：`plan`、`triage`、`build`、`checkfail`、`fix`、`review`、`arbitrate` |

默认的 agent：planner 用 `claude`、模型 `opus`；builder 用 `claude`；reviewer 用 `codex`。模型留空表示用 CLI 的默认模型。reviewer 和 builder 来自不同的模型家族，能发现更多问题。

环境变量可以覆盖单值配置项，格式是 `ISSUEPILOT_<配置项大写>`，比如 `ISSUEPILOT_AUTO_MERGE=1`、`ISSUEPILOT_MAX_IN_FLIGHT=3`、`ISSUEPILOT_BUILDER_MODEL=sonnet`、`ISSUEPILOT_REVIEWER_CLI=claude`。

项目专用的审查规则写进 `notes`，不用改提示词：

```toml
[notes]
review = """
- 每个 API 都要检查调用者的角色，漏了就是 blocker。
- 只有在微信开发者工具里才能看到的界面效果，不列为 blocker。
"""
plan = "第一个 issue 搭好 `backend/`（Fastify）和 `miniprogram/` 的骨架。"
```

### 全自动模式

```toml
[automation]
auto_merge = true
auto_ready = true
```

两个都打开后，`issuepilot approve` 之后就不需要任何人确认：审查通过就合并，分诊最多可以新建并启动 `max_triaged_issues` 个 issue。只有在你信得过检查命令和 CI 时才这样用，事后要读一遍合并记录。

## agent 能做什么

| 角色 | Claude Code | Codex |
|---|---|---|
| planner、reviewer | `Read`、`Grep`、`Glob` 和 `git diff/log/show/status`，不能改文件 | `-s read-only` 沙箱 |
| builder | 在自己的 worktree 里 `bypassPermissions`；禁止 `git push` 和 `gh` | `-s workspace-write`，没有网络 |

**builder 执行命令不需要确认。** 你的用户在这台机器上能执行的命令，它都能执行。请在容器、虚拟机或单独的系统账号里运行 issuepilot，不要让 builder 的环境里有它不该看到的密钥。issuepilot 自己只用你的凭据运行 `gh` 和 `git`。

## 人工介入

- **给 agent 提意见：** 在 PR 下发普通评论，下一轮 reviewer 和 builder 都会看到。行内评论（review comment）暂时读不到。
- **处理 `agent:needs-human`：** 看 issuepilot 留的评论，解决问题后把标签改回 `agent:ready`（issue），或 `agent:reviewing` / `agent:fixing`（PR）。
- **让某个 PR 多修几轮：** 把它的 `round:N` 标签改小，或者调大 `max_fix_rounds`。
- **热更新：** issuepilot 的代码或 `.issuepilot.toml` 变化后，`issuepilot run` 会在两轮之间重启自己，不打断正在运行的 issue。用 `--no-reload` 关闭。

## 看板

`issuepilot dashboard`（需要 `rich`）显示：每个 issue 的状态、PR 和修复轮数；每个正在运行的 agent 一个面板（角色、运行时间、worktree 里的提交和改动、最近的工具调用，从 Claude Code 和 Codex 的会话记录里实时读取）；`.issuepilot/loop.log` 的最后几行。看板只读，不改任何东西。

## 已知限制

- 所有 GitHub 操作都用你的账号，所以 reviewer 不能正式 Approve 或 Request changes，审查结论用评论和标签表示。要正式审批，需要单独的机器人账号或 GitHub App。
- 看板读取 `/proc`，所以只有在 Linux 上能显示正在运行的 agent。
- issuepilot 靠轮询 GitHub，不用 webhook。

## 开发

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/python -m pytest
```

端到端测试在本地的 bare 仓库上运行真实的调度循环，`gh`、`claude`、`codex` 换成 `tests/fakebin/` 里的假程序，不需要网络或账号。

## 许可证

MIT
