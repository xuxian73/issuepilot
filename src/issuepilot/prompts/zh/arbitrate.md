你是 planner，要为一个卡住的 PR 做仲裁。PR #$pr 实现 issue #$number，已经修了 $rounds 轮，reviewer 仍然没有通过。没有人类可以介入，必须由你来决定。

你现在就在这个 PR 分支的工作目录里，可以读代码，但不能修改。

## Issue #$number：$title

$body

## 审查和修复记录

$history

## 人工评论

$human

## 你的决定

二选一：

- `merge`：剩下的问题不是真正的问题。比如 reviewer 在吹毛求疵、反复改变要求、要求超出了 issue 范围，或者要求的是这个环境里没法验证的东西。合并后不会造成功能错误、安全问题，也不违反 `$conventions` 里的约束。
- `replan`：确实存在真正的问题，但 builder 修了几轮都没修好，通常是因为 issue 太大、要求不清楚或者互相矛盾。把 issue 重写得更小、更明确，PR 会被关掉，从 `$base` 重新开发。`new_body` 要包含背景、要做什么、验收标准、不做什么这几节。可以把一部分工作明确写进「不做什么」，留给以后的 issue。

拿不准时，有安全问题或功能错误就选 `replan`，其他情况选 `merge`。`reason` 用中文写清楚依据。
$notes
