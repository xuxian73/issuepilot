"""Run one agent call (Claude Code or Codex) and return its structured output."""

import json
import subprocess
from pathlib import Path

from . import errors

SCHEMAS = Path(__file__).parent / "schemas"

# Read-only Claude Code may still inspect git history, which the reviewer needs.
CLAUDE_READ_ONLY_TOOLS = ["Read", "Grep", "Glob", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
                          "Bash(git status:*)"]


def schema_path(name):
    return SCHEMAS / f"{name}.json"


def claude_cmd(schema, model, writable):
    cmd = ["claude", "-p", "--output-format", "json", "--json-schema", schema_path(schema).read_text()]
    if model:
        cmd += ["--model", model]
    if writable:
        # the builder may edit files and run commands in its own worktree, but must not touch the remote
        cmd += ["--permission-mode", "bypassPermissions",
                "--disallowedTools", "Bash(git push:*)", "Bash(gh:*)"]
    else:
        cmd += ["--permission-mode", "default", "--allowedTools", *CLAUDE_READ_ONLY_TOOLS,
                "--disallowedTools", "Edit", "Write", "NotebookEdit"]
    return cmd


def codex_cmd(schema, model, cwd, out, writable):
    if writable:
        # workspace-write has no network access, so the builder cannot push. It also keeps .git read-only,
        # so the builder cannot commit either; the orchestrator commits its changes (see commit_leftovers).
        cmd = ["codex", "exec", "-s", "workspace-write"]
    else:
        cmd = ["codex", "exec", "-s", "read-only"]
    cmd += ["-C", str(cwd), "--output-schema", str(schema_path(schema)), "-o", str(out)]
    if model:
        cmd += ["-m", model]
    return cmd + ["-"]  # the prompt comes from stdin


def run(agent, prompt, schema, cwd, name, writable, timeout, log_path):
    """Run `agent` (a config.Agent) and return the parsed JSON object it produced.

    `log_path(name, ext)` returns a fresh path for a run log. Raises errors.StepError on failure.
    """
    if agent.cli == "claude":
        cmd = claude_cmd(schema, agent.model, writable)
        out = None
    else:
        out = Path(log_path(f"codex-{name}", "json")).resolve()  # codex resolves relative paths against -C
        cmd = codex_cmd(schema, agent.model, cwd, out, writable)
    try:
        p = subprocess.run(cmd, cwd=cwd, input=prompt, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise errors.StepError(f"{agent.cli} {name}: timed out after {timeout}s")
    except FileNotFoundError:
        raise errors.StepError(f"{agent.cli} {name}: `{agent.cli}` is not installed or not on PATH")
    log_path(f"{agent.cli}-{name}").write_text(
        f"{prompt}\n\n===== STDOUT =====\n{p.stdout}\n===== STDERR =====\n{p.stderr}")

    if agent.cli == "claude":
        try:
            data = json.loads(p.stdout)
        except json.JSONDecodeError:
            raise errors.StepError(f"claude {name}: output is not JSON: {p.stderr[-2000:] or p.stdout[-2000:]}")
        if data.get("is_error") or data.get("subtype") != "success" or "structured_output" not in data:
            raise errors.StepError(f"claude {name} failed: {data.get('subtype')} {str(data.get('result'))[:2000]}")
        return data["structured_output"]

    if p.returncode != 0 or not out.exists():
        raise errors.StepError(f"codex {name} failed ({p.returncode}): {p.stderr[-2000:]}")
    try:
        # codex sometimes writes the same JSON object twice; take the first one
        obj, _ = json.JSONDecoder().raw_decode(out.read_text().strip())
    except json.JSONDecodeError:
        raise errors.StepError(f"codex {name}: output is not JSON: {out.read_text()[-2000:]}")
    return obj
