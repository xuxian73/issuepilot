import subprocess

import pytest

pytest.importorskip("rich")

from rich.console import Console  # noqa: E402

from issuepilot import config, dashboard  # noqa: E402


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_render_shows_issue_states(tmp_path, lang, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    cfg = config.load(tmp_path, env={}, require_file=False)
    cfg.language = lang
    dashboard.CFG = cfg
    monkeypatch.setattr(dashboard, "process_snapshot", lambda: {"agents": [], "alive": False, "unavailable": False})
    (cfg.state).mkdir()
    (cfg.state / "loop.log").write_text("[10:00:00] [#1]   ✓ opened PR https://x/pull/3\nTraceback (ignored)\n")
    gh = dashboard.GitHub()
    gh.issues = [
        {"number": 1, "title": "Feature A", "state": "OPEN", "labels": [{"name": "agent:in-pr"}], "body": ""},
        {"number": 2, "title": "Feature B", "state": "OPEN", "labels": [{"name": "agent:ready"}],
         "body": "Depends on: #1"},
    ]
    gh.prs = [{"number": 3, "headRefName": "agent/issue-1", "state": "OPEN",
               "labels": [{"name": "agent:fixing"}, {"name": "round:2"}]}]
    console = Console(width=120, record=True)
    console.print(dashboard.render(gh, [], {}, (120, 40)))
    out = console.export_text()
    status = dashboard.TEXT[lang]
    assert "Feature A" in out and status["fixing"] in out and status["waiting"] in out
    assert "opened PR" in out and "Traceback" not in out
    assert status["no_agents"] in out


def test_macos_processes_are_scoped_and_codex_children_are_not_double_counted(tmp_path, monkeypatch):
    cfg = config.Config(root=tmp_path / "repo with spaces")
    monkeypatch.setattr(dashboard, "CFG", cfg)
    monkeypatch.setattr(dashboard.sys, "platform", "darwin")
    monkeypatch.setattr(dashboard.time, "time", lambda: 200000)
    wt = str(cfg.worktrees / "issue-21")
    ps_output = "\n".join([
        "100 1 01:02 Python /opt/python -m issuepilot run",
        f"110 100 00:30 node /opt/node /opt/codex.js exec -C {wt} --output-schema /schemas/builder.json -",
        f"111 110 00:29 codex /opt/codex exec -C {wt} --output-schema /schemas/builder.json -",
        "120 100 1-02:03:04 codex /opt/codex exec --output-schema /schemas/reviewer.json -",
        "130 100 00:01 sh /bin/sh -c ./scripts/check.sh",
        "140 100 10:01 claude /opt/claude -p --json-schema {}",
        "150 1 01:02 Python /opt/python -m issuepilot run",
        "151 1 01:02 codex /opt/codex exec --output-schema /schema.json -",
        "160 1 00:01 node /opt/node /tmp/other.js codex exec --output-schema /schema.json",
        "170 1 00:01 Python /opt/python -c print('issuepilot run')",
        "180 1 00:01 codex /opt/codex exec --output-schema /schema.json -",
        "malformed row",
    ])
    cwds = {100: str(cfg.root), 110: wt, 111: wt, 120: str(cfg.worktrees / "issue-22"),
            130: wt, 140: wt, 150: str(tmp_path / "other"), 151: str(cfg.root) + "-other"}
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "ps":
            return subprocess.CompletedProcess(cmd, 0, ps_output, "")
        assert cmd[0] == "lsof"
        # PID 180 exited between ps and lsof. A partial result must remain usable.
        return subprocess.CompletedProcess(cmd, 1, "".join(f"p{pid}\nn{cwd}\n" for pid, cwd in cwds.items()), "")

    monkeypatch.setattr(dashboard.subprocess, "run", run)
    result = dashboard.process_snapshot()
    assert result["alive"] is True and result["unavailable"] is False
    assert {(a["pid"], a["kind"]) for a in result["agents"]} == {
        (111, "codex"), (120, "codex"), (130, "check"), (140, "claude")}
    assert result["agents"][0]["cwd"] == wt
    assert next(a for a in result["agents"] if a["pid"] == 120)["started"] == 200000 - 93784
    assert len(calls) == 2
    assert "160" not in calls[1][3] and "170" not in calls[1][3]


@pytest.mark.parametrize("command", [
    "/opt/python -m issuepilot run --no-reload",
    "/opt/Python3.13 -u -m issuepilot run",
    "/path with spaces/python /another path/issuepilot run",
])
def test_macos_recognizes_runner_entrypoints(command):
    assert dashboard._mac_kind("Python", command) == "loop"


@pytest.mark.parametrize("lang", ["en", "zh"])
@pytest.mark.parametrize("failure", ["missing_proc", "ps_denied", "lsof_missing", "lsof_timeout", "unsupported"])
def test_process_detection_failure_keeps_dashboard_usable(tmp_path, monkeypatch, lang, failure):
    cfg = config.Config(root=tmp_path, language=lang)
    cfg.state.mkdir()
    (cfg.state / "loop.log").write_text("[10:00:00] loop log still visible\n")
    monkeypatch.setattr(dashboard, "CFG", cfg)
    monkeypatch.setattr(dashboard.sys, "platform", "linux" if failure == "missing_proc" else
                        "win32" if failure == "unsupported" else "darwin")

    def listdir(path):
        raise FileNotFoundError(path)

    def run(cmd, **kwargs):
        if failure == "ps_denied":
            raise PermissionError("process access denied")
        if cmd[0] == "ps":
            return subprocess.CompletedProcess(cmd, 0, "100 1 00:01 Python /opt/python -m issuepilot run", "")
        if failure == "lsof_missing":
            raise FileNotFoundError("lsof")
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(dashboard.os, "listdir", listdir)
    monkeypatch.setattr(dashboard.subprocess, "run", run)
    snapshot = dashboard.process_snapshot()
    assert snapshot == {"agents": [], "alive": None, "unavailable": True}
    gh = dashboard.GitHub()
    gh.issues = [{"number": 21, "title": "Follow-up task", "state": "OPEN", "labels": [], "body": ""}]
    console = Console(width=160, record=True)
    console.print(dashboard.render(gh, [], {}, (160, 40), snapshot))
    out = console.export_text()
    assert "Follow-up task" in out and "loop log still visible" in out
    assert dashboard.TEXT[lang]["process_unknown"] in out
    assert dashboard.TEXT[lang]["process_unavailable"] in out
    assert dashboard.TEXT[lang]["dead"] not in out


def test_macos_idle_without_candidates_does_not_require_lsof(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "CFG", config.Config(root=tmp_path))
    monkeypatch.setattr(dashboard.sys, "platform", "darwin")

    def run(cmd, **kwargs):
        assert cmd[0] == "ps"
        return subprocess.CompletedProcess(cmd, 0, "200 1 00:01 node /opt/node app.js", "")

    monkeypatch.setattr(dashboard.subprocess, "run", run)
    assert dashboard.process_snapshot() == {"agents": [], "alive": False, "unavailable": False}


def test_linux_proc_snapshot_still_recognizes_module_runner(tmp_path, monkeypatch):
    cfg = config.Config(root=tmp_path)
    monkeypatch.setattr(dashboard, "CFG", cfg)
    monkeypatch.setattr(dashboard.sys, "platform", "linux")
    monkeypatch.setattr(dashboard.os, "listdir", lambda _: ["self", "100", "101", "102"])
    monkeypatch.setattr(dashboard.os, "readlink", lambda _: str(cfg.root))
    path_type = type(tmp_path)
    argv = {"100": b"python\0-m\0issuepilot\0run\0",
            "101": b"codex\0exec\0--output-schema\0schema.json\0", "102": b"node\0other.js\0"}
    monkeypatch.setattr(path_type, "read_bytes", lambda p: argv[p.parent.name])
    monkeypatch.setattr(path_type, "read_text", lambda p: f"{p.parent.name} (name with spaces) S 1 0 0")
    monkeypatch.setattr(path_type, "stat", lambda p: type("Stat", (), {"st_mtime": 1000})())
    snapshot = dashboard.process_snapshot()
    assert snapshot["alive"] is True and snapshot["unavailable"] is False
    assert [(a["pid"], a["kind"]) for a in snapshot["agents"]] == [(101, "codex")]
