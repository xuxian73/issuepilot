"""`issuepilot dashboard`: issue states and live progress of the running agents.

Read-only. Data comes from GitHub, the git worktrees, the agents' session transcripts and the loop log
(`<state_dir>/loop.log`, written by `issuepilot run`).
"""

import json
import os
import re
import subprocess
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
GH_REFRESH = 20  # seconds between GitHub refreshes
LOCAL_REFRESH = 2  # seconds between local refreshes

CFG = None

STATUS_STYLE = {
    "merged": "green", "closed": "dim", "needs_human": "bold red", "building": "bold yellow",
    "reviewing": "bold magenta", "fixing": "bold red3", "approved": "green", "ready": "cyan",
    "waiting": "dim", "planned": "blue", "in_pr": "magenta", "unknown": "dim",
}

TEXT = {
    "en": {
        "merged": "merged", "closed": "closed", "needs_human": "needs human", "building": "building",
        "reviewing": "reviewing", "fixing": "fixing", "approved": "approved", "ready": "ready",
        "waiting": "blocked", "planned": "planned", "in_pr": "in PR", "unknown": "unknown",
        "timeout": "timed out", "unknown_error": "unknown error",
        "stale": "showing data from {s}s ago", "not_loaded": "no data yet", "gh_failed": "GitHub refresh failed, {stale}: {e}",
        "no_agents": "No agent is running.", "agent": "agent", "repo_root": "repository root",
        "running_for": "running {age}", "running_check": "running checks", "commits": "{n} commit(s)",
        "no_diff": "no diff", "dirty": "uncommitted files {n}", "todos": "Todos", "recent": "Recent actions",
        "no_transcript": "(no transcript found yet)", "log": "Loop log", "no_log": "(no log yet)",
        "title": " {repo} · issuepilot ", "alive": "● loop running", "dead": "● loop not running",
        "updated": "GitHub updated {s}s ago", "done": "done {d}/{n}  ",
        "col_status": "Status", "col_round": "Rnd", "col_deps": "Deps",
    },
    "zh": {
        "merged": "已合并", "closed": "已关闭", "needs_human": "需要人工", "building": "开发中",
        "reviewing": "审查中", "fixing": "修复中", "approved": "已通过", "ready": "待开发",
        "waiting": "等依赖", "planned": "待确认", "in_pr": "已开 PR", "unknown": "未知",
        "timeout": "超时", "unknown_error": "未知错误",
        "stale": "显示的是 {s}s 前的数据", "not_loaded": "还没拉到数据", "gh_failed": "GitHub 拉取失败，{stale}：{e}",
        "no_agents": "现在没有 agent 在运行。", "agent": "当前 agent", "repo_root": "仓库根目录",
        "running_for": "已运行 {age}", "running_check": "正在跑检查", "commits": "提交 {n} 个",
        "no_diff": "与基础分支无差异", "dirty": "未提交文件 {n}", "todos": "待办", "recent": "最近动作",
        "no_transcript": "（还没找到会话记录）", "log": "调度器日志", "no_log": "（还没有日志）",
        "title": " {repo} · issuepilot 看板 ", "alive": "● 调度器运行中", "dead": "● 调度器未运行",
        "updated": "GitHub {s}s 前更新", "done": "完成 {d}/{n}  ",
        "col_status": "状态", "col_round": "轮", "col_deps": "依赖",
    },
}


def tr(key, **kw):
    return TEXT[CFG.language][key].format(**kw)


def sh(cmd, cwd=None):
    try:
        return subprocess.run(cmd, cwd=cwd or CFG.root, text=True, capture_output=True, timeout=30).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def gh_list(args):
    try:
        p = subprocess.run(["gh", *args], cwd=CFG.root, text=True, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError(tr("timeout"))
    if p.returncode != 0:
        raise RuntimeError((p.stderr.strip().splitlines() or [tr("unknown_error")])[-1][:80])
    return json.loads(p.stdout or "[]")


def labels(obj):
    return {l["name"] for l in obj.get("labels", [])}


def round_of(pr):
    return max((int(n.split(":")[1]) for n in labels(pr) if re.fullmatch(r"round:\d+", n)), default=0)


# ---------- GitHub ----------

class GitHub:
    def __init__(self):
        self.issues, self.prs, self.at, self.tried, self.error = [], [], time.time(), 0, ""
        self.loaded = False

    def refresh(self):
        """If a refresh fails (for example the network is down), keep the last data instead of showing nothing."""
        if time.time() - self.tried < GH_REFRESH:
            return
        self.tried = time.time()
        try:
            issues = gh_list(["issue", "list", "--state", "all", "--limit", "200",
                              "--json", "number,title,state,labels,body"])
            prs = gh_list(["pr", "list", "--state", "all", "--limit", "200",
                           "--json", "number,headRefName,state,labels"])
        except RuntimeError as e:
            stale = tr("stale", s=int(time.time() - self.at)) if self.loaded else tr("not_loaded")
            self.error = tr("gh_failed", stale=stale, e=e)
            return
        # only issues issuepilot knows about: they carry an agent:* label or have an agent PR
        branches = {p["headRefName"] for p in prs}
        self.issues = [i for i in issues if any(l.startswith("agent:") for l in labels(i))
                       or f"agent/issue-{i['number']}" in branches]
        self.prs, self.at, self.error, self.loaded = prs, time.time(), "", True

    def pr_for(self, issue_n):
        prs = [p for p in self.prs if p["headRefName"] == f"agent/issue-{issue_n}"]
        return max(prs, key=lambda p: p["number"]) if prs else None

    def status(self, it):
        pr = self.pr_for(it["number"])
        ls = labels(it)
        if it["state"] == "CLOSED":
            return "merged" if pr and pr["state"] == "MERGED" else "closed"
        if pr and pr["state"] == "OPEN":
            pls = labels(pr)
            if "agent:needs-human" in pls:
                return "needs_human"
            for label, name in (("agent:fixing", "fixing"), ("agent:reviewing", "reviewing"),
                                ("agent:approved", "approved")):
                if label in pls:
                    return name
        if "agent:needs-human" in ls:
            return "needs_human"
        if "agent:building" in ls:
            return "building"
        if "agent:ready" in ls:
            return "waiting" if self.open_deps(it) else "ready"
        if "agent:planned" in ls:
            return "planned"
        if "agent:in-pr" in ls:
            return "in_pr"
        return "unknown"

    def open_deps(self, it):
        m = re.search(r"^Depends on:(.*)$", it.get("body") or "", re.M)
        deps = [int(n) for n in re.findall(r"#(\d+)", m.group(1))] if m else []
        state = {i["number"]: i["state"] for i in self.issues}
        return [d for d in deps if state.get(d) != "CLOSED"]


# ---------- running agents ----------

def _inside_repo(path):
    root = str(CFG.root)
    return path == root or path.startswith(root + "/")


def running_agents():
    """Find the `claude -p` and `codex exec` processes (and check runs) started for this repository."""
    found = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            argv = [a.decode(errors="replace") for a in argv if a]
            if not argv:
                continue
            exe = Path(argv[0]).name
            cwd = os.readlink(f"/proc/{pid}/cwd")
            if exe == "claude" and "-p" in argv and "--json-schema" in argv:
                kind = "claude"
            elif "codex" in exe and "exec" in argv and "--output-schema" in argv:
                kind = "codex"
                cwd = argv[argv.index("-C") + 1] if "-C" in argv else cwd
            elif exe in ("sh", "bash", "dash") and argv[1:2] == ["-c"] and argv[2:3] == [CFG.check]:
                kind = "check"
            else:
                continue
            if not _inside_repo(cwd):
                continue
            found.append({"kind": kind, "cwd": cwd, "pid": int(pid),
                          "started": Path(f"/proc/{pid}").stat().st_mtime})
        except (OSError, ValueError, IndexError):
            continue
    return found


def loop_alive():
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").split("\0")
            if any(a.endswith("issuepilot") for a in argv) and "run" in argv \
                    and _inside_repo(os.readlink(f"/proc/{pid}/cwd")):
                return True
        except OSError:
            continue
    return False


# ---------- transcripts ----------

class Transcript:
    """Read a jsonl session transcript incrementally and keep the recent tool calls and the todo list."""

    def __init__(self, path, kind, base):
        self.path, self.kind, self.base = path, kind, base
        self.offset, self.events, self.todos = 0, deque(maxlen=60), []

    def update(self):
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            return
        end = chunk.rfind(b"\n") + 1
        self.offset += end
        for line in chunk[:end].splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            (self._claude if self.kind == "claude" else self._codex)(d)

    def _short(self, s, n=200):
        s = (s or "").replace(self.base + "/", "").strip()
        return s.splitlines()[0][:n] if s else ""

    def _claude(self, d):
        if d.get("type") != "assistant":
            return
        ts = d.get("timestamp")
        for b in (d.get("message") or {}).get("content") or []:
            if b.get("type") == "tool_use":
                name, inp = b.get("name", "?"), b.get("input") or {}
                if name == "TodoWrite":
                    self.todos = inp.get("todos") or []
                    continue
                arg = (inp.get("command") or inp.get("file_path") or inp.get("pattern")
                       or inp.get("description") or json.dumps(inp, ensure_ascii=False))
                self.events.append((ts, name, self._short(str(arg))))
            elif b.get("type") == "text" and b.get("text", "").strip():
                self.events.append((ts, "💬", self._short(b["text"])))

    def _codex(self, d):
        p = d.get("payload") or {}
        ts = d.get("timestamp")
        if d.get("type") != "response_item":
            return
        if p.get("type") in ("custom_tool_call", "function_call"):
            arg = p.get("input") or p.get("arguments") or ""
            try:
                arg = json.loads(arg).get("command", arg) if isinstance(arg, str) and arg.startswith("{") else arg
            except (json.JSONDecodeError, AttributeError):
                pass
            arg = " ".join(arg) if isinstance(arg, list) else str(arg)
            self.events.append((ts, p.get("name", "?"), self._short(arg)))
        elif p.get("type") == "message" and p.get("role") == "assistant":
            text = " ".join(c.get("text", "") for c in p.get("content") or [])
            if text.strip():
                self.events.append((ts, "💬", self._short(text)))


def claude_dir(cwd):
    return CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def find_transcript(agent):
    """The transcript this agent process is writing: the newest file for its cwd modified after it started."""
    since = agent["started"] - 5
    if agent["kind"] == "claude":
        d = claude_dir(agent["cwd"])
        files = [f for f in d.glob("*.jsonl") if f.stat().st_mtime >= since] if d.exists() else []
        # the repository root also has interactive sessions (perhaps your own); keep only headless ones
        files = [f for f in files if headless(f)]
    else:
        day = datetime.now().strftime("%Y/%m/%d")
        files = [f for f in (CODEX_SESSIONS / day).glob("rollout-*.jsonl")
                 if f.stat().st_mtime >= since and codex_cwd(f) == agent["cwd"]]
    return max(files, key=lambda f: f.stat().st_mtime, default=None)


def headless(path):
    with open(path, "rb") as f:
        for line in f:
            if b'"entrypoint"' in line:
                return b'"entrypoint":"cli"' not in line
    return True


def codex_cwd(path):
    with open(path, "rb") as f:
        for line in f:
            if b'"turn_context"' in line or b'"session_meta"' in line:
                cwd = (json.loads(line).get("payload") or {}).get("cwd")
                if cwd:
                    return cwd
    return None


# ---------- worktree ----------

def worktree_info(cwd):
    if not cwd.startswith(str(CFG.worktrees) + "/"):
        return None
    base = f"origin/{CFG.base_branch}"
    commits = sh(["git", "log", "--oneline", f"{base}..HEAD"], cwd=cwd).splitlines()
    fork = sh(["git", "merge-base", base, "HEAD"], cwd=cwd).strip() or base
    stat = sh(["git", "diff", "--shortstat", fork], cwd=cwd).strip()  # against the fork point, so base moving on doesn't count
    dirty = len(sh(["git", "status", "--porcelain"], cwd=cwd).splitlines())
    return commits, stat, dirty


# ---------- rendering ----------

def fmt_age(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def fmt_ts(ts):
    if not ts:
        return "        "
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return ts[11:19]


def last_loop_lines(n):
    """The last n lines of the loop log, without Python tracebacks (only lines that start with a timestamp)."""
    try:
        lines = (CFG.state / "loop.log").read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [l for l in lines if l.startswith("[")][-n:]


def current_role(issue=None):
    """Name of the agent most recently started for this issue. Lane log lines carry a [#N] prefix."""
    for line in reversed(last_loop_lines(300)):
        m = re.search(r"→ (claude|codex) (\S+)", line)
        if m and (issue is None or f"[#{issue}] " in line):
            return m.group(2)
    return ""


def render_issues(gh, active_issues):
    t = Table(expand=True, box=None, padding=(0, 1), header_style="bold")
    t.add_column("", width=1)
    t.add_column("#", justify="right", width=4)
    t.add_column("Issue", ratio=1, no_wrap=True, overflow="ellipsis")
    t.add_column(tr("col_status"), width=11, no_wrap=True)
    t.add_column("PR", width=5, justify="right")
    t.add_column(tr("col_round"), width=3, justify="right")
    t.add_column(tr("col_deps"), width=10, no_wrap=True)
    counts = {}
    for it in sorted(gh.issues, key=lambda i: i["number"]):
        st = gh.status(it)
        counts[st] = counts.get(st, 0) + 1
        pr = gh.pr_for(it["number"])
        deps = gh.open_deps(it) if it["state"] == "OPEN" else []
        t.add_row("▶" if it["number"] in active_issues else "", str(it["number"]), it["title"],
                  Text(tr(st), style=STATUS_STYLE.get(st, "")), f"#{pr['number']}" if pr else "",
                  str(round_of(pr)) if pr and pr["state"] == "OPEN" else "",
                  Text(" ".join(f"#{d}" for d in deps), style="dim"))
    summary = Text(tr("done", d=counts.get("merged", 0), n=len(gh.issues)), style="bold green")
    for st, c in counts.items():
        if st != "merged":
            summary.append(f"{tr(st)} {c}  ", style=STATUS_STYLE.get(st, ""))
    if gh.error:
        summary.append(gh.error, style="red")
    return Panel(Group(summary, t), title="Issues", border_style="blue")


def issue_of(a):
    m = re.search(r"issue-(\d+)", a["cwd"])
    return int(m.group(1)) if m else None


def render_agents(agents, transcripts, height):
    """One panel per issue lane (several when lanes run in parallel), sharing the height."""
    lanes = {}
    for a in agents:
        lanes.setdefault(issue_of(a), []).append(a)
    if not lanes:
        return [Panel(Text(tr("no_agents"), style="dim"), title=tr("agent"), border_style="dim")]
    each = max(height // len(lanes), 8)
    return [render_agent(group, transcripts, each) for _, group in sorted(lanes.items(), key=lambda x: x[0] or 0)]


def render_agent(agents, transcripts, height):
    main = [a for a in agents if a["kind"] in ("claude", "codex")]
    checks = [a for a in agents if a["kind"] == "check"]
    a = (main or checks)[0]
    issue = issue_of(a)
    role = current_role(issue) if main else "check"
    head = Text()
    head.append(f"{role or a['kind']}", style="bold yellow")
    head.append(f"  ({a['kind']})", style="dim")
    head.append(f"  issue #{issue}" if issue else f"  {tr('repo_root')}", style="cyan")
    head.append("  " + tr("running_for", age=fmt_age(time.time() - a["started"])), style="dim")
    if checks and main:
        head.append("  " + tr("running_check"), style="magenta")
    parts = [head]

    wt = worktree_info(a["cwd"])
    if wt:
        commits, stat, dirty = wt
        line = Text(tr("commits", n=len(commits)), style="green")
        line.append(f"  ·  {stat or tr('no_diff')}")
        line.append("  ·  " + tr("dirty", n=dirty), style="yellow" if dirty else "dim")
        parts.append(line)
        for c in commits[:3]:
            parts.append(Text(f"  {c}", style="dim"))

    tscript = transcripts.get(a["pid"])
    if tscript and tscript.todos:
        parts.append(Text(tr("todos"), style="bold"))
        mark = {"completed": ("✔", "green"), "in_progress": ("▶", "bold yellow"), "pending": ("○", "dim")}
        for todo in tscript.todos:
            sym, style = mark.get(todo.get("status"), ("?", ""))
            label = todo.get("activeForm") if todo.get("status") == "in_progress" else todo.get("content")
            parts.append(Text(f"  {sym} {label}", style=style, no_wrap=True, overflow="ellipsis"))
    if tscript:
        parts.append(Text(tr("recent"), style="bold"))
        room = max(height - len(parts) - 2, 3)
        for ts, name, arg in list(tscript.events)[-room:]:
            line = Text(f"  {fmt_ts(ts)} ", style="dim", no_wrap=True, overflow="ellipsis")
            line.append(f"{name:<6} ", style="cyan" if name != "💬" else "white")
            line.append(arg)
            parts.append(line)
    elif main:
        parts.append(Text(tr("no_transcript"), style="dim"))
    return Panel(Group(*parts), title=f"{tr('agent')} · issue #{issue}" if issue else tr("agent"),
                 border_style="yellow", height=height)


def render_log(n):
    body = Text()
    for l in last_loop_lines(n):
        style = "red" if re.search(r"✗|Traceback", l) else "green" if "✓" in l else "yellow" if "↻" in l else ""
        body.append(l[:300] + "\n", style=style)
    return Panel(body or Text(tr("no_log"), style="dim"), title=tr("log"), border_style="dim")


def render(gh, agents, transcripts, size):
    _, height = size
    alive = loop_alive()
    head = Text()
    head.append(tr("title", repo=CFG.root.name), style="bold reverse")
    head.append(f"  {datetime.now():%H:%M:%S}  ")
    head.append(tr("alive") if alive else tr("dead"), style="green" if alive else "bold red")
    head.append("  " + tr("updated", s=int(time.time() - gh.at)), style="red" if gh.error else "dim")
    active = {issue_of(a) for a in agents} - {None}
    issues = render_issues(gh, active)
    log_rows = 8
    agent_rows = max(height - (len(gh.issues) + 4) - log_rows - 4, 8)
    return Group(head, issues, *render_agents(agents, transcripts, agent_rows), render_log(log_rows - 2))


def main(cfg):
    global CFG
    CFG = cfg
    gh = GitHub()
    transcripts = {}
    console = Console()
    try:
        with Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                gh.refresh()
                agents = running_agents()
                for a in agents:
                    if a["kind"] == "check":
                        continue
                    tscript = transcripts.get(a["pid"])
                    if tscript is None:
                        path = find_transcript(a)
                        if path:
                            transcripts[a["pid"]] = tscript = Transcript(path, a["kind"], a["cwd"])
                    if tscript:
                        tscript.update()
                for pid in list(transcripts):
                    if pid not in {a["pid"] for a in agents}:
                        del transcripts[pid]
                live.update(render(gh, agents, transcripts, console.size), refresh=True)
                time.sleep(LOCAL_REFRESH)
    except KeyboardInterrupt:
        pass
