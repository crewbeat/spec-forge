"""The pipeline: what a job does next, and what a human decision changes.

`step(job)` settles a job from the files its agents wrote, runs the
deterministic stages itself (gate, delivery) and returns the actions the
orchestrator must take: spawn an agent, or wait for a person.
"""

import fnmatch
import time
from pathlib import Path

from . import core, deps, sources, usage
from .core import FORGE, REPO, Refused, config, event, header, job_dir, settings

# What a person may answer at each gate, and the label the dashboard shows.
GATES = {
    "intake": {"approve": "Approve", "changes": "Reconsider", "reject": "Reject task"},
    "questions": {"changes": "Answer", "reject": "Reject task"},
    "plan": {"approve": "Approve plan", "changes": "Request changes", "reject": "Reject task"},
    "design": {"approve": "Approve design", "changes": "Request changes", "reject": "Reject task"},
    "dependencies": {"approve": "Allow", "changes": "Request changes", "reject": "Reject task"},
    "ui_check": {"approve": "Looks good", "changes": "Request changes", "reject": "Reject task"},
    "blocked": {"changes": "Give direction", "reject": "Reject task"},
    "agent_error": {"approve": "Retry", "reject": "Reject task"},
    "deliver_error": {"approve": "Retry", "reject": "Stop here"},
    "commit": {"approve": "Commit", "reject": "Stop here"},
    "push": {"approve": "Push", "reject": "Stop here"},
    "pull_request": {"approve": "Open PR", "reject": "Stop here"},
    "merge": {"approve": "Merge", "reject": "Stop here"},
}
DELIVERY = ("commit", "push", "pull_request", "merge")
REMOTE_STEPS = ("push", "pull_request", "merge")
PLAN = ("spec", "design", "tests")  # the stages before the first build, in order
TAIL = 80


# ---- Small helpers ---------------------------------------------------------


def round_dir(job):
    path = job_dir(job["id"]) / f"round-{job['round']}"
    path.mkdir(exist_ok=True)
    return path


def wait(job, gate, about, file=None):
    job["waiting"] = {"gate": gate, "about": about, "file": file, "since": core.now()}
    job["decision"] = None
    event(job, "waiting", gate)


def finish(job, stage, note=None):
    job["stage"] = stage
    job["ended"] = core.now()
    job["waiting"] = None
    job["inflight"] = []
    if note:
        job["note"] = note
    event(job, stage, note or "")
    sources.report(job, stage, note or "")


def human_files(job):
    folder = job_dir(job["id"])
    files = sorted(
        (p for p in folder.glob("*.md") if p.name.startswith(("answers-", "feedback-"))),
        key=lambda p: p.stat().st_mtime,
    )
    if not files:
        return ""
    return (" The person has already given input, oldest first: " + ", ".join(p.name for p in files)
            + ". Read every one of them. Where they conflict with the task or an earlier file, the newest wins.")


def prompt_header(job):
    parts = [f"Job: {job['id']}.", f"Job folder: {job_dir(job['id'])}/.", f"Main checkout: {REPO}."]
    if job.get("worktree"):
        parts += [f"Worktree: {job['worktree']}.", f"Base commit: {job['base_commit']}."]
    return " ".join(parts)


def spawn(job, key, prompt):
    """An action that starts one agent, unless it is already running."""
    if key in job["inflight"]:
        return []
    job["inflight"].append(key)
    moved = core.refresh_worktree(job)
    if moved:
        event(job, "base", f"worktree moved to {moved[:7]}, the base branch as it is now")
    model = settings(job)["models"].get(key) or job["agents"]["reviewers"].get(key.replace("reviewer-", ""))
    job["spawned"][key] = {"at": core.now(), "model": model}
    event(job, "spawn", key)
    return [{
        "action": "spawn",
        "job": job["id"],
        "key": key,
        "agent": f"forge-{key}",
        "model": model,
        "prompt": f"{prompt_header(job)} {prompt}{human_files(job)}",
        "then": f"forge/bin/forge tick {job['id']} {key}",
    }]


def artifact(job, key):
    folder = job_dir(job["id"])
    if key == "intake":
        return folder / "intake.md"
    if key == "spec-writer":
        return folder / "spec.md"
    if key == "architect":
        return folder / "design.md"
    if key == "tester":
        return folder / "tests.md"
    if key == "builder":
        return folder / "build.md"
    return folder / f"round-{job['round']}" / f"review-{key.replace('reviewer-', '')}.md"


def artifact_ok(job, key):
    """Did the agent leave a file this stage can act on?"""
    fields = header(artifact(job, key))
    if key == "intake":
        return fields.get("DECISION") in ("ACCEPT", "NEEDS-INFO", "REJECT")
    if key in ("spec-writer", "architect"):
        return fields.get("STATUS") in ("READY", "QUESTIONS")
    if key == "tester":
        return fields.get("STATUS") in ("READY", "BLOCKED")
    if key == "builder":
        return fields.get("STATUS") in ("READY", "BLOCKED", "DISPUTE") and fields.get("ROUND") == str(job["round"])
    return fields.get("VERDICT") in ("PASS", "CHANGES")


def finished(job, key, tokens=None):
    """An agent returned. Record what it used; count the attempt when it left nothing usable."""
    if key in job["inflight"]:
        job["inflight"].remove(key)
    started = job["spawned"].pop(key, None)
    if started:
        job["usage"].append({"key": key, "model": started["model"], "round": job["round"], "at": started["at"],
                             "seconds": core.seconds_between(started["at"]), "tokens": tokens})
    if job["stage"] in core.TERMINAL or job.get("waiting"):
        return
    if artifact_ok(job, key):
        job["attempts"].pop(key, None)
        return
    job["attempts"][key] = job["attempts"].get(key, 0) + 1
    if job["attempts"][key] >= 2:
        name = artifact(job, key).name
        wait(job, "agent_error", f"{key} finished twice without a valid {name}.", name)


# ---- Stages ----------------------------------------------------------------


def next_stage(job, after):
    """Move on from a stage, following the profile's stage list."""
    stages = settings(job)["stages"]
    if after == "intake" or after in PLAN:
        later = PLAN[PLAN.index(after) + 1:] if after in PLAN else PLAN
        job["stage"] = next((s for s in later if s in stages), None)
        if job["stage"] is None:
            start_round(job, "first build")
    elif after == "gate":
        if "review" in stages and job["agents"]["reviewers"]:
            job["stage"] = "review"
        else:
            next_stage(job, "review")
    elif after == "review":
        ui = "ui" in ((job.get("intake") or {}).get("surfaces") or [])
        job["stage"] = "ui-check" if ui and settings(job)["human"]["ui_check"] == "approve" else "deliver"
    elif after == "ui-check":
        job["stage"] = "deliver"
    event(job, "stage", job["stage"])


def start_round(job, reason):
    if job["round"] >= job["round_limit"]:
        folder = job_dir(job["id"]) / f"round-{job['round']}"
        asked = [p.name for p in sorted(folder.glob("review-*.md")) if header(p).get("VERDICT") == "CHANGES"]
        name = "checks.md" if reason == "the gate failed" else asked[0] if asked else None
        wait(job, "blocked", f"Round limit reached ({job['round']} rounds) and the last one failed: {reason}.",
             f"round-{job['round']}/{name}" if name else None)
        return
    job["round"] += 1
    job["round_reason"] = reason
    job["stage"] = "build"
    job["gate"] = None
    job["reviews"] = {}
    event(job, "round", f"{job['round']}: {reason}")


def accept(job, profile):
    job["profile"] = profile
    reviewers = {}
    cfg = config()
    names = list(cfg["reviewers"].get("always", []))
    for surface in job["intake"]["surfaces"]:
        names += cfg["reviewers"].get("surfaces", {}).get(surface, [])
    models = settings(job)["models"]
    for name in names:
        model = models.get(f"reviewer-{name}")
        if model is None:
            raise Refused(f"config.json models has no 'reviewer-{name}'")
        reviewers[name] = model
    job["agents"] = {"reviewers": reviewers}
    core.create_worktree(job)
    event(job, "accepted", profile)
    sources.report(job, "accepted", profile)
    next_stage(job, "intake")


def stage_intake(job):
    if not artifact_ok(job, "intake"):
        policy = ("If something essential is missing and only the person can answer it, return NEEDS-INFO."
                  if settings(job)["human"]["questions"] == "ask" else
                  "Do not return NEEDS-INFO: make the most reasonable assumption and list it.")
        return spawn(job, "intake", f"Decide whether and how to build the task in task.md. {policy}")
    fields = header(artifact(job, "intake"))
    profiles = config()["profiles"]
    profile = fields.get("PROFILE", "").lower()
    job["intake"] = {
        "decision": fields["DECISION"],
        "profile": profile if profile in profiles else ("medium" if "medium" in profiles else next(iter(profiles))),
        "risk": fields.get("RISK", "medium").lower(),
        "surfaces": [s.strip().lower() for s in fields.get("SURFACES", "").split(",")
                     if s.strip() and s.strip().lower() != "none"],
    }
    decision = fields["DECISION"]
    if decision == "NEEDS-INFO":
        wait(job, "questions", "Intake needs answers before this can be planned.", "intake.md")
    elif settings(job)["human"]["intake"] == "approve":
        about = ("Intake wants to reject this task." if decision == "REJECT" else
                 f"Intake suggests the {job['intake']['profile']} profile, risk {job['intake']['risk']}.")
        wait(job, "intake", about, "intake.md")
    elif decision == "REJECT":
        finish(job, "rejected", "Rejected at intake.")
    else:
        accept(job, job["intake"]["profile"])
    return None


def stage_spec(job):
    if not artifact_ok(job, "spec-writer"):
        policy = ("If a decision only the person can make blocks the spec, return STATUS: QUESTIONS."
                  if settings(job)["human"]["questions"] == "ask" else
                  "Do not return STATUS: QUESTIONS: make the most reasonable assumption and list it.")
        later = " An architect designs this job after you." if "design" in settings(job)["stages"] else ""
        return spawn(job, "spec-writer", f"Write spec.md for the task as scoped in intake.md.{later} {policy}")
    if header(artifact(job, "spec-writer"))["STATUS"] == "QUESTIONS":
        wait(job, "questions", "The spec writer needs answers.", "spec.md")
    elif settings(job)["human"]["plan"] == "approve":
        wait(job, "plan", "The spec is ready for your approval.", "spec.md")
    else:
        next_stage(job, "spec")
    return None


def contract(job):
    folder = job_dir(job["id"])
    text = "spec.md is the contract." if (folder / "spec.md").exists() else \
        "There is no spec for this job: intake.md has the scope."
    if (folder / "design.md").exists():
        text += " design.md has the architecture, the contracts and the task order: follow it."
    return text


def stage_design(job):
    if not artifact_ok(job, "architect"):
        policy = ("If a decision only the person can make blocks the design, return STATUS: QUESTIONS."
                  if settings(job)["human"]["questions"] == "ask" else
                  "Do not return STATUS: QUESTIONS: make the most reasonable assumption and list it.")
        return spawn(job, "architect", f"Write design.md. {contract(job)} {policy}")
    if header(artifact(job, "architect"))["STATUS"] == "QUESTIONS":
        wait(job, "questions", "The architect needs answers.", "design.md")
    elif settings(job)["human"]["design"] == "approve":
        wait(job, "design", "The design is ready for your approval.", "design.md")
    else:
        next_stage(job, "design")
    return None


def changed_files(job):
    """Every file the worktree adds or changes against the base commit, with its hash."""
    worktree = Path(job["worktree"])
    core.git(worktree, "add", "-A")
    names = core.git(worktree, "diff", "--cached", "--name-only", "--diff-filter=ACMR", job["base_commit"])
    return {name: core.sha256(worktree / name) for name in names.splitlines() if (worktree / name).is_file()}


def stage_tests(job):
    """A tester writes the tests before the builder. What it wrote is then locked."""
    if not artifact_ok(job, "tester"):
        if job["pre_tests"] is None:
            job["pre_tests"] = changed_files(job)
        why = ("The builder says some of your tests are wrong: read `## Test problems` in build.md and fix "
               "the tests that really are wrong. Leave the rest."
               if job["round_reason"] == "tests disputed" else
               "Write the tests that will prove this job is built, before any of it is built.")
        return spawn(job, "tester", f"{why} {contract(job)} Write tests.md.")
    if header(artifact(job, "tester"))["STATUS"] == "BLOCKED":
        wait(job, "blocked", "The tester is blocked and needs direction.", "tests.md")
        return None
    now, before = changed_files(job), job["pre_tests"] or {}
    locked = {path: now[path] for path in job["tests"] if path in now}
    locked.update({path: digest for path, digest in now.items() if before.get(path) != digest})
    job["tests"] = locked
    job["pre_tests"] = None
    event(job, "tests", f"{len(locked)} files locked")
    if job["round"] == 0:
        next_stage(job, "tests")
    else:
        job["stage"] = "build"
        event(job, "stage", "build")
    return None


def locked_changes(job):
    """The tester's files that someone changed or removed after the tester."""
    hits = []
    for path, digest in sorted(job["tests"].items()):
        file = Path(job["worktree"]) / path
        if not file.is_file():
            hits.append(f"D {path}")
        elif core.sha256(file) != digest:
            hits.append(f"M {path}")
    return hits


def stage_build(job):
    if not artifact_ok(job, "builder"):
        n = job["round"]
        why = {
            "first build": "Build the task.",
            "the gate failed": f"The gate failed: read round-{n - 1}/checks.md and checks.log and fix it.",
            "review asked for changes": f"Address every VERDICT: CHANGES in round-{n - 1}/.",
            "feedback": "The person gave direction: the newest feedback file says what to change.",
            "tests disputed": "The tester has looked at the tests you disputed: read tests.md, then build.",
        }.get(job.get("round_reason"), "Continue the build.")
        tests = (" A tester wrote this job's tests before you (tests.md lists them): make them pass. Those "
                 "files are locked, and the gate fails if you change one. If a locked test is wrong, return "
                 "STATUS: DISPUTE with `## Test problems`." if job["tests"] else "")
        return spawn(job, "builder", f"Build round {n}. {why} {contract(job)}{tests} Write build.md with ROUND: {n}.")
    status = header(artifact(job, "builder"))["STATUS"]
    if status == "DISPUTE" and job["tests"]:
        start_round(job, "tests disputed")
        if not job["waiting"]:
            core.archive(job_dir(job["id"]) / "tests.md")
            job["pre_tests"] = None
            job["stage"] = "tests"
            event(job, "stage", "tests")
    elif status != "READY":
        wait(job, "blocked", "The builder is blocked and needs direction.", "build.md")
    else:
        job["stage"] = "gate"
        event(job, "stage", "gate")
    return None


def protected_changes(job):
    """Existing files under protected_paths that the worktree modified or deleted."""
    diff = core.git(job["worktree"], "diff", "--cached", "--name-status", job["base_commit"])
    hits = []
    for line in diff.splitlines():
        status, *paths = line.split("\t")
        if status.startswith("A"):
            continue
        if any(fnmatch.fnmatch(paths[0], pattern) for pattern in config().get("protected_paths", [])):
            hits.append(f"{status[0]} {paths[0]}")
    return hits


def stage_gate(job):
    """Run the repo's checks in the worktree. The script, not an agent, decides."""
    worktree = job["worktree"]
    core.git(worktree, "add", "-A")
    protected = protected_changes(job)
    locked = locked_changes(job)
    new_deps, deps_text, missing, ask = new_dependencies(job)
    start = time.monotonic()
    # The main checkout's checks.sh, so a job can't edit the checks it is judged by.
    res = core.run(["bash", str(FORGE / "checks.sh")], cwd=worktree, check=False)
    seconds = int(time.monotonic() - start)
    log = res.stdout + res.stderr
    verdict = "PASS" if res.returncode == 0 and not protected and not locked and not missing else "FAIL"
    folder = round_dir(job)
    (folder / "checks.log").write_text(log)
    tail = "\n".join(log.rstrip().splitlines()[-TAIL:])
    (folder / "checks.md").write_text(
        f"GATE: {verdict}\n\n## Summary\n"
        f"- checks.sh {'passed' if res.returncode == 0 else f'failed with exit {res.returncode}'} in {seconds}s\n"
        f"- Protected paths: {'changed: ' + ', '.join(protected) if protected else 'none changed'}\n"
        f"- Locked tests: {'changed after the tester: ' + ', '.join(locked) if locked else 'none changed'}\n"
        f"- New dependencies: {len(new_deps)}"
        f"{' (' + str(len(missing)) + ' not found in the registry)' if missing else ''}\n\n"
        + (f"## New dependencies\n{deps_text}\n\n" if new_deps else "") +
        f"## checks.sh\nLast {TAIL} lines of checks.log:\n\n```\n{tail}\n```\n"
    )
    job["gate"] = verdict
    event(job, "gate", verdict)
    if verdict != "PASS":
        start_round(job, "the gate failed")
    elif ask:
        job["deps_pending"] = [d["key"] for d in ask]
        names = ", ".join(d["name"] or d["manifest"] for d in ask)
        wait(job, "dependencies", f"This change adds dependencies: {names}.", f"round-{job['round']}/checks.md")
    else:
        next_stage(job, "gate")
    return None


def new_dependencies(job):
    """What the change adds to the repo's manifests: (all, text, not found, needs a person)."""
    options = config().get("dependencies", {})
    min_age = options.get("min_age_days", 30)
    found = deps.added(job["worktree"], job["base_commit"])
    lines, missing, ask = [], [], []
    for dep in found:
        check = None
        private = any(fnmatch.fnmatch(dep["name"] or "", pattern) for pattern in options.get("ignore", []))
        if options.get("registry_check", True) and dep["name"] and not private:
            check = job["deps_checked"].get(dep["key"])
            if check is None or check["found"] is None:
                check = job["deps_checked"][dep["key"]] = deps.lookup(dep)
        lines.append("- " + deps.describe(dep, check, min_age))
        if check and check["found"] is False:
            missing.append(dep)
            continue
        if dep["key"] in job["deps_approved"]:
            continue
        young = bool(check) and check["age_days"] is not None and check["age_days"] < min_age
        if young or settings(job)["human"]["dependencies"] == "approve":
            ask.append(dep)
    return found, "\n".join(lines), missing, ask


def stage_review(job):
    actions, verdicts = [], {}
    for name in job["agents"]["reviewers"]:
        key = f"reviewer-{name}"
        if artifact_ok(job, key):
            verdicts[name] = header(artifact(job, key))["VERDICT"]
        else:
            round_dir(job)
            actions += spawn(job, key, f"Review round {job['round']}. Write round-{job['round']}/review-{name}.md.")
    job["reviews"] = verdicts
    if len(verdicts) < len(job["agents"]["reviewers"]):
        return actions
    event(job, "review", ", ".join(f"{name} {verdict}" for name, verdict in verdicts.items()))
    if all(v == "PASS" for v in verdicts.values()):
        next_stage(job, "review")
    else:
        start_round(job, "review asked for changes")
    return None


def stage_ui_check(job):
    wait(job, "ui_check", f"Check the UI yourself. The change is in {job['worktree']}.", "build.md")
    return None


def delivery_plan():
    """The delivery steps for this repo, and whether they end in a local merge."""
    mode = config().get("delivery", "auto")
    local = mode == "local" or (mode == "auto" and not core.has_remote())
    return (("commit", "merge") if local else DELIVERY), local


def merge_local(job):
    """Merge the job's branch into the base branch in the main checkout, without a remote."""
    base, branch = config()["base_branch"], job["branch"]
    if core.git(REPO, "rev-parse", "--abbrev-ref", "HEAD").strip() != base:
        raise Refused(f"the main checkout isn't on {base}: switch to it, then retry")
    if core.git(REPO, "status", "--porcelain", "--untracked-files=no").strip():
        raise Refused("the main checkout has uncommitted changes: commit or stash them, then retry")
    method = config().get("merge_method", "squash")
    subject = core.git(REPO, "log", "-1", "--format=%s", job["commit"]).strip()
    if method == "squash":
        res = core.run(["git", "merge", "--squash", branch], check=False)
        if res.returncode != 0:
            core.run(["git", "reset", "--merge"], check=False)
            raise Refused(f"{branch} doesn't merge cleanly into {base}: {(res.stderr or res.stdout).strip()}")
        core.git(REPO, "commit", "-q", "-m", subject)
    elif method == "rebase":
        res = core.run(["git", "merge", "--ff-only", branch], check=False)
        if res.returncode != 0:
            raise Refused(f"{base} has moved since the job started, so {branch} can't be fast-forwarded: "
                          "rebase it, or set merge_method to squash or merge")
    else:
        res = core.run(["git", "merge", "--no-ff", "-m", f"Merge {branch}: {subject}", branch], check=False)
        if res.returncode != 0:
            core.run(["git", "merge", "--abort"], check=False)
            raise Refused(f"{branch} doesn't merge cleanly into {base}: {(res.stderr or res.stdout).strip()}")


def do_delivery(job, name, local=False):
    worktree = job["worktree"]
    folder = job_dir(job["id"])
    if name == "commit":
        core.git(worktree, "add", "-A")
        if not core.git(worktree, "status", "--porcelain").strip():
            if core.git(worktree, "rev-parse", "HEAD").strip() == job["base_commit"]:
                raise Refused("there is nothing to commit: the worktree has no changes")
        else:
            message = header(folder / "build.md").get("COMMIT") or job["title"]
            core.git(worktree, "commit", "-q", "-m", f"{message}\n\nForge job {job['id']}")
        job["commit"] = core.git(worktree, "rev-parse", "HEAD").strip()
    elif name == "merge" and local:
        merge_local(job)
    elif name == "push":
        core.git(worktree, "push", "-q", "-u", "origin", job["branch"])
    elif name == "pull_request":
        body = folder / "pr-body.md"
        closes = f"\n\nCloses {job['source']['ref']}." if job["source"]["kind"] == "github" else ""
        body.write_text((core.section(folder / "build.md", "Summary") or job["title"])
                        + f"{closes}\n\nForge job `{job['id']}`.\n")
        out = core.run(["gh", "pr", "create", "--head", job["branch"], "--base", config()["base_branch"],
                        "--title", job["title"], "--body-file", str(body)], cwd=worktree).stdout
        job["pr_url"] = out.strip().splitlines()[-1]
    elif name == "merge":
        core.run(["gh", "pr", "merge", job["pr_url"], f"--{config().get('merge_method', 'squash')}"], cwd=worktree)
    job["delivered"].append(name)
    event(job, "delivered", name)


def stage_deliver(job):
    human = settings(job)["human"]
    steps, local = delivery_plan()
    for name in steps:
        if name in job["delivered"]:
            continue
        if human[name] == "off":
            finish(job, "done", f"Stopped before {name.replace('_', ' ')}: it is off in config.json.")
            return None
        if not local and name in REMOTE_STEPS and not core.has_remote():
            finish(job, "done", f"Stopped before {name.replace('_', ' ')}: this repo has no `origin` remote.")
            return None
        if human[name] == "approve" and job.get("approved_step") != name:
            where = job["pr_url"] or job["branch"]
            if local and name == "merge":
                where = f"{job['branch']} into {config()['base_branch']} in this checkout, no remote involved"
            wait(job, name, f"Ready to {name.replace('_', ' ')} ({where}).", "build.md")
            return None
        try:
            do_delivery(job, name, local)
        except Refused as e:
            wait(job, "deliver_error", f"{name.replace('_', ' ')} failed: {e}")
            return None
        job["approved_step"] = None
    finish(job, "done", "Delivered: " + ", ".join(s.replace("_", " ") for s in job["delivered"]) + ".")
    return None


STAGES = {
    "intake": stage_intake,
    "spec": stage_spec,
    "design": stage_design,
    "tests": stage_tests,
    "build": stage_build,
    "gate": stage_gate,
    "review": stage_review,
    "ui-check": stage_ui_check,
    "deliver": stage_deliver,
}


# ---- Human decisions -------------------------------------------------------


def decide(job, decision, comment="", profile=None):
    """Record a person's answer to the gate the job waits at."""
    waiting = job.get("waiting")
    if not waiting:
        raise Refused(f"{job['id']} isn't waiting for a decision (stage {job['stage']})")
    if job.get("decision"):
        raise Refused(f"{job['id']} already has a decision that hasn't been applied yet")
    gate = waiting["gate"]
    if decision not in GATES[gate]:
        raise Refused(f"{gate} takes: {', '.join(GATES[gate])}")
    comment = (comment or "").strip()
    if decision == "changes" and not comment:
        raise Refused("say what to change: a comment is required")
    if profile is not None:
        if gate != "intake" or decision != "approve":
            raise Refused("a profile can only be set when approving intake")
        if profile not in config()["profiles"]:
            raise Refused(f"profile {profile!r} isn't in config.json ({', '.join(config()['profiles'])})")
    job["decision"] = {"gate": gate, "decision": decision, "comment": comment, "profile": profile, "at": core.now()}
    with (job_dir(job["id"]) / "decisions.md").open("a") as f:
        f.write(f"## {core.now()} · {gate} · {decision}\n\n{comment or '(no comment)'}\n\n")
    event(job, "decision", f"{gate}: {decision}")


def save_input(job, prefix, gate, text):
    path = core.next_numbered(job_dir(job["id"]), prefix)
    path.write_text(f"GATE: {gate}\nAT: {core.now()}\n\nThe person wrote:\n\n{text}\n")
    return path


def apply_decision(job):
    d = job["decision"]
    waiting = job["waiting"]
    gate, decision, comment = d["gate"], d["decision"], d["comment"]
    job["waited_s"] += core.seconds_between(waiting["since"], d["at"])
    job["waiting"] = None
    job["decision"] = None
    folder = job_dir(job["id"])
    stop = gate in DELIVERY or gate == "deliver_error"

    if decision == "reject":
        if stop:
            finish(job, "done", f"Stopped at {gate.replace('_', ' ')} by the person."
                   + (f" {comment}" if comment else ""))
        else:
            finish(job, "rejected", comment or "Rejected by the person.")
        return
    if gate == "intake":
        if decision == "approve":
            if job["intake"]["decision"] == "REJECT":
                finish(job, "rejected", "Rejected at intake.")
            else:
                accept(job, d["profile"] or job["intake"]["profile"])
        else:
            save_input(job, "feedback", gate, comment)
            core.archive(folder / "intake.md")
    elif gate == "questions":
        save_input(job, "answers", gate, comment)
        core.archive(folder / waiting["file"])
    elif gate == "plan":
        if decision == "approve":
            next_stage(job, "spec")
        else:
            save_input(job, "feedback", gate, comment)
            core.archive(folder / "spec.md")
    elif gate == "design":
        if decision == "approve":
            next_stage(job, "design")
        else:
            save_input(job, "feedback", gate, comment)
            core.archive(folder / "design.md")
    elif gate == "dependencies":
        if decision == "approve":
            job["deps_approved"] += job["deps_pending"]
            job["deps_pending"] = []
            next_stage(job, "gate")
        else:
            save_input(job, "feedback", gate, comment)
            job["round_limit"] = job["round"] + config()["max_rounds"]
            start_round(job, "feedback")
    elif gate == "ui_check":
        if decision == "approve":
            next_stage(job, "ui-check")
        else:
            save_input(job, "feedback", gate, comment)
            job["round_limit"] = job["round"] + config()["max_rounds"]
            start_round(job, "feedback")
    elif gate == "blocked" and job["stage"] == "tests":
        save_input(job, "feedback", gate, comment)
        core.archive(folder / "tests.md")
    elif gate == "blocked":
        save_input(job, "feedback", gate, comment)
        job["round_limit"] = job["round"] + config()["max_rounds"]
        start_round(job, "feedback")
    elif gate == "agent_error":
        job["attempts"] = {}
    elif gate in DELIVERY:
        job["approved_step"] = gate
    # deliver_error + approve: nothing to change, the stage runs again.


# ---- The step --------------------------------------------------------------


def step(job):
    """Advance a job as far as it goes without an agent or a person."""
    actions = []
    for _ in range(30):
        if job["stage"] in core.TERMINAL:
            break
        if job.get("waiting"):
            if not job.get("decision"):
                w = job["waiting"]
                actions.append({"action": "wait", "job": job["id"], "gate": w["gate"], "about": w["about"],
                                "file": str(job_dir(job["id"]) / w["file"]) if w.get("file") else None,
                                "answers": list(GATES[w["gate"]])})
                break
        try:
            if job.get("waiting"):
                apply_decision(job)
                continue
            result = STAGES[job["stage"]](job)
        except Refused as e:
            job["decision"] = None
            wait(job, "agent_error", f"The {job['stage']} stage failed: {e}")
            continue
        if result is not None:
            actions += result
            break
    usage.fill(job)
    core.save(job)
    return actions


def schedule(only=None, resume=False):
    """Step the jobs that can move. Returns every action for the orchestrator."""
    actions = []
    jobs = core.all_jobs()
    if resume:
        for job in jobs:
            job["inflight"] = []
    live = [j for j in jobs if j["stage"] not in core.TERMINAL]
    # Started jobs first: one that finishes here frees its slot for a new job.
    for job in live:
        if job["started"] and (only is None or job["id"] in only or job["decision"]):
            actions += step(job)
    running = [j for j in live if j["started"] and j["stage"] not in core.TERMINAL
               and not (j["waiting"] and not j["decision"])]
    slots = config()["concurrency"] - len(running)
    for job in live:
        if job["started"]:
            continue
        waits = core.blockers(job, jobs)
        if waits:
            actions.append({"action": "hold", "job": job["id"], "about": core.blocked_text(waits)})
            continue
        if slots <= 0:
            actions.append({"action": "hold", "job": job["id"], "about": "No free slot yet."})
            continue
        slots -= 1
        job["started"] = core.now()
        actions += step(job)
    return actions
