# 项目约定（所有 agent 必读）

需求见 `docs/spec.md`，遇到冲突时以 spec 为准。

## 规则

1. 提交前必须运行 `./scripts/check.sh` 并全部通过。
2. 每个 issue 只做 issue 要求的内容。发现范围外的问题，写进 `proposed_issues`，不要自己去改。
3. 新功能和修复都要带测试。测试不能依赖网络。
4. 绝不在代码、测试或测试数据里写入真实的个人信息、密钥或令牌。
5. 提交信息格式：`<type>: <摘要> (#<issue 号>)`，type 取 feat / fix / test / docs / chore / refactor。
6. 不要执行 `git push`，也不要调用 `gh`。推送和 GitHub 操作都由调度器完成。
