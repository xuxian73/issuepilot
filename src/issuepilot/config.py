"""Load `.issuepilot.toml` from the target repository.

Every key has a default, so an empty file works. Any scalar key can also be
overridden with an environment variable: `ISSUEPILOT_<KEY>` in upper case, for
example `ISSUEPILOT_AUTO_MERGE=1` or `ISSUEPILOT_MAX_IN_FLIGHT=3`.
"""

import os
import subprocess
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

CONFIG_NAME = ".issuepilot.toml"
CLIS = ("claude", "codex")
ROLES = ("planner", "builder", "reviewer")
NOTE_KEYS = ("plan", "triage", "build", "checkfail", "fix", "review", "arbitrate")


class ConfigError(Exception):
    pass


@dataclass
class Agent:
    cli: str
    model: str = ""


@dataclass
class Config:
    root: Path
    spec: str = "docs/spec.md"
    conventions: str = "AGENTS.md"
    check: str = "./scripts/check.sh"
    base_branch: str = "main"
    language: str = "en"
    state_dir: str = ".issuepilot"
    prompts_dir: str = ""
    # [automation]
    auto_merge: bool = True
    auto_ready: bool = True
    max_in_flight: int = 1
    max_fix_rounds: int = 3
    max_check_retries: int = 2
    max_triaged_issues: int = 8
    poll_seconds: int = 60
    agent_timeout: int = 3600
    check_timeout: int = 1800
    ci_timeout: int = 1800
    # [agents]
    planner: Agent = field(default_factory=lambda: Agent("claude", "opus"))
    builder: Agent = field(default_factory=lambda: Agent("claude"))
    reviewer: Agent = field(default_factory=lambda: Agent("codex"))
    # [notes]: extra project-specific text appended to each prompt
    notes: dict = field(default_factory=dict)

    @property
    def state(self):
        return self.root / self.state_dir

    @property
    def runs(self):
        return self.state / "runs"

    @property
    def worktrees(self):
        return self.state / "worktrees"

    @property
    def config_path(self):
        return self.root / CONFIG_NAME


SCALARS = {f.name: f.type for f in fields(Config) if f.type in (str, int, bool)}


def repo_root(start=None):
    p = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start or os.getcwd(),
                       text=True, capture_output=True)
    if p.returncode != 0:
        raise ConfigError("not inside a git repository")
    return Path(p.stdout.strip())


def _coerce(name, kind, value, source):
    if kind is bool:
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("1", "true", "yes", "on"):
            return True
        if str(value).lower() in ("0", "false", "no", "off", ""):
            return False
    elif kind is int:
        try:
            if not isinstance(value, bool):
                return int(value)
        except (TypeError, ValueError):
            pass
    elif kind is str and isinstance(value, str):
        return value
    raise ConfigError(f"{source}: `{name}` must be {kind.__name__}, got {value!r}")


def load(root=None, env=None, require_file=True):
    root = Path(root) if root else repo_root()
    env = os.environ if env is None else env
    path = root / CONFIG_NAME
    if path.exists():
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{CONFIG_NAME}: {e}")
    elif require_file:
        raise ConfigError(f"{CONFIG_NAME} not found in {root}. Run `issuepilot init` first.")
    else:
        data = {}

    cfg = Config(root=root)
    flat = dict(data)
    flat.update(flat.pop("automation", {}) or {})
    agents = flat.pop("agents", {}) or {}
    notes = flat.pop("notes", {}) or {}
    for key, value in flat.items():
        if key not in SCALARS:
            raise ConfigError(f"{CONFIG_NAME}: unknown key `{key}`")
        setattr(cfg, key, _coerce(key, SCALARS[key], value, CONFIG_NAME))
    for key, kind in SCALARS.items():
        var = f"ISSUEPILOT_{key.upper()}"
        if var in env:
            setattr(cfg, key, _coerce(key, kind, env[var], var))

    for role, spec in agents.items():
        if role not in ROLES:
            raise ConfigError(f"{CONFIG_NAME}: unknown agent role `{role}` (expected {', '.join(ROLES)})")
        if not isinstance(spec, dict) or set(spec) - {"cli", "model"}:
            raise ConfigError(f"{CONFIG_NAME}: agents.{role} must be a table with `cli` and optional `model`")
        default = getattr(cfg, role)
        cli = spec.get("cli", default.cli)
        # switching the CLI drops the default model, which belongs to the other CLI
        model = spec.get("model", default.model if cli == default.cli else "")
        setattr(cfg, role, Agent(cli, model))
    for role in ROLES:
        a = getattr(cfg, role)
        a.cli = env.get(f"ISSUEPILOT_{role.upper()}_CLI", a.cli)
        a.model = env.get(f"ISSUEPILOT_{role.upper()}_MODEL", a.model)
        if a.cli not in CLIS:
            raise ConfigError(f"agents.{role}.cli must be one of {', '.join(CLIS)}, got {a.cli!r}")

    for key, text in notes.items():
        if key not in NOTE_KEYS:
            raise ConfigError(f"{CONFIG_NAME}: unknown notes key `{key}` (expected {', '.join(NOTE_KEYS)})")
        if not isinstance(text, str):
            raise ConfigError(f"{CONFIG_NAME}: notes.{key} must be a string")
    cfg.notes = notes

    if cfg.language not in ("en", "zh"):
        raise ConfigError(f"language must be `en` or `zh`, got {cfg.language!r}")
    for key in ("max_in_flight", "poll_seconds", "agent_timeout", "check_timeout", "ci_timeout"):
        if getattr(cfg, key) < 1:
            raise ConfigError(f"`{key}` must be at least 1")
    for key in ("max_fix_rounds", "max_check_retries", "max_triaged_issues"):
        if getattr(cfg, key) < 0:
            raise ConfigError(f"`{key}` must not be negative")
    return cfg
