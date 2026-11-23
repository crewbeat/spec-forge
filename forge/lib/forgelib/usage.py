"""Tokens each agent used, read from Claude Code's own transcripts.

Claude Code writes one transcript per subagent under
~/.claude/projects/<project>/<session>/subagents/, with the API's usage on
every reply. The factory reads those files itself, so the numbers don't depend
on the orchestrator passing anything along.

A run is matched to its transcript by the agent type, the `Job: <id>.` that
opens every prompt the factory writes, and the time it was spawned.
"""

import json
import os
import re
import time
from pathlib import Path

from .core import REPO

FIELDS = {"input": "input_tokens", "output": "output_tokens",
          "cache_read": "cache_read_input_tokens", "cache_write": "cache_creation_input_tokens"}


def transcript_dirs():
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    project = root / re.sub(r"[^A-Za-z0-9]", "-", str(REPO))
    return sorted(project.glob("*/subagents")) if project.is_dir() else []


def first_text(line):
    content = (line.get("message") or {}).get("content")
    if isinstance(content, list):
        content = " ".join(block.get("text", "") for block in content if isinstance(block, dict))
    return content if isinstance(content, str) else ""


def read(path):
    """One transcript: who ran, for which job, when, and what it used. None when it isn't ours."""
    try:
        meta = json.loads(path.with_suffix(".meta.json").read_text())
        agent = meta.get("agentType", "")
        if not agent.startswith("forge-"):
            return None
        seen, totals, job, started = set(), dict.fromkeys(FIELDS, 0), None, None
        with path.open(errors="replace") as f:
            for raw in f:
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if job is None and line.get("type") == "user":
                    match = re.match(r"Job: (\S+)\. ", first_text(line))
                    if not match:
                        return None
                    job, started = match.group(1), line.get("timestamp", "")[:19] + "Z"
                message = line.get("message") or {}
                usage = message.get("usage")
                # A reply is written once per content block, with the same usage each time.
                if line.get("type") != "assistant" or not usage or message.get("id") in seen:
                    continue
                seen.add(message.get("id"))
                for name, field in FIELDS.items():
                    totals[name] += usage.get(field) or 0
    except (OSError, ValueError):
        return None
    if job is None:
        return None
    return {"id": path.stem, "key": agent[len("forge-"):], "job": job, "started": started, **totals}


def fill(job):
    """Give every run that has no token count the numbers from its transcript."""
    missing = [u for u in job["usage"] if u.get("tokens") is None]
    if not missing:
        return
    claimed = {u.get("transcript") for u in job["usage"]}
    found = [t for folder in transcript_dirs() for t in map(read, sorted(folder.glob("agent-*.jsonl")))
             if t and t["job"] == job["id"] and t["id"] not in claimed]
    found.sort(key=lambda t: t["started"])
    for run in missing:
        # The agent starts within moments of the spawn; runs recorded before `at` existed pair up in order.
        match = next((t for t in found if t["key"] == run["key"]
                      and (not run.get("at") or t["started"] >= run["at"])), None)
        if match is None:
            continue
        found.remove(match)
        run.update({name: match[name] for name in FIELDS}, transcript=match["id"],
                   tokens=match["input"] + match["output"] + match["cache_write"])


def last_seen(job_id, key, since):
    """Seconds since the transcript of a run spawned at `since` last grew; None when it has none."""
    newest = None
    for folder in transcript_dirs():
        for meta in folder.glob("agent-*.meta.json"):
            path = meta.with_name(meta.name[:-len(".meta.json")] + ".jsonl")
            try:
                changed = path.stat().st_mtime
                if json.loads(meta.read_text()).get("agentType") != f"forge-{key}":
                    continue
                with path.open(errors="replace") as f:
                    first = next((json.loads(raw) for raw in f if '"user"' in raw), {})
            except (OSError, ValueError):
                continue
            if first_text(first).startswith(f"Job: {job_id}. ") and first.get("timestamp", "")[:19] + "Z" >= since:
                newest = max(newest or 0, changed)
    return None if newest is None else max(0, int(time.time() - newest))
