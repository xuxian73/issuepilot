"""The orchestrator's state machine.

The planner splits the spec into issues and triages new problems; the builder implements and fixes;
the reviewer reviews read-only. All state lives in GitHub labels. This module makes only deterministic
state changes and never asks an LLM how to proceed. Agents never touch GitHub: this module does every
push, PR, comment and label change.

Call `configure(cfg)` before anything else.
"""

import base64
import json
import os
import re
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from string import Template

from . import agents, i18n
from .errors import StepError
from .i18n import t

CFG = None
ROOT = RUNS = WORKTREES = None
POOL = None

LABELS = {  # name -> color; descriptions come from i18n
    "agent:planned": "c5def5",
    "agent:ready": "0e8a16",
    "agent:building": "fbca04",
    "agent:in-pr": "1d76db",
    "agent:reviewing": "5319e7",
    "agent:fixing": "d93f0b",
    "agent:approved": "0e8a16",
    "agent:needs-human": "b60205",
    "agent:retried": "ededed",
    "agent:replanned": "ededed",
    "agent:triaged": "ededed",
}
BLOCKING = ("blocker", "major")
MARKER = "<!-- agent:"
NET_TIMEOUT = 120  # seconds for git network operations; a timeout counts as a transient error
LABEL_SETTLE = 20  # seconds; see the comment above start_lane
PROMPTS = Path(__file__).parent / "prompts"

# Without keepalives a stalled SSH connection hangs forever. With them it fails within about a minute
# and `retrying` takes over.
os.environ.setdefault("GIT_SSH_COMMAND", "ssh -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4")


def configure(cfg):
    global CFG, ROOT, RUNS, WORKTREES, POOL
    CFG, ROOT, RUNS, WORKTREES = cfg, cfg.root, cfg.runs, cfg.worktrees
    i18n.LANG = cfg.language
    POOL = ThreadPoolExecutor(max_workers=cfg.max_in_flight + 2)
    LANES.clear()
    FINISHED.clear()


# ---------- basics ----------

_lane = threading.local()  # which issue the current thread works on; shown in log lines


_log_lock = threading.Lock()


def log(msg):
    """Print a line and append it to <state_dir>/loop.log, which the dashboard reads."""
    line = f"[{datetime.now():%H:%M:%S}] {getattr(_lane, 'tag', '')}{msg}"
    print(line, flush=True)
    if CFG is None:
        return
    with _log_lock:
        try:
            CFG.state.mkdir(parents=True, exist_ok=True)
            with open(CFG.state / "loop.log", "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


def run_log_path(name, ext="log"):
    RUNS.mkdir(parents=True, exist_ok=True)
    return RUNS / f"{datetime.now():%Y%m%d-%H%M%S}-{name}.{ext}"


def sh(cmd, cwd=None, input=None, timeout=600, check=True):
    try:
        p = subprocess.run(cmd, cwd=cwd or ROOT, input=input, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise StepError(f"command timed out after {timeout}s: {' '.join(map(str, cmd))}")
    if check and p.returncode != 0:
        raise StepError(f"command failed ({p.returncode}): {' '.join(map(str, cmd))}\n{p.stderr[-3000:]}")
    return p.stdout


TRANSIENT = re.compile(r"EOF|timeout|timed out|connection reset|connection closed|502|503|504|TLS handshake|"
                       r"i/o timeout|cannot lock ref|unable to access|Could not read from remote", re.I)


def retrying(fn, what):
    """Retry transient errors (network drops, ref lock races) up to 4 times: after 5, 15, 45 and 135 s."""
    for attempt in range(5):
        try:
            return fn()
        except StepError as e:
            if attempt == 4 or not TRANSIENT.search(str(e)):
                raise
            log(t("log.retry", what=what, wait=5 * 3 ** attempt, err=str(e).splitlines()[-1][:120]))
            time.sleep(5 * 3 ** attempt)


def gh(*args, input=None):
    return retrying(lambda: sh(["gh", *args], input=input), "GitHub")


def gh_json(*args):
    return json.loads(gh(*args) or "null")


ROOT_LOCK = threading.Lock()  # git commands in the main checkout (fetch, worktree add, pull…) run one at a time


def git(wt, *args, check=True, timeout=600):
    if Path(wt) == ROOT:
        with ROOT_LOCK:
            return sh(["git", *args], cwd=wt, check=check, timeout=timeout).strip()
    return sh(["git", *args], cwd=wt, check=check, timeout=timeout).strip()


def git_common_dir():
    return (ROOT / git(ROOT, "rev-parse", "--git-common-dir")).resolve()


def ensure_state_excluded():
    """Keep the state directory (logs, worktrees) out of `git status` without editing .gitignore."""
    exclude = git_common_dir() / "info" / "exclude"
    line = f"/{CFG.state_dir.strip('/')}/"
    current = exclude.read_text() if exclude.exists() else ""
    if line not in current.splitlines():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(current + ("" if current.endswith("\n") or not current else "\n") + line + "\n")


def render(name, **values):
    custom = Path(CFG.root, CFG.prompts_dir, f"{name}.md") if CFG.prompts_dir else None
    path = custom if custom and custom.exists() else PROMPTS / CFG.language / f"{name}.md"
    note = CFG.notes.get(name, "").strip()
    base = {"spec": CFG.spec, "conventions": CFG.conventions, "check": CFG.check, "base": CFG.base_branch,
            "notes": f"\n## {t('prompt.notes')}\n\n{note}\n" if note else "",
            # Codex's workspace-write sandbox keeps .git read-only, so a Codex builder cannot commit
            "commit_rule": t("prompt.commit_none" if CFG.builder.cli == "codex" else "prompt.commit_git",
                             conventions=CFG.conventions)}
    base.update(values)
    return Template(path.read_text()).safe_substitute(**{k: str(v) for k, v in base.items()})


def label_names(obj):
    return {l["name"] for l in obj.get("labels", [])}


def ensure_label(name, color="ededed", desc=""):
    gh("label", "create", name, "--color", color, "--description", desc, "--force")


def run_agent(role, prompt, schema, cwd, name, writable=False):
    agent = getattr(CFG, role)
    log(f"  → {agent.cli} {name}")
    return agents.run(agent, prompt, schema, cwd, name, writable, CFG.agent_timeout, run_log_path)


# ---------- worktrees and checks ----------

def branch_for(issue):
    return f"agent/issue-{issue}"


def issue_of_branch(branch):
    m = re.fullmatch(r"agent/issue-(\d+)", branch)
    return int(m.group(1)) if m else None


def worktree(issue, fresh):
    """fresh=True: new branch from origin/<base>. Otherwise sync to the latest remote branch."""
    wt, br = WORKTREES / f"issue-{issue}", branch_for(issue)
    retrying(lambda: git(ROOT, "fetch", "origin", "--prune", timeout=NET_TIMEOUT), "git fetch")
    if not wt.exists():
        WORKTREES.mkdir(parents=True, exist_ok=True)
        start = f"origin/{CFG.base_branch}" if fresh else f"origin/{br}"
        git(ROOT, "worktree", "add", "-B", br, str(wt), start)
    elif not fresh:
        git(wt, "reset", "--hard", f"origin/{br}")
    return wt


def commit_leftovers(wt, issue):
    """Commit what the builder left uncommitted. A Codex builder cannot commit, so this is its only commit."""
    if git(wt, "status", "--porcelain"):
        git(wt, "add", "-A")
        msg = (f"{issue['title']} (#{issue['number']})" if CFG.builder.cli == "codex"
               else f"chore: commit leftover builder changes (#{issue['number']})")
        git(wt, "commit", "-m", msg)


def run_checks(wt, name):
    try:
        p = subprocess.run(CFG.check, shell=True, cwd=wt, text=True, capture_output=True, timeout=CFG.check_timeout)
        ok, out = p.returncode == 0, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        ok, out = False, partial + f"\n[issuepilot] check timed out after {CFG.check_timeout}s"
    run_log_path(f"check-{name}").write_text(out)
    return ok, out[-6000:]


def build_until_green(wt, issue, prompt, name):
    """Run the builder, then the checks. On failure feed the output back, up to max_check_retries times."""
    result = run_agent("builder", prompt, "build", wt, name, writable=True)
    proposals = list(result["proposed_issues"])
    for attempt in range(CFG.max_check_retries + 1):
        commit_leftovers(wt, issue)
        ok, out = run_checks(wt, name)
        if ok:
            result["proposed_issues"] = proposals
            return result
        if attempt == CFG.max_check_retries:
            raise StepError(t("err.check_failed", n=CFG.max_check_retries, check=CFG.check, out=out[-2500:]))
        log(t("log.check_retry", n=attempt + 1))
        retry = run_agent("builder", render("checkfail", number=issue["number"], branch=branch_for(issue["number"]),
                                            title=issue["title"], body=issue["body"], log=out),
                          "build", wt, f"{name}-checkfix{attempt + 1}", writable=True)
        proposals += retry["proposed_issues"]


# ---------- GitHub data ----------

def get_issue(number):
    return gh_json("issue", "view", str(number), "--json", "number,title,body,state,labels")


def depends_on(body):
    m = re.search(r"^Depends on:(.*)$", body or "", re.M)
    return [int(n) for n in re.findall(r"#(\d+)", m.group(1))] if m else []


def agent_prs(label):
    return gh_json("pr", "list", "--state", "open", "--label", label, "--limit", "100",
                   "--json", "number,title,body,headRefName,labels,mergeable")


def pr_comments(number):
    data = gh_json("pr", "view", str(number), "--json", "comments,reviews")
    return [c["body"] for c in data["comments"]] + [r["body"] for r in data["reviews"] if r["body"]]


def encode(obj):
    return base64.b64encode(json.dumps(obj, ensure_ascii=False).encode()).decode()


def decode(b64):
    return json.loads(base64.b64decode(b64).decode())


def agent_payloads(comments, kind):
    out = []
    for body in comments:
        m = re.match(rf"<!-- agent:{kind} round=(\d+) data=(\S+) -->", body)
        if m:
            out.append((int(m.group(1)), decode(m.group(2))))
    return out


def human_comments(comments):
    hs = [c for c in comments if not c.startswith(MARKER)]
    return "\n\n---\n\n".join(hs) if hs else t("none")


def fix_round(pr):
    rounds = [int(n.split(":")[1]) for n in label_names(pr) if re.fullmatch(r"round:\d+", n)]
    return max(rounds, default=0)


def relabel(kind, number, add=(), remove=()):
    args = [kind, "edit", str(number)]
    for l in add:
        args += ["--add-label", l]
    for l in remove:
        args += ["--remove-label", l]
    gh(*args)


def comment(kind, number, body):
    gh(kind, "comment", str(number), "--body-file", "-", input=body)


def needs_human(kind, number, err, remove=(), retry=None):
    """On error, retry once automatically (back to the `retry` label). On a second error, hand it to a human."""
    if retry and "agent:retried" not in label_names(gh_json(kind, "view", str(number), "--json", "labels")):
        log(t("log.auto_retry", kind=kind, n=number, err=str(err)[:300]))
        relabel(kind, number, add=[retry, "agent:retried"], remove=remove)
        comment(kind, number, f"{MARKER}error -->\n### 🤖 {t('c.retry')}\n\n{err}")
        return
    log(t("log.needs_human", kind=kind, n=number, err=str(err)[:300]))
    relabel(kind, number, add=["agent:needs-human"], remove=remove)
    comment(kind, number, f"{MARKER}error -->\n### 🤖 {t('c.needs_human')}\n\n{err}")


# ---------- formatting ----------

def fmt_findings(findings):
    if not findings:
        return t("none")
    lines = []
    for i, f in enumerate(findings, 1):
        loc = f["file"] + (f":{f['line']}" if f.get("line") else "")
        lines.append(t("c.finding", i=i, severity=f["severity"], loc=loc, problem=f["problem"],
                       suggestion=f["suggestion"]))
    return "\n".join(lines)


def fmt_previous(reviews):
    if not reviews:
        return t("c.first_round")
    return "\n\n".join(f"### {t('c.round', r=r)}\n{d['summary']}\n\n{fmt_findings(d['findings'])}" for r, d in reviews)


# ---------- steps ----------

TRIAGE_LOCK = threading.Lock()


def triage(proposals, kind, number):
    """Let the planner triage problems that agents proposed."""
    if not proposals:
        return
    log(t("log.triage", n=len(proposals)))
    try:
        with TRIAGE_LOCK:  # two lanes triaging at once could create duplicate issues
            _triage(proposals, kind, number)
    except (StepError, subprocess.TimeoutExpired) as e:
        # a failed triage does not stop the main flow; leave a comment so a human sees it
        log(t("log.triage_failed", err=str(e)[:300]))
        comment(kind, number, f"{MARKER}error -->\n### 🤖 {t('c.triage_failed')}\n\n{e}\n\n{t('c.proposals')}\n"
                + "\n".join(f"- **{p['title']}**: {p['description']}" for p in proposals))


def _triage(proposals, kind, number):
    open_issues = gh_json("issue", "list", "--state", "open", "--limit", "200", "--json", "number,title")
    text = "\n".join(f"{i}. **{p['title']}**: {p['description']}" for i, p in enumerate(proposals, 1))
    existing = "\n".join(f"- #{x['number']} {x['title']}" for x in open_issues) or t("none")
    source = f"{'PR' if kind == 'pr' else 'issue'} #{number}"
    decisions = run_agent("planner", render("triage", source=source, proposals=text, open_issues=existing,
                                            create_note=t("prompt.create_auto" if CFG.auto_ready else "prompt.create_manual")),
                          "triage", ROOT, f"planner-triage-{kind}{number}")["decisions"]
    report = []
    for d in decisions:
        title = proposals[d["index"] - 1]["title"] if 0 < d["index"] <= len(proposals) else "?"
        if d["action"] == "create":
            triaged = len(gh_json("issue", "list", "--state", "all", "--label", "agent:triaged", "--json", "number"))
            auto = CFG.auto_ready and triaged < CFG.max_triaged_issues
            labels = ["agent:ready", "agent:triaged"] if auto else ["agent:planned"]
            url = gh("issue", "create", "--title", d["title"], *sum((["--label", l] for l in labels), []),
                     "--body-file", "-", input=f"{d['body']}\n\n---\n{t('c.found_in', source=source)}").strip()
            report.append(t("c.triage_create", url=url, title=d["title"], reason=d["reason"]))
        elif d["action"] == "duplicate":
            report.append(t("c.triage_dup", title=title, n=d["duplicate_of"], reason=d["reason"]))
        else:
            report.append(t("c.triage_drop", title=title, reason=d["reason"]))
    comment(kind, number, f"{MARKER}triage -->\n### 🤖 {t('c.triage')}\n\n" + "\n".join(report))


def next_ready_issue(skip=()):
    for it in sorted(gh_json("issue", "list", "--state", "open", "--label", "agent:ready", "--limit", "100",
                             "--json", "number,title,body,labels"), key=lambda x: x["number"]):
        if it["number"] in skip:
            continue
        blockers = [n for n in depends_on(it["body"]) if get_issue(n)["state"] != "CLOSED"]
        if blockers:
            log(t("log.waiting_deps", n=it["number"], deps=blockers))
            continue
        return it
    return None


def step_build(issue):
    n, br = issue["number"], branch_for(issue["number"])
    log(t("log.build", n=n, title=issue["title"]))
    relabel("issue", n, add=["agent:building"], remove=["agent:ready"])
    try:
        wt = worktree(n, fresh=True)
        result = build_until_green(wt, issue, render("build", number=n, branch=br, title=issue["title"],
                                                     body=issue["body"]), f"builder-{n}")
        if git(wt, "rev-list", "--count", f"origin/{CFG.base_branch}..HEAD") == "0":
            raise StepError(t("err.no_commits"))
        retrying(lambda: git(wt, "push", "-u", "origin", br, timeout=NET_TIMEOUT), "git push")
        ensure_label("round:0", "ededed", t("label.round"))
        url = gh("pr", "create", "--base", CFG.base_branch, "--head", br, "--title", f"{issue['title']} (#{n})",
                 "--label", "agent:reviewing", "--label", "round:0", "--body-file", "-",
                 input=f"Closes #{n}\n\n## 🤖 {t('c.builder_notes', cli=CFG.builder.cli)}\n\n{result['summary']}").strip()
        relabel("issue", n, add=["agent:in-pr"], remove=["agent:building"])
        log(t("log.pr_opened", url=url))
        pr_number = int(url.rstrip("/").rsplit("/", 1)[1])
        triage(result["proposed_issues"], "pr", pr_number)
    except (StepError, subprocess.TimeoutExpired) as e:
        needs_human("issue", n, e, remove=["agent:building"], retry="agent:ready")


def step_review(pr):
    """Review one PR. While CI is still running, do nothing and return False so the loop sleeps normally."""
    n, issue_n = pr["number"], issue_of_branch(pr["headRefName"])
    rounds_done = fix_round(pr)
    review_round = rounds_done + 1
    try:
        if pr.get("mergeable") == "CONFLICTING":
            log(t("log.conflict", n=n, base=CFG.base_branch))
            relabel("pr", n, add=["agent:fixing"], remove=["agent:reviewing"])
            return True
        wt = worktree(issue_n, fresh=False)
        state, failed = ci_state(n, wt)
        if state == "pending":
            age = time.time() - int(git(wt, "log", "-1", "--format=%ct"))
            if age > CFG.ci_timeout:
                needs_human("pr", n, t("err.ci_timeout", m=CFG.ci_timeout // 60), remove=["agent:reviewing"])
                return True
            log(t("log.ci_wait", n=n))
            return False
        if state == "fail":
            log(t("log.ci_failed", n=n, names=", ".join(c["name"] for c in failed)))
            ci = {"summary": t("c.ci_summary"),
                  "findings": [{"severity": "blocker", "file": ".github/workflows", "line": None,
                                "problem": t("c.ci_problem", name=c["name"], log=ci_log(c)),
                                "suggestion": t("c.ci_suggestion")}
                               for c in failed],
                  "out_of_scope": []}
            comment("pr", n, f"<!-- agent:review round={review_round} data={encode(ci)} -->\n"
                             f"### 🤖 {t('c.ci_title', r=review_round)}\n\n{fmt_findings(ci['findings'])}")
            conclude(pr, rounds_done, len(failed))
            return True
        log(t("log.review", n=n, r=review_round))
        issue = get_issue(issue_n)
        comments = pr_comments(n)
        builder_notes = [d["summary"] for _, d in agent_payloads(comments, "builder")]
        review = run_agent("reviewer", render(
            "review", pr=n, number=issue_n, title=issue["title"], body=issue["body"],
            builder_summary=builder_notes[-1] if builder_notes else pr["body"],
            previous=fmt_previous(agent_payloads(comments, "review")),
            triage="\n\n".join(c.split("\n", 1)[1] for c in comments if c.startswith(f"{MARKER}triage")) or t("none"),
            human=human_comments(comments)), "review", wt, f"reviewer-pr{n}-r{review_round}")
        blocking = [f for f in review["findings"] if f["severity"] in BLOCKING]
        verdict = t("c.pass") if not blocking else t("c.changes", n=len(blocking))
        comment("pr", n, f"<!-- agent:review round={review_round} data={encode(review)} -->\n"
                         f"### 🤖 {t('c.review_title', cli=CFG.reviewer.cli, r=review_round, verdict=verdict)}\n\n"
                         f"{review['summary']}\n\n{fmt_findings(review['findings'])}")
        triage(review["out_of_scope"], "pr", n)
        conclude(pr, rounds_done, len(blocking))
    except (StepError, subprocess.TimeoutExpired) as e:
        needs_human("pr", n, e, remove=["agent:reviewing"], retry="agent:reviewing")
    return True


def conclude(pr, rounds_done, blocking):
    """After a review (or CI) round: merge if clean, send back for fixes, or arbitrate after too many rounds."""
    n = pr["number"]
    if not blocking:
        relabel("pr", n, add=["agent:approved"], remove=["agent:reviewing"])
        log(t("log.approved"))
        merge(pr)
    elif rounds_done >= CFG.max_fix_rounds:
        arbitrate(pr, rounds_done)
    else:
        relabel("pr", n, add=["agent:fixing"], remove=["agent:reviewing"])
        log(t("log.sent_back", n=blocking))


def merge(pr):
    if not CFG.auto_merge:
        log(t("log.wait_merge"))
        return
    gh("pr", "merge", str(pr["number"]), "--squash")
    git(ROOT, "push", "origin", "--delete", pr["headRefName"], check=False, timeout=NET_TIMEOUT)
    log(t("log.merged", n=pr["number"]))


def arbitrate(pr, rounds_done):
    """After max_fix_rounds: the planner reads the whole history and decides to merge or to rewrite the issue
    and start over (only once per issue)."""
    n, issue_n = pr["number"], issue_of_branch(pr["headRefName"])
    log(t("log.arbitrate", r=rounds_done))
    issue = get_issue(issue_n)
    if "agent:replanned" in label_names(issue):
        needs_human("pr", n, t("err.replanned_twice", r=rounds_done), remove=["agent:reviewing"])
        return
    comments = pr_comments(n)
    history = "\n\n".join(
        [f"### {t('c.reviewer_round', r=r)}\n{d['summary']}\n\n{fmt_findings(d['findings'])}"
         for r, d in agent_payloads(comments, "review")]
        + [f"### {t('c.builder_round', r=r)}\n{d['summary']}" for r, d in agent_payloads(comments, "builder")])
    wt = worktree(issue_n, fresh=False)
    d = run_agent("planner", render("arbitrate", pr=n, number=issue_n, title=issue["title"], body=issue["body"],
                                    rounds=rounds_done, history=history, human=human_comments(comments)),
                  "arbitrate", wt, f"planner-arbitrate-pr{n}")
    decision = t("c.arb_merge") if d["decision"] == "merge" else t("c.arb_replan")
    comment("pr", n, f"{MARKER}arbitrate -->\n### 🤖 {t('c.arbitrate', decision=decision)}\n\n{d['reason']}")
    if d["decision"] == "merge":
        relabel("pr", n, add=["agent:approved"], remove=["agent:reviewing"])
        merge(pr)
        return
    gh("pr", "close", str(n))
    git(ROOT, "push", "origin", "--delete", branch_for(issue_n), check=False, timeout=NET_TIMEOUT)
    git(ROOT, "worktree", "remove", "--force", str(WORKTREES / f"issue-{issue_n}"), check=False)
    git(ROOT, "branch", "-D", branch_for(issue_n), check=False)
    deps = re.search(r"^Depends on:.*$", issue["body"], re.M)
    gh("issue", "edit", str(issue_n), "--body-file", "-",
       input=d["new_body"] + (f"\n\n---\n{deps.group(0)}" if deps and "Depends on:" not in d["new_body"] else ""))
    relabel("issue", issue_n, add=["agent:ready", "agent:replanned"], remove=["agent:in-pr"])
    log(t("log.replanned", n=issue_n))


def ci_state(n, wt):
    """CI state of the PR's latest commit: pass / pending / fail / none, plus the failed checks."""
    p = subprocess.run(["gh", "pr", "checks", str(n), "--json", "name,bucket,link"],
                       cwd=ROOT, text=True, capture_output=True, timeout=120)
    checks = json.loads(p.stdout) if p.stdout.strip() else []
    if not checks:
        # right after a push the checks may not be registered yet; if the repo has workflows, assume they run
        return ("pending" if any((wt / ".github" / "workflows").glob("*.y*ml")) else "none"), []
    failed = [c for c in checks if c["bucket"] in ("fail", "cancel")]
    if failed:
        return "fail", failed
    if any(c["bucket"] == "pending" for c in checks):
        return "pending", []
    return "pass", []


CI_LOG_CHARS = 5000


def ci_log(check):
    m = re.search(r"/runs/(\d+)", check.get("link") or "")
    if not m:
        return t("c.no_ci_log", link=check.get("link"))
    p = subprocess.run(["gh", "run", "view", m.group(1), "--log-failed"],
                       cwd=ROOT, text=True, capture_output=True, timeout=300)
    return (p.stdout or p.stderr)[-CI_LOG_CHARS:]


def step_fix(pr):
    n, issue_n = pr["number"], issue_of_branch(pr["headRefName"])
    rounds_done = fix_round(pr)
    log(t("log.fix", n=n, r=rounds_done + 1))
    try:
        issue = get_issue(issue_n)
        wt = worktree(issue_n, fresh=False)
        comments = pr_comments(n)
        reviews = agent_payloads(comments, "review")
        last_round, last = reviews[-1] if reviews else (0, {"findings": []})
        conflict = pr.get("mergeable") == "CONFLICTING"
        before = git(wt, "rev-parse", "HEAD")
        prompt = render("fix", pr=n, number=issue_n, branch=branch_for(issue_n), round=last_round,
                        title=issue["title"], body=issue["body"], findings=fmt_findings(last["findings"]),
                        human=human_comments(comments),
                        conflict=t("prompt.conflict", base=CFG.base_branch) if conflict else "")
        result = build_until_green(wt, issue, prompt, f"builder-pr{n}-fix{rounds_done + 1}")
        if git(wt, "rev-parse", "HEAD") != before:
            retrying(lambda: git(wt, "push", "origin", branch_for(issue_n), timeout=NET_TIMEOUT), "git push")
        new_round = rounds_done + 1
        ensure_label(f"round:{new_round}", "ededed", t("label.round"))
        comment("pr", n, f"<!-- agent:builder round={new_round} data={encode(result)} -->\n"
                         f"### 🤖 {t('c.fix_title', cli=CFG.builder.cli, r=new_round)}\n\n{result['summary']}")
        relabel("pr", n, add=["agent:reviewing", f"round:{new_round}"],
                remove=["agent:fixing", f"round:{rounds_done}"])
        triage(result["proposed_issues"], "pr", n)
    except (StepError, subprocess.TimeoutExpired) as e:
        needs_human("pr", n, e, remove=["agent:fixing"], retry="agent:fixing")


def cleanup():
    """Remove the worktree of an issue once it is closed (its PR was merged)."""
    if not WORKTREES.exists():
        return
    for wt in WORKTREES.glob("issue-*"):
        n = int(wt.name.split("-")[1])
        if n not in LANES and get_issue(n)["state"] == "CLOSED":
            log(t("log.cleanup", n=n))
            git(ROOT, "worktree", "remove", "--force", str(wt), check=False)
            git(ROOT, "branch", "-D", branch_for(n), check=False)


# ---------- parallel lanes ----------
# Each issue gets one "lane": building, reviewing and fixing run in that lane's thread, and issues do not wait
# for each other. An issue has at most one lane at a time. After a lane ends, the issue rests for LABEL_SETTLE
# seconds, because GitHub's label-filtered lists briefly return stale results and an immediate reschedule could
# repeat the step that just finished.

LANES = {}     # issue number -> Future
FINISHED = {}  # issue number -> time its last lane ended
_last_pull = 0.0


def start_lane(n, fn):
    def run():
        _lane.tag = f"[#{n}] "
        try:
            return fn()
        except Exception as e:  # one lane's error does not affect the others; the next tick retries
            log(t("log.lane_error", err=e))
            traceback.print_exc()
    LANES[n] = POOL.submit(run)


def reap():
    for n, f in list(LANES.items()):
        if f.done():
            del LANES[n]
            FINISHED[n] = time.time()


def busy(n):
    return n in LANES or time.time() - FINISHED.get(n, 0) < LABEL_SETTLE


def still_labeled(kind, n, label):
    """Label-filtered lists can be stale, so read the issue/PR directly before starting work."""
    obj = gh_json(kind, "view", str(n), "--json", "labels,state")
    return obj["state"] == "OPEN" and label in label_names(obj)


def tick():
    """One scheduling round: start a lane for each PR that needs a fix or a review, and for ready issues.
    Returns whether it started anything."""
    global _last_pull
    reap()
    if time.time() - _last_pull > CFG.poll_seconds:  # so the planner sees the latest base branch
        retrying(lambda: git(ROOT, "pull", "--ff-only", "-q", "origin", CFG.base_branch, check=False,
                             timeout=NET_TIMEOUT), "git pull")
        _last_pull = time.time()
    cleanup()
    did = False
    for label, step in (("agent:fixing", step_fix), ("agent:reviewing", step_review)):
        for pr in agent_prs(label):
            n = issue_of_branch(pr["headRefName"])
            if n is None or busy(n):
                continue
            start_lane(n, lambda pr=pr, label=label, step=step:
                       still_labeled("pr", pr["number"], label) and step(pr))
            did = True
    # Do not count with --label: right after a label change, a label-filtered list can miss the PR and the
    # lane count would exceed max_in_flight. List every open agent PR instead and skip only those waiting
    # for a human (approved and waiting for a merge, or needs-human).
    open_prs = gh_json("pr", "list", "--state", "open", "--limit", "100", "--json", "headRefName,labels")
    waiting = {"agent:approved", "agent:needs-human"}
    active = {issue_of_branch(p["headRefName"]) for p in open_prs
              if issue_of_branch(p["headRefName"]) and not waiting & label_names(p)}
    # count lanes that just ended too: a new PR may not be listed yet. Over-counting only delays by LABEL_SETTLE
    active |= set(LANES) | {n for n in FINISHED if busy(n)}
    while len(active) < CFG.max_in_flight:
        issue = next_ready_issue(skip=active)
        if not issue:
            break
        n = issue["number"]
        start_lane(n, lambda issue=issue: still_labeled("issue", issue["number"], "agent:ready") and step_build(issue))
        active.add(n)
        did = True
    return did


# ---------- commands ----------

def cmd_setup():
    for name, color in LABELS.items():
        ensure_label(name, color, t(f"label.{name}"))
    log(t("log.labels", n=len(LABELS)))


def cmd_plan(spec):
    spec_text = (ROOT / spec).read_text()
    existing = gh_json("issue", "list", "--state", "all", "--limit", "200", "--json", "number,title")
    plan = run_agent("planner", render("plan", spec=spec, spec_text=spec_text,
                                       existing="\n".join(f"- #{x['number']} {x['title']}" for x in existing) or t("none")),
                     "plan", ROOT, "planner-plan")
    numbers = {}
    for it in plan["issues"]:
        deps = [f"#{numbers[k]}" for k in it["depends_on"] if k in numbers]
        body = it["body"] + (f"\n\n---\nDepends on: {', '.join(deps)}" if deps else "")
        url = gh("issue", "create", "--title", it["title"], "--label", "agent:planned",
                 "--body-file", "-", input=body).strip()
        numbers[it["key"]] = int(url.rstrip("/").rsplit("/", 1)[1])
        log(t("log.created", url=url, title=it["title"]) + (t("log.deps", deps=", ".join(deps)) if deps else ""))
    log(t("log.plan_done"))
    return list(numbers.values())


def cmd_approve(issues):
    for n in issues:
        relabel("issue", n, add=["agent:ready"], remove=["agent:planned"])
        log(f"#{n} → agent:ready")


def code_mtime():
    files = list(Path(__file__).parent.rglob("*.py")) + list(PROMPTS.rglob("*.md"))
    if CFG.config_path.exists():
        files.append(CFG.config_path)
    return max(f.stat().st_mtime for f in files)


def cmd_run(once=False, reload=None):
    """`reload` is called between rounds when issuepilot's code or the config file changed."""
    ensure_state_excluded()
    # an issue left in agent:building by an interrupted run goes back to ready; its worktree commits are kept
    for it in gh_json("issue", "list", "--state", "open", "--label", "agent:building", "--json", "number"):
        if not still_labeled("issue", it["number"], "agent:building"):  # label-filtered lists can be stale
            continue
        log(t("log.resume", n=it["number"]))
        relabel("issue", it["number"], add=["agent:ready"], remove=["agent:building"])
    started = code_mtime()
    while True:
        reap()
        if not LANES and reload and code_mtime() > started:  # never restart while lanes are running
            log(t("log.reload"))
            reload()
        try:
            did = tick()
        except (StepError, subprocess.TimeoutExpired) as e:
            log(t("log.lane_error", err=e))
            traceback.print_exc()
            did = False
        if once:
            POOL.shutdown(wait=True)
            return
        # with lanes running, check back often so the next step starts soon after a lane ends
        time.sleep(LABEL_SETTLE if LANES or did else CFG.poll_seconds)


def cmd_status():
    for label in LABELS:
        issues = gh_json("issue", "list", "--state", "open", "--label", label, "--json", "number,title")
        prs = gh_json("pr", "list", "--state", "open", "--label", label, "--json", "number,title")
        items = [f"#{x['number']} {x['title']}" for x in issues] + [f"PR #{x['number']} {x['title']}" for x in prs]
        if items:
            print(f"{label}:\n  " + "\n  ".join(items))
