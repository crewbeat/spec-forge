"""Where tasks come from.

A source turns something outside the factory into a task
({source, ref, title, body, url}) and may be told how its job went.
The pipeline never looks at where a job came from.

To add a source: write `pull_<kind>(options, force)` returning the jobs it
created and, if the source should hear back, `report_<kind>(job, event, text)`.
Register both below and enable it in config.json `sources`.
"""

import json
import re
import time

from . import core
from .core import JOBS, REPO, Refused, config

problems = []  # what went wrong while pulling, for the command to show


def split_markdown(text, fallback_title):
    """Title from the first `# heading`, the rest is the body."""
    lines = text.strip().splitlines()
    for i, line in enumerate(lines):
        match = re.match(r"^#\s+(.+)$", line)
        if match:
            return match.group(1).strip(), "\n".join(lines[:i] + lines[i + 1:]).strip()
        if line.strip():
            break
    return fallback_title, text.strip()


def pull_local(options, force=False):
    """Every .md file dropped in the inbox folder becomes a job."""
    inbox = REPO / options.get("path", "forge/inbox")
    if not inbox.is_dir():
        return []
    created = []
    for path in sorted(inbox.glob("*.md")):
        title, body = split_markdown(path.read_text(errors="replace"), path.stem.replace("-", " ").replace("_", " "))
        job = core.create_job({"source": "local", "ref": path.name, "title": title, "body": body})
        imported = inbox / "imported"
        imported.mkdir(exist_ok=True)
        path.rename(imported / f"{job['id']}.md")
        created.append(job)
    return created


def pending_local(options):
    inbox = REPO / options.get("path", "forge/inbox")
    return inbox.is_dir() and any(inbox.glob("*.md"))


# ---- GitHub Issues ---------------------------------------------------------
#
# Open issues carrying a label become jobs. On a public repo anyone can open an
# issue but only people with triage rights can label one, so the label is what
# lets a task in. Calls go through `gh`, which the person has already signed in.


def github_state(update=None):
    path = JOBS / ".github.json"
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    if update is not None:
        state.update(update)
        JOBS.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    return state


def github_due(options):
    return time.time() - github_state().get("checked", 0) >= options.get("poll_seconds", 120)


def github_args(options):
    return ["--repo", options["repo"]] if options.get("repo") else []


def github_unseen(options):
    """Labelled open issues that aren't jobs yet, oldest first."""
    out = core.run(["gh", "issue", "list", "--state", "open", "--label", options.get("label", "forge"),
                    "--limit", str(options.get("limit", 30)), "--json", "number,title,body,url",
                    *github_args(options)]).stdout
    seen = {j["source"]["ref"] for j in core.all_jobs() if j["source"]["kind"] == "github"}
    return sorted((i for i in json.loads(out or "[]") if f"#{i['number']}" not in seen), key=lambda i: i["number"])


def pull_github(options, force=False):
    if not (force or github_due(options) or github_state().get("pending")):
        return []
    github_state({"checked": time.time(), "pending": False})
    return [core.create_job({"source": "github", "ref": f"#{i['number']}", "title": i["title"],
                             "body": i.get("body") or "", "url": i.get("url")})
            for i in github_unseen(options)]


def pending_github(options):
    """Asks GitHub at most once per poll_seconds, so `watch` can call this in a loop."""
    state = github_state()
    if state.get("pending"):
        return True
    if not github_due(options):
        return False
    try:
        found = bool(github_unseen(options))
    except (Refused, ValueError):
        found = False
    github_state({"checked": time.time(), "pending": found})
    return found


def report_github(job, event, text):
    """Comment on the issue, when config.json sources.github.comment is true."""
    options = config().get("sources", {}).get("github", {})
    if not options.get("comment"):
        return
    body = {
        "accepted": f"Forge picked this up as job `{job['id']}` ({text} profile).",
        "done": f"Forge finished job `{job['id']}`. {text}" + (f"\n\n{job['pr_url']}" if job.get("pr_url") else ""),
        "rejected": f"Forge did not build this (job `{job['id']}`). {text}",
    }.get(event)
    if body:
        core.run(["gh", "issue", "comment", job["source"]["ref"].lstrip("#"), "--body", body,
                  *github_args(options)])


PULL = {"local": pull_local, "github": pull_github}
PENDING = {"local": pending_local, "github": pending_github}
REPORT = {"github": report_github}  # local and manual tasks have nobody to tell


def enabled():
    return [(kind, options) for kind, options in config().get("sources", {}).items()
            if options.get("enabled") and kind in PULL]


def pull(force=False):
    """New jobs from every enabled source. `force` skips a source's own throttling."""
    created = []
    for kind, options in enabled():
        try:
            created += PULL[kind](options, force)
        except (Refused, ValueError) as e:
            problems.append(f"source {kind}: {e}")
    return created


def pending():
    """Is there a task waiting in a source that hasn't become a job yet?"""
    return any(PENDING[kind](options) for kind, options in enabled() if kind in PENDING)


def report(job, event, text):
    """Tell the job's source what happened: accepted, done, rejected."""
    handler = REPORT.get(job["source"]["kind"])
    if handler:
        try:
            handler(job, event, text)
        except Refused as e:
            core.event(job, "report failed", str(e))
