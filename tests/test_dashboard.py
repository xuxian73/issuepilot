import subprocess

import pytest

pytest.importorskip("rich")

from rich.console import Console  # noqa: E402

from issuepilot import config, dashboard  # noqa: E402


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_render_shows_issue_states(tmp_path, lang):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    cfg = config.load(tmp_path, env={}, require_file=False)
    cfg.language = lang
    dashboard.CFG = cfg
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
