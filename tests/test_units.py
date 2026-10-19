import re
import subprocess
from pathlib import Path

import pytest

from issuepilot import agents, config, core, i18n
from issuepilot.config import Agent
from issuepilot.errors import StepError

PROMPT_VARS = {
    "build": ["number", "branch", "title", "body"],
    "checkfail": ["number", "branch", "title", "body", "log"],
    "fix": ["pr", "number", "branch", "round", "title", "body", "findings", "human", "conflict"],
    "review": ["pr", "number", "title", "body", "builder_summary", "previous", "triage", "human"],
    "arbitrate": ["pr", "number", "title", "body", "rounds", "history", "human"],
    "plan": ["spec_text", "existing"],
    "triage": ["source", "proposals", "open_issues", "create_note"],
}


@pytest.fixture
def git_root(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def test_defaults_without_a_file(git_root):
    cfg = config.load(git_root, env={}, require_file=False)
    assert (cfg.check, cfg.auto_merge, cfg.max_in_flight) == ("./scripts/check.sh", True, 1)
    assert cfg.planner == Agent("claude", "opus") and cfg.reviewer == Agent("codex", "")
    assert cfg.worktrees == git_root / ".issuepilot" / "worktrees"


def test_missing_file_is_an_error_by_default(git_root):
    with pytest.raises(config.ConfigError, match="issuepilot init"):
        config.load(git_root, env={})


def test_file_and_env_overrides(git_root):
    (git_root / ".issuepilot.toml").write_text(
        'check = "make test"\nlanguage = "zh"\n[automation]\nauto_merge = false\nmax_in_flight = 3\n'
        '[agents]\nplanner = { cli = "codex" }\nbuilder = { model = "sonnet" }\n[notes]\nreview = "check roles"\n')
    cfg = config.load(git_root, env={"ISSUEPILOT_MAX_IN_FLIGHT": "5", "ISSUEPILOT_AUTO_READY": "0",
                                     "ISSUEPILOT_REVIEWER_MODEL": "o9"})
    assert (cfg.check, cfg.language, cfg.auto_merge, cfg.auto_ready, cfg.max_in_flight) == ("make test", "zh", False, False, 5)
    assert cfg.planner == Agent("codex", "")  # switching CLI drops the other CLI's default model
    assert cfg.builder == Agent("claude", "sonnet")
    assert cfg.reviewer == Agent("codex", "o9")
    assert cfg.notes == {"review": "check roles"}


@pytest.mark.parametrize("text, match", [
    ("bogus = 1\n", "unknown key `bogus`"),
    ("[automation]\nmax_in_flight = \"many\"\n", "must be int"),
    ("[automation]\nauto_merge = 3\n", "must be bool"),
    ("[agents]\nplanner = { cli = \"gpt\" }\n", "must be one of"),
    ("[agents]\ntester = { cli = \"claude\" }\n", "unknown agent role"),
    ("[notes]\nreviewer = \"x\"\n", "unknown notes key"),
    ("language = \"fr\"\n", "language"),
    ("[automation]\nmax_in_flight = 0\n", "at least 1"),
    ("check = [\n", ".issuepilot.toml"),
])
def test_invalid_config(git_root, text, match):
    (git_root / ".issuepilot.toml").write_text(text)
    with pytest.raises(config.ConfigError, match=re.escape(match)):
        config.load(git_root, env={})


def test_invalid_env_value(git_root):
    with pytest.raises(config.ConfigError, match="ISSUEPILOT_POLL_SECONDS"):
        config.load(git_root, env={"ISSUEPILOT_POLL_SECONDS": "soon"}, require_file=False)


def test_messages_have_the_same_keys_in_every_language():
    assert set(i18n.MESSAGES["en"]) == set(i18n.MESSAGES["zh"])
    for key in i18n.MESSAGES["en"]:
        fields = lambda s: set(re.findall(r"{(\w+)}", s))
        assert fields(i18n.MESSAGES["en"][key]) == fields(i18n.MESSAGES["zh"][key]), key


def test_every_label_has_a_description():
    for lang in i18n.MESSAGES:
        for name in core.LABELS:
            assert f"label.{name}" in i18n.MESSAGES[lang]


@pytest.mark.parametrize("lang", ["en", "zh"])
@pytest.mark.parametrize("name", PROMPT_VARS)
def test_prompts_have_no_unfilled_variables(git_root, lang, name):
    cfg = config.load(git_root, env={}, require_file=False)
    cfg.language = lang
    core.configure(cfg)
    text = core.render(name, **{v: f"<{v}>" for v in PROMPT_VARS[name]})
    assert not re.findall(r"\$[a-z_]+", text), text
    for v in PROMPT_VARS[name]:
        assert f"<{v}>" in text or v == "conflict"
    assert "./scripts/check.sh" in text or name in ("triage", "arbitrate")


def test_notes_and_prompt_overrides(git_root):
    cfg = config.load(git_root, env={}, require_file=False)
    cfg.notes = {"review": "Every route checks roles."}
    cfg.prompts_dir = "my-prompts"
    core.configure(cfg)
    assert "## Project notes\n\nEvery route checks roles." in core.render("review")
    assert "Project notes" not in core.render("build")
    (git_root / "my-prompts").mkdir()
    (git_root / "my-prompts" / "build.md").write_text("custom $number with $check")
    assert core.render("build", number=7) == "custom 7 with ./scripts/check.sh"


def test_depends_on_and_payloads():
    assert core.depends_on("text\n\n---\nDepends on: #3, #12") == [3, 12]
    assert core.depends_on("no deps") == []
    data = {"summary": "中文 ok", "findings": []}
    body = f"<!-- agent:review round=2 data={core.encode(data)} -->\n### title"
    assert core.agent_payloads([body, "human text"], "review") == [(2, data)]
    assert core.agent_payloads([body], "builder") == []


def test_retrying_only_retries_transient_errors(monkeypatch):
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    monkeypatch.setattr(core, "log", lambda m: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise StepError("Post https://api.github.com/graphql: EOF")
        return "ok"
    assert core.retrying(flaky, "x") == "ok" and len(calls) == 3
    with pytest.raises(StepError, match="not found"):
        core.retrying(lambda: (_ for _ in ()).throw(StepError("not found")), "x")


def test_ensure_state_excluded_is_idempotent(git_root):
    core.configure(config.load(git_root, env={}, require_file=False))
    core.ensure_state_excluded()
    core.ensure_state_excluded()
    assert (git_root / ".git" / "info" / "exclude").read_text().count("/.issuepilot/") == 1


def test_agent_commands():
    claude_ro = agents.claude_cmd("review", "", writable=False)
    assert "bypassPermissions" not in claude_ro and "Edit" in claude_ro
    claude_rw = agents.claude_cmd("build", "sonnet", writable=True)
    assert claude_rw[claude_rw.index("--model") + 1] == "sonnet" and "Bash(git push:*)" in claude_rw
    codex_rw = agents.codex_cmd("build", "o9", Path("/w"), Path("/o.json"), True)
    assert codex_rw[:4] == ["codex", "exec", "-s", "workspace-write"]
    assert codex_rw[-3:] == ["-m", "o9", "-"]
    assert agents.codex_cmd("review", "", Path("/w"), Path("/o.json"), False)[:4] == \
        ["codex", "exec", "-s", "read-only"]


def test_commit_rule_depends_on_the_builder_cli(git_root):
    cfg = config.load(git_root, env={}, require_file=False)
    core.configure(cfg)
    assert "Commit with `git commit`" in core.render("build")
    cfg.builder = Agent("codex")
    assert "Do not commit" in core.render("build") and "Do not commit" in core.render("fix")
