"""End-to-end runs of the loop against fake gh, claude and codex."""


def plan_and_approve(repo):
    repo.cli("setup")
    repo.cli("plan")
    repo.cli("approve", "1", "2")


def test_issues_merge_in_dependency_order(repo):
    plan_and_approve(repo)
    assert "Depends on: #1" in repo.issue(2)["body"]
    out = repo.run_until(lambda: repo.issue(2)["state"] == "CLOSED")
    assert repo.issue(1)["state"] == "CLOSED"
    prs = sorted(repo.gh()["prs"].values(), key=lambda p: p["number"])
    assert [p["headRefName"] for p in prs] == ["agent/issue-1", "agent/issue-2"]
    assert all(p["state"] == "MERGED" for p in prs)
    assert "feature-1-0.txt" in repo.origin_files() and "feature-2-0.txt" in repo.origin_files()
    # issue 2 started only after issue 1 was merged
    assert out.index("merged PR #3") < out.index("building issue #2")
    # each PR got a machine-readable review comment
    assert all(any(c.startswith("<!-- agent:review round=1 data=") for c in p["comments"]) for p in prs)
    # merged worktrees are cleaned up
    repo.cli("run", "--once", "--no-reload")
    assert not list((repo.work / ".issuepilot" / "worktrees").glob("issue-*"))


def test_review_rejection_goes_through_a_fix_round(repo):
    repo.script(reject_rounds=1)
    plan_and_approve(repo)
    repo.run_until(lambda: repo.issue(1)["state"] == "CLOSED")
    pr = repo.pr(3)
    assert pr["state"] == "MERGED"
    assert "round:1" in pr["labels"] and "round:0" not in pr["labels"]
    assert any(c.startswith("<!-- agent:builder round=1 data=") for c in pr["comments"])
    assert any("changes needed (1 blocker/major)" in c for c in pr["comments"])
    fix_prompt = next(c["prompt"] for c in repo.agent_calls() if "did not pass round 1" in c["prompt"])
    assert "missing test" in fix_prompt


def test_planner_arbitrates_after_max_fix_rounds(repo):
    repo.script(reject_rounds=99)
    plan_and_approve(repo)
    repo.run_until(lambda: repo.issue(1)["state"] == "CLOSED", env={"ISSUEPILOT_MAX_FIX_ROUNDS": "1"})
    pr = repo.pr(3)
    assert any(c.startswith("<!-- agent:arbitrate -->") and "Planner arbitration: merge" in c for c in pr["comments"])
    assert pr["state"] == "MERGED"


def test_arbitration_replan_rewrites_issue_and_restarts(repo):
    repo.script(reject_rounds=99, arbitrate="replan")
    plan_and_approve(repo)
    env = {"ISSUEPILOT_MAX_FIX_ROUNDS": "0"}
    repo.run_until(lambda: "agent:replanned" in repo.issue(1)["labels"], env=env)
    assert repo.pr(3)["state"] == "CLOSED"
    assert "agent:ready" in repo.issue(1)["labels"]
    # the second time it gets stuck, a human must decide
    repo.run_until(lambda: any("agent:needs-human" in p["labels"] for p in repo.gh()["prs"].values()), env=env)


def test_build_error_is_retried_once_then_needs_human(repo):
    repo.script(fail_build=True)
    plan_and_approve(repo)
    repo.cli("run", "--once", "--no-reload")
    assert {"agent:ready", "agent:retried"} <= set(repo.issue(1)["labels"])
    repo.cli("run", "--once", "--no-reload")
    assert "agent:needs-human" in repo.issue(1)["labels"]
    assert any("needs a human" in c for c in repo.issue(1)["comments"])


def test_failing_check_is_fed_back_to_the_builder(repo):
    plan_and_approve(repo)
    # passes only once the builder has committed twice (the second commit comes from the check-fix call)
    env = {"ISSUEPILOT_CHECK": '[ "$(ls feature-* | wc -l)" -ge 2 ]'}
    repo.run_until(lambda: repo.issue(1)["state"] == "CLOSED", env=env)
    prompts = [c["prompt"] for c in repo.agent_calls() if c["cli"] == "claude"]
    assert any("does not pass the check command" in p for p in prompts)


def test_manual_mode_waits_for_a_human_merge(repo):
    plan_and_approve(repo)
    env = {"ISSUEPILOT_AUTO_MERGE": "0"}
    repo.run_until(lambda: "agent:approved" in repo.gh()["prs"].get("3", {}).get("labels", []), env=env)
    repo.cli("run", "--once", "--no-reload", env=env)
    assert repo.pr(3)["state"] == "OPEN"
    assert repo.issue(1)["state"] == "OPEN"


def test_triage_creates_and_auto_approves_issues(repo):
    repo.script(proposals=[{"title": "Rate limit", "description": "login has none"}],
                triage=[{"index": 1, "action": "create", "duplicate_of": None, "title": "Add rate limit",
                         "body": "Background\n\nAdd it.", "reason": "security"}])
    plan_and_approve(repo)
    repo.cli("run", "--once", "--no-reload")
    created = [i for i in repo.gh()["issues"].values() if i["title"] == "Add rate limit"]
    assert len(created) == 1
    assert {"agent:ready", "agent:triaged"} <= set(created[0]["labels"])
    assert "found while building or reviewing PR #3" in created[0]["body"]


def test_triage_without_auto_ready_waits_for_approval(repo):
    repo.script(proposals=[{"title": "x", "description": "y"}],
                triage=[{"index": 1, "action": "create", "duplicate_of": None, "title": "Do x",
                         "body": "b", "reason": "r"}])
    plan_and_approve(repo)
    repo.cli("run", "--once", "--no-reload", env={"ISSUEPILOT_AUTO_READY": "0"})
    created = next(i for i in repo.gh()["issues"].values() if i["title"] == "Do x")
    assert created["labels"] == ["agent:planned"]


def test_agents_get_the_right_permissions(repo):
    plan_and_approve(repo)
    repo.run_until(lambda: repo.issue(1)["state"] == "CLOSED")
    calls = repo.agent_calls()
    planner = next(c for c in calls if c["cli"] == "claude" and "You are the planner" in c["prompt"])
    builder = next(c for c in calls if c["cli"] == "claude" and "You are the builder" in c["prompt"])
    reviewer = next(c for c in calls if c["cli"] == "codex")
    assert "--disallowedTools" in planner["argv"] and "bypassPermissions" not in " ".join(planner["argv"])
    assert builder["cwd"].endswith(".issuepilot/worktrees/issue-1")
    assert reviewer["argv"][:3] == ["exec", "-s", "read-only"]
    # the state directory stays out of git status
    assert "/.issuepilot/" in (repo.work / ".git" / "info" / "exclude").read_text()
    assert repo.git("status", "--porcelain") == ""


def test_chinese_comments_and_prompts(repo_zh):
    repo = repo_zh
    plan_and_approve(repo)
    repo.run_until(lambda: repo.issue(1)["state"] == "CLOSED")
    assert any("第 1 轮：✅ 通过" in c for c in repo.pr(3)["comments"])
    assert any("你是 builder" in c["prompt"] for c in repo.agent_calls())
    assert "已合并 PR #3" in (repo.work / ".issuepilot" / "loop.log").read_text()


def test_interrupted_build_is_resumed(repo):
    plan_and_approve(repo)
    repo.gh()  # labels exist; simulate a crash mid-build
    import json
    s = repo.gh()
    s["issues"]["1"]["labels"] = ["agent:building"]
    repo.state.write_text(json.dumps(s))
    out = repo.cli("run", "--once", "--no-reload").stdout
    assert "resuming interrupted issue #1" in out
    assert "agent:in-pr" in repo.issue(1)["labels"]


def test_doctor_passes_with_tools_installed(repo):
    repo.cli("setup")
    p = repo.cli("doctor", check=False)
    assert p.returncode == 0, p.stdout
    assert "[  ok] GitHub repository: acme/widgets" in p.stdout
    assert "auto_merge is on" in p.stdout


def test_init_writes_config_and_conventions(tmp_path):
    from conftest import Repo
    repo = Repo(tmp_path)
    (repo.work / ".issuepilot.toml").unlink()
    (repo.work / "AGENTS.md").unlink()
    repo.cli("init", "--language", "zh")
    assert 'language = "zh"' in (repo.work / ".issuepilot.toml").read_text()
    assert "项目约定" in (repo.work / "AGENTS.md").read_text()
    assert repo.cli("init", check=False).returncode != 0  # refuses to overwrite


def test_dependencies_hold_back_parallel_lanes(repo):
    repo.cli("setup")
    repo.cli("plan")
    repo.cli("approve", "1", "2")
    out = repo.cli("run", "--once", "--no-reload", env={"ISSUEPILOT_MAX_IN_FLIGHT": "2"}).stdout
    assert "#2 is waiting for [1]" in out
    assert "agent:ready" in repo.issue(2)["labels"] and "agent:in-pr" in repo.issue(1)["labels"]


def test_independent_issues_build_in_parallel(repo):
    repo.script(plan=[{"key": "a", "title": "A", "body": "a", "depends_on": []},
                      {"key": "b", "title": "B", "body": "b", "depends_on": []}])
    repo.cli("setup")
    repo.cli("plan")
    repo.cli("approve", "1", "2")
    repo.cli("run", "--once", "--no-reload", env={"ISSUEPILOT_MAX_IN_FLIGHT": "2"})
    assert all("agent:in-pr" in repo.issue(n)["labels"] for n in (1, 2))
    repo.run_until(lambda: all(repo.issue(n)["state"] == "CLOSED" for n in (1, 2)),
                   env={"ISSUEPILOT_MAX_IN_FLIGHT": "2"})
