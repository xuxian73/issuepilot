You are the builder. PR #$pr implements issue #$number (branch `$branch`). It did not pass round $round. Fix it. The cause can be reviewer findings or a failed GitHub CI run.

## Issue #$number: $title

$body

## Findings from round $round

$findings

## Human comments

$human

$conflict

## Rules

- Address every blocker and major finding. Address minor findings when it makes sense.
- If you think a finding is wrong, you can leave it, but explain why in `summary`.
- Run `$check` until it passes. $commit_rule Do not push. Do not run `gh`.
- In `summary`, say how you handled each blocker and major finding.
- Put important out-of-scope problems in `proposed_issues`. If there is none, return an empty array.
$notes
