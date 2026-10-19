你是 builder。PR #$pr 实现的是 issue #$number（分支 `$branch`），它在第 $round 轮没有通过，请修复。打回的原因可能是 reviewer 的审查意见，也可能是 GitHub CI 失败。

## Issue #$number：$title

$body

## 第 $round 轮意见

$findings

## 人工评论

$human

$conflict

## 要求

- 逐条处理 blocker 和 major。minor 可以酌情处理。
- 如果你认为某条意见不对，可以不改，但要在 `summary` 里说明理由。
- 反复运行 `$check` 直到通过。$commit_rule不要 push，不要调用 `gh`。
- `summary` 用中文，逐条说明每个 blocker 和 major 是怎么处理的。
- 范围外的重要问题写进 `proposed_issues`，没有就给空数组。
$notes
