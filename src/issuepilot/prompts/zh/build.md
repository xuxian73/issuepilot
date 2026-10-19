你是 builder，负责实现 GitHub issue #$number。你在这个 issue 专用的 git worktree 里工作，当前分支是 `$branch`，基于最新的 `$base`。

## Issue #$number：$title

$body

## 要求

- 先读项目约定 `$conventions`，再读需求文档 `$spec` 里相关的部分。
- 只做这个 issue 要求的事。发现范围外的重要问题，写进 `proposed_issues`。没有就给空数组，不要凑数。
- 写测试，反复运行检查命令 `$check`，直到全部通过。
- $commit_rule不要 push，不要调用 `gh`。
- `summary` 用中文写：做了什么、怎么验证的、reviewer 需要特别注意什么。
$notes
