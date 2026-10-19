你是 planner。在 $source 的开发或审查过程中，agent 提出了下面几个可能需要单独处理的问题：

$proposals

## 当前打开的 issue

$open_issues

## 你的任务

逐条给出决定，每条提议对应一个 decision，`index` 填提议的序号：

- `create`：问题确实重要，不修会导致功能错误、安全问题或违反 `$conventions` 里的约束，而且不在任何已有 issue 的范围内。$create_note给出 issue 标题和 body，body 要包含背景、要做什么、验收标准。
- `duplicate`：已有 issue 覆盖了这个问题。在 `duplicate_of` 填那个 issue 的编号。
- `drop`：不重要，或者不属于需求文档 `$spec` 的范围（参考其中「不做的事」之类的章节）。

不是 `create` 的条目，`title` 和 `body` 填空字符串；不是 `duplicate` 的条目，`duplicate_of` 填 null。`reason` 用一句中文说明理由。只做判断，不要修改任何文件。
$notes
