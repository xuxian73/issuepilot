"""Command line entry point: `issuepilot <command>`."""

import argparse
import os
import sys
from pathlib import Path
from string import Template

from . import __version__, config, core
from .errors import StepError

TEMPLATES = Path(__file__).parent / "templates"


def load_config(require_file=True):
    try:
        cfg = config.load(require_file=require_file)
    except config.ConfigError as e:
        sys.exit(f"issuepilot: {e}")
    core.configure(cfg)
    return cfg


def cmd_init(args):
    try:
        root = config.repo_root()
    except config.ConfigError as e:
        sys.exit(f"issuepilot: {e}")
    path = root / config.CONFIG_NAME
    if path.exists() and not args.force:
        sys.exit(f"issuepilot: {path} already exists (use --force to overwrite)")
    path.write_text(Template((TEMPLATES / "issuepilot.toml").read_text()).safe_substitute(language=args.language))
    print(f"wrote {path}")
    conventions = root / "AGENTS.md"
    if not conventions.exists():
        conventions.write_text((TEMPLATES / f"AGENTS.{args.language}.md").read_text())
        print(f"wrote {conventions} (edit it: every agent reads it first)")
    print("\nNext steps:\n"
          "  1. Edit .issuepilot.toml and AGENTS.md, and write your spec (default docs/spec.md).\n"
          "  2. issuepilot doctor      # check tools, login and repository setup\n"
          "  3. issuepilot setup       # create the agent:* labels on GitHub\n"
          "  4. issuepilot plan        # the planner opens issues labeled agent:planned\n"
          "  5. issuepilot approve N…  # then: issuepilot run")


def cmd_doctor(_):
    from .doctor import run
    sys.exit(run())


def cmd_setup(_):
    load_config()
    core.cmd_setup()


def cmd_plan(args):
    cfg = load_config()
    spec = args.spec or cfg.spec
    if not (cfg.root / spec).exists():
        sys.exit(f"issuepilot: spec file {spec} not found")
    core.cmd_plan(spec)


def cmd_approve(args):
    load_config()
    core.cmd_approve(args.issues)


def reexec():
    os.execv(sys.executable, [sys.executable, "-m", "issuepilot", *sys.argv[1:]])


def cmd_run(args):
    load_config()
    core.cmd_run(once=args.once, reload=None if args.no_reload else reexec)


def cmd_status(_):
    load_config()
    core.cmd_status()


def cmd_dashboard(_):
    cfg = load_config()
    try:
        from . import dashboard
    except ImportError:
        sys.exit("issuepilot: the dashboard needs `rich`. Install it with: pip install 'issuepilot[dashboard]'")
    dashboard.main(cfg)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="issuepilot",
                                 description="Turn a spec into GitHub issues, then build, review and merge them "
                                             "with coding agents.")
    ap.add_argument("--version", action="version", version=f"issuepilot {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="write .issuepilot.toml (and AGENTS.md if missing) in this repository")
    p.add_argument("--language", choices=("en", "zh"), default="en")
    p.add_argument("--force", action="store_true", help="overwrite an existing .issuepilot.toml")
    p.set_defaults(fn=cmd_init)
    sub.add_parser("doctor", help="check tools, login and repository setup").set_defaults(fn=cmd_doctor)
    sub.add_parser("setup", help="create the agent:* labels in the GitHub repository").set_defaults(fn=cmd_setup)
    p = sub.add_parser("plan", help="let the planner split the spec into issues (labeled agent:planned)")
    p.add_argument("--spec", help="spec file, relative to the repository root (default: `spec` in the config)")
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("approve", help="move issues from agent:planned to agent:ready")
    p.add_argument("issues", nargs="+", type=int)
    p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("run", help="run the build/review loop")
    p.add_argument("--once", action="store_true", help="run one round and exit")
    p.add_argument("--no-reload", action="store_true", help="do not restart when the code or config changes")
    p.set_defaults(fn=cmd_run)
    sub.add_parser("status", help="list open issues and PRs by label").set_defaults(fn=cmd_status)
    sub.add_parser("dashboard", help="live terminal dashboard (needs rich)").set_defaults(fn=cmd_dashboard)
    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except StepError as e:
        sys.exit(f"issuepilot: {e}")
    except KeyboardInterrupt:
        sys.exit(130)
