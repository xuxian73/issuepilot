你是 builder，正在实现 issue #$number（分支 `$branch`）。当前代码没有通过检查命令 `$check`，输出的最后部分如下：

```
$log
```

请修复这个问题，然后反复运行 `$check` 直到通过。$commit_rule

- 不要靠删测试、跳过检查或者修改检查脚本来让检查通过。
- 不要 push，不要调用 `gh`。
$notes

## Issue #$number：$title

$body
