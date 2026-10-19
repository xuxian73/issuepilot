import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FAKEBIN = Path(__file__).parent / "fakebin"

CONFIG = """\
spec = "docs/spec.md"
check = "true"
language = "{language}"

[automation]
auto_merge = true
auto_ready = true
poll_seconds = 1
"""


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Repo:
    """A local checkout with a bare `origin`, plus fake gh/claude/codex on PATH."""

    def __init__(self, tmp, language="en"):
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.work = tmp / "work"
        self.state = tmp / "gh.json"
        self.script_path = tmp / "script.json"
        self.agent_log = tmp / "agents.jsonl"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("ISSUEPILOT_")}
        self.env.update(
            PATH=f"{FAKEBIN}{os.pathsep}{os.environ['PATH']}",
            FAKE_GH_STATE=str(self.state), FAKE_ORIGIN=str(self.origin), FAKE_SCRIPT=str(self.script_path),
            FAKE_AGENT_LOG=str(self.agent_log), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
        )
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True, env=self.env)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.work)], check=True, env=self.env,
                       capture_output=True)
        (self.work / "docs").mkdir()
        (self.work / "docs" / "spec.md").write_text("# Spec\n\nF1: feature A\nF2: feature B\n")
        (self.work / "AGENTS.md").write_text("# Conventions\n")
        (self.work / ".issuepilot.toml").write_text(CONFIG.format(language=language))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.git("push", "-q", "origin", "HEAD:main")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.work, check=True, capture_output=True, text=True,
                              env=self.env).stdout.strip()

    def script(self, **kw):
        self.script_path.write_text(json.dumps(kw))

    def cli(self, *args, check=True, env=None):
        p = subprocess.run([sys.executable, "-m", "issuepilot", *args], cwd=self.work, text=True,
                           capture_output=True, env={**self.env, **(env or {})}, timeout=120)
        if check and p.returncode != 0:
            raise AssertionError(f"issuepilot {' '.join(args)} failed ({p.returncode}):\n{p.stdout}\n{p.stderr}")
        return p

    def gh(self):
        return json.loads(self.state.read_text())

    def issue(self, n):
        return self.gh()["issues"][str(n)]

    def pr(self, n):
        return self.gh()["prs"][str(n)]

    def agent_calls(self):
        if not self.agent_log.exists():
            return []
        return [json.loads(l) for l in self.agent_log.read_text().splitlines()]

    def run_until(self, done, rounds=12, env=None):
        out = []
        for _ in range(rounds):
            p = self.cli("run", "--once", "--no-reload", env=env)
            out.append(p.stdout + p.stderr)
            if done():
                return "\n".join(out)
        raise AssertionError("condition not reached; output:\n" + "\n".join(out))

    def origin_files(self):
        return git("ls-tree", "-r", "--name-only", "main", cwd=self.origin).splitlines()


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


@pytest.fixture
def repo_zh(tmp_path):
    return Repo(tmp_path, language="zh")
