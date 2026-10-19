"""`issuepilot doctor`: check everything the loop needs before it runs unattended."""

import json
import shlex
import shutil
import subprocess
import sys

from . import config, core

OK, WARN, FAIL = "ok", "warn", "FAIL"


def _run(cmd, cwd=None):
    try:
        p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=60)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def checks():
    """Yield (status, message) pairs."""
    if sys.version_info < (3, 11):
        yield FAIL, f"Python 3.11+ is required, this is {sys.version.split()[0]}"
    try:
        root = config.repo_root()
    except config.ConfigError as e:
        yield FAIL, str(e)
        return
    yield OK, f"repository: {root}"
    try:
        cfg = config.load(root)
    except config.ConfigError as e:
        yield FAIL, str(e)
        return
    yield OK, f"config: {cfg.config_path.name} (language {cfg.language})"

    if not shutil.which("gh"):
        yield FAIL, "`gh` (GitHub CLI) is not installed"
    else:
        code, out = _run(["gh", "auth", "status"], root)
        yield (OK, "gh is logged in") if code == 0 else (FAIL, f"gh is not logged in: run `gh auth login`\n{out}")
        code, out = _run(["gh", "repo", "view", "--json", "nameWithOwner"], root)
        if code == 0:
            yield OK, f"GitHub repository: {json.loads(out)['nameWithOwner']}"
            code, out = _run(["gh", "label", "list", "--limit", "200", "--json", "name"], root)
            have = {l["name"] for l in json.loads(out)} if code == 0 else set()
            missing = [l for l in core.LABELS if l not in have]
            if missing:
                yield WARN, f"{len(missing)} agent:* labels are missing: run `issuepilot setup`"
            else:
                yield OK, "labels exist"
        else:
            yield FAIL, f"gh cannot see a GitHub repository for this checkout:\n{out}"

    code, _ = _run(["git", "ls-remote", "--exit-code", "--heads", "origin", cfg.base_branch], root)
    yield (OK, f"origin/{cfg.base_branch} exists") if code == 0 else \
        (FAIL, f"cannot find branch {cfg.base_branch} on remote origin")

    for role in config.ROLES:
        agent = getattr(cfg, role)
        if shutil.which(agent.cli):
            yield OK, f"{role}: {agent.cli}" + (f" (model {agent.model})" if agent.model else "")
        else:
            yield FAIL, f"{role}: `{agent.cli}` is not installed or not on PATH"

    first = shlex.split(cfg.check)[0] if cfg.check.strip() else ""
    if not first:
        yield FAIL, "`check` is empty"
    elif "/" in first and not (root / first).exists():
        yield WARN, f"check command `{cfg.check}`: {first} does not exist yet (the first issue can create it)"
    else:
        yield OK, f"check command: {cfg.check}"
    for key in ("spec", "conventions"):
        path = getattr(cfg, key)
        yield (OK, f"{key}: {path}") if (root / path).exists() else (WARN, f"{key} file {path} does not exist")

    workflows = root / ".github" / "workflows"
    if not any(workflows.glob("*.y*ml")):
        yield WARN, "no GitHub Actions workflow: PRs go to review without a CI check on a clean machine"
    gates = [g for g, on in (("merging PRs", not cfg.auto_merge), ("starting issues", not cfg.auto_ready)) if on]
    yield OK, ("fully automatic: no human approval needed" if not gates
               else f"waits for a human before {' and '.join(gates)}")
    try:
        import rich  # noqa: F401
    except ImportError:
        yield WARN, "rich is not installed, so `issuepilot dashboard` will not work: pip install 'issuepilot[dashboard]'"


def run():
    failed = False
    for status, msg in checks():
        failed |= status == FAIL
        print(f"[{status:>4}] {msg}")
    return 1 if failed else 0
