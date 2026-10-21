# issuepilot

[中文说明](README.zh-CN.md)

issuepilot turns a written spec into GitHub issues, then builds, reviews and merges them with coding agents.
It runs unattended on your own machine: by default nobody needs to approve or merge anything.

- **Planner** (Claude Code by default) splits the spec into issues and triages new problems the agents find.
- **Builder** (Claude Code) implements each issue in its own git worktree and fixes review findings.
- **Reviewer** (Codex by default) reviews each PR read-only and returns structured findings.

The orchestrator itself never asks an LLM what to do next. All state lives in GitHub labels, and plain
Python code moves issues and PRs between states. Agents never touch GitHub: issuepilot does every push,
PR, comment and label change.

## How it works

```
plan ─▶ issue: agent:ready ─────────────────────────▶ agent:building
                                                              │ builder implements, check passes
                                                              ▼
                              PR: agent:reviewing  round:0 ◀──────────┐
                                      │ after CI passes, reviewer      │
                       no blocker/major│      blocker/major            │
                                      ▼              ▼                 │
                                  merge      rounds < max? ──yes──▶ agent:fixing
                                                   │no                   │ builder fixes
                                                   ▼                   │ round+1
                                 planner arbitrates: merge / rewrite   └───────┘
```

- **Two gates before review.** The builder's work must pass your check command locally (issuepilot runs it
  itself and feeds failures back to the builder). After the push, the PR's GitHub CI must pass too. CI runs on
  a clean machine, so it catches files that exist only locally. A CI failure counts as a review round.
- **Dependencies.** A line `Depends on: #3, #5` in an issue body holds the issue back until those issues are
  closed. The planner writes these lines for you.
- **Parallel lanes.** With `max_in_flight = 2` or more, independent issues are built, reviewed and fixed at the
  same time, each in its own thread and worktree. If two PRs touch the same code, the later one gets a merge
  conflict, and the builder resolves it in a fix round.
- **Stuck PRs.** After `max_fix_rounds` rounds the planner reads the whole history and decides: `merge` (the
  remaining findings are not real problems) or `replan` (close the PR, rewrite the issue smaller and clearer,
  start again). An issue can be rewritten once. The second time it gets stuck, it is labeled
  `agent:needs-human`.
- **Errors** (timeouts, network drops, agent crashes) are retried once automatically. A second error labels the
  issue or PR `agent:needs-human` and leaves a comment.
- **Restarts are safe.** Stop the loop at any time. On restart, an issue left in `agent:building` goes back to
  `agent:ready`, and the commits in its worktree are kept.

## Requirements

- Linux or macOS, Python 3.11+, git
- [GitHub CLI](https://cli.github.com/) logged in (`gh auth login`) with access to the repository
- [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`) and/or [Codex CLI](https://github.com/openai/codex)
  (`codex`), installed and logged in. Which one each role uses is configurable.
- A check command for your project that runs tests and type checks, for example `./scripts/check.sh`
- Recommended: a GitHub Actions workflow that runs the same check command on pull requests

## Install

```bash
uv tool install 'issuepilot[dashboard] @ git+https://github.com/heiyan-2020/issuepilot'
# or
pipx install 'issuepilot[dashboard] @ git+https://github.com/heiyan-2020/issuepilot'
```

## Quick start

```bash
cd your-repo
issuepilot init                # writes .issuepilot.toml, and AGENTS.md if it does not exist
$EDITOR .issuepilot.toml AGENTS.md docs/spec.md
issuepilot doctor              # checks tools, login, remote, labels, check command
issuepilot setup               # creates the agent:* labels on GitHub
issuepilot plan                # the planner opens issues, ready to build
issuepilot run                 # builds, reviews and merges them; Ctrl-C to stop
issuepilot dashboard           # in a second terminal
```

Commit `.issuepilot.toml` and `AGENTS.md`. Logs and worktrees go to `.issuepilot/`, which issuepilot keeps out
of git through `.git/info/exclude`.

When the spec changes later, edit it and run `issuepilot plan` again. The planner reads the existing code and
issues and creates issues only for the gap.

## Configuration

`.issuepilot.toml` at the repository root. Every key is optional.

| Key | Default | Meaning |
|---|---|---|
| `spec` | `docs/spec.md` | The requirements the planner splits into issues |
| `conventions` | `AGENTS.md` | Project rules every agent reads first |
| `check` | `./scripts/check.sh` | Shell command that must pass before a PR opens |
| `base_branch` | `main` | Branch that PRs target |
| `language` | `en` | `en` or `zh`: prompts, GitHub comments, logs and the dashboard |
| `state_dir` | `.issuepilot` | Logs and worktrees |
| `prompts_dir` | (none) | Directory with your own `<name>.md` prompts that replace the built-in ones |
| `automation.auto_merge` | `true` | Merge (squash) as soon as the review passes. `false`: label `agent:approved` and wait for a human to merge |
| `automation.auto_ready` | `true` | Issues from `plan` and triage start right away. `false`: label `agent:planned` and wait for `issuepilot approve` |
| `automation.max_in_flight` | `1` | Issues worked on at the same time |
| `automation.max_fix_rounds` | `3` | Fix rounds before the planner arbitrates |
| `automation.max_check_retries` | `2` | Times the builder may retry a failing check |
| `automation.max_triaged_issues` | `8` | Upper limit for issues triage may create and start on its own |
| `automation.poll_seconds` | `60` | GitHub polling interval when idle |
| `automation.agent_timeout` | `3600` | Seconds per agent call |
| `automation.check_timeout` | `1800` | Seconds per check run |
| `automation.ci_timeout` | `1800` | Seconds to wait for CI on the latest commit |
| `agents.<role>` | see below | `{ cli = "claude" \| "codex", model = "..." }` for `planner`, `builder`, `reviewer` |
| `notes.<prompt>` | (none) | Extra text appended to one prompt: `plan`, `triage`, `build`, `checkfail`, `fix`, `review`, `arbitrate` |

Default agents: planner `claude` with model `opus`, builder `claude`, reviewer `codex`. An empty model means
the CLI's default. A reviewer from a different model family than the builder catches more problems.

Environment variables override scalar keys: `ISSUEPILOT_<KEY>`, for example `ISSUEPILOT_AUTO_MERGE=1`,
`ISSUEPILOT_MAX_IN_FLIGHT=3`, `ISSUEPILOT_BUILDER_MODEL=sonnet`, `ISSUEPILOT_REVIEWER_CLI=claude`.

Use `notes` for project-specific review rules instead of editing prompts:

```toml
[notes]
review = """
- Every API route must check the caller's role. A missing check is a blocker.
- UI behavior that can only be seen in the WeChat devtools is not a blocker.
"""
plan = "The first issue sets up `backend/` (Fastify) and `miniprogram/`."
```

### Adding human gates

issuepilot is fully automatic by default: issues from `plan` start right away, PRs merge when the review passes,
and triage can create and start up to `max_triaged_issues` new issues. The check command and CI are what keep bad
code out, so make them strict, and read the merged history afterwards. To approve issues and merge PRs yourself:

```toml
[automation]
auto_merge = false   # approved PRs get agent:approved and wait for you to merge
auto_ready = false   # new issues get agent:planned and wait for `issuepilot approve <numbers…>`
```

## What the agents can do

| Role | Claude Code | Codex |
|---|---|---|
| planner, reviewer | `Read`, `Grep`, `Glob` and `git diff/log/show/status`; no edits | `-s read-only` sandbox |
| builder | `bypassPermissions` in its worktree; `git push` and `gh` are blocked | `-s workspace-write`, no network |

**The builder runs commands without asking.** It can run anything your user can run on this machine. Run
issuepilot in a container, VM or a dedicated user account, and do not keep secrets the builder should not see in
its environment. issuepilot itself only uses `gh` and `git` with your credentials.

## Working with the loop

- **Give feedback:** write a normal comment on the PR. The next reviewer and builder rounds read it. Inline
  review comments are not read yet.
- **`agent:needs-human`:** read issuepilot's comment, fix the cause, then set the label back to `agent:ready`
  (issue) or `agent:reviewing` / `agent:fixing` (PR).
- **More fix rounds for one PR:** lower its `round:N` label, or raise `max_fix_rounds`.
- **Hot reload:** `issuepilot run` restarts itself between rounds when its code or `.issuepilot.toml` changes,
  without stopping running lanes. Disable it with `--no-reload`.

## Dashboard

`issuepilot dashboard` (needs `rich`) shows each issue's state, PR and fix round, a live panel per running agent
(role, elapsed time, commits and diff in its worktree, and its latest tool calls read from the Claude Code and
Codex session transcripts), and the end of `.issuepilot/loop.log`. It is read-only.

## Limitations

- All GitHub actions use your account, so the reviewer cannot formally approve or request changes. The verdict
  is a comment plus a label. A separate bot account or GitHub App would allow formal reviews.
- The dashboard reads `/proc` on Linux and uses the system `ps` and `lsof` tools on macOS to inspect the current user's processes. If detection is unavailable, it shows an unknown process status while keeping issue states and logs visible. A Codex launcher and its native child are shown as one agent.
- issuepilot polls GitHub; it does not use webhooks.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The end-to-end tests run the real loop against fake `gh`, `claude` and `codex` executables in `tests/fakebin/`
and a local bare repository, so they need no network or accounts.

## License

MIT
