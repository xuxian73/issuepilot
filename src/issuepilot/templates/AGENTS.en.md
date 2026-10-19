# Project conventions (every agent reads this)

Requirements are in `docs/spec.md`. When something here conflicts with the spec, the spec wins.

## Rules

1. Run `./scripts/check.sh` before every commit. It must pass.
2. Do only what the issue asks. Report problems outside its scope in `proposed_issues` instead of fixing them.
3. Every feature and fix comes with tests. Tests must not need the network.
4. Never write real personal data, secrets or tokens into code, tests or fixtures.
5. Commit message format: `<type>: <summary> (#<issue>)`, where type is feat / fix / test / docs / chore / refactor.
6. Never run `git push` or `gh`. The orchestrator does all pushes and GitHub operations.
