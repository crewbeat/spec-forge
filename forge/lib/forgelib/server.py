"""The dashboard's server: reads job files, records a person's decisions.

It binds to 127.0.0.1 only. It never runs agents: a decision made here is
written to the job, and the Claude Code session running the factory picks it up.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import core, pipeline, usage
from .core import FORGE, Refused, config, job_dir

MAX_BODY = 1_000_000


def job_view(job, jobs=()):
    folder = job_dir(job["id"])
    files = [p for p in folder.rglob("*") if p.is_file() and p.suffix in (".md", ".log")]
    view = dict(job)
    view["files"] = [str(p.relative_to(folder)) for p in sorted(files, key=lambda p: p.stat().st_mtime)]
    end = job["ended"]
    view["elapsed_s"] = core.seconds_between(job["started"], end) if job["started"] else 0
    waiting = job.get("waiting")
    view["waiting_now_s"] = core.seconds_between(waiting["since"]) if waiting else 0
    view["answers"] = pipeline.GATES[waiting["gate"]] if waiting and not job.get("decision") else {}
    view["effective"] = core.settings(job)
    verdicts = {}
    for path in files:
        rel = path.relative_to(folder)
        if len(rel.parts) == 2 and rel.parts[0].startswith("round-") and rel.suffix == ".md":
            fields = core.header(path)
            name = "gate" if rel.name == "checks.md" else rel.stem.replace("review-", "")
            verdict = fields.get("GATE") or fields.get("VERDICT")
            if verdict:
                verdicts.setdefault(rel.parts[0].split("-", 1)[1], {})[name] = verdict
    view["round_verdicts"] = verdicts
    view["running"] = []
    for key in job["inflight"]:
        spawned = job["spawned"].get(key, {})
        view["running"].append({
            "key": key, "model": spawned.get("model"),
            "seconds": core.seconds_between(spawned["at"]) if spawned else 0,
            # None: Claude Code has no transcript for this run, so the session never started the agent.
            "quiet_s": usage.last_seen(job["id"], key, spawned["at"]) if spawned else None,
        })
    view["blocked"] = "" if job["started"] else core.blocked_text(core.blockers(job, jobs))
    view["decided_s"] = core.seconds_between(job["decision"]["at"]) if job.get("decision") else 0
    view["tokens"] = sum(u["tokens"] or 0 for u in job["usage"])
    view["agent_s"] = sum(u["seconds"] for u in job["usage"])
    view["token_parts"] = {name: sum(u.get(name) or 0 for u in job["usage"])
                           for name in ("input", "cache_write", "output", "cache_read")}
    view["cost"] = core.cost(job)
    return view


def factory(jobs):
    """Is anything moving, and will anything move without the person doing something?"""
    try:
        last = core.seconds_between((core.JOBS / ".heartbeat").read_text().strip())
    except (OSError, ValueError):
        last = None
    live = [j for j in jobs if j["stage"] not in core.TERMINAL]
    return {
        "watching": core.watcher_alive(),
        "last_command_s": last,
        "agents": sum(len(j["inflight"]) for j in live),
        "undelivered": sum(1 for j in live if j.get("decision")) + sum(1 for j in live if not j["started"] and not core.blockers(j, jobs)),
        "held": sum(1 for j in live if not j["started"] and core.blockers(j, jobs)),
        "needs_you": sum(1 for j in live if j["waiting"] and not j["decision"]),
        "live": len(live),
    }


def activity(jobs, limit=40):
    rows = [{"at": e["at"], "job": j["id"], "number": j["number"], "event": e["event"], "detail": e["detail"], "n": n}
            for j in jobs for n, e in enumerate(j["events"])]
    return sorted(rows, key=lambda r: (r["at"], r["n"]), reverse=True)[:limit]


def state():
    cfg = config()
    jobs = core.all_jobs()
    return {
        "jobs": [job_view(j, jobs) for j in jobs],
        "factory": factory(jobs),
        "activity": activity(jobs),
        "profiles": list(cfg["profiles"]),
        "human": cfg["human"],
        "models": cfg["models"],
        "repo": str(core.REPO),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "forge"

    def log_message(self, *args):
        pass

    def send(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def local_request(self):
        """Refuse requests another website could make the browser send here."""
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in hosts}

    def do_GET(self):
        if not self.local_request():
            return self.send(403, {"error": "forbidden"})
        url = urlparse(self.path)
        try:
            if url.path in ("/", "/dashboard.html"):
                return self.send(200, (FORGE / "dashboard.html").read_bytes(), "text/html")
            if url.path == "/api/state":
                return self.send(200, state())
            if url.path == "/api/file":
                query = parse_qs(url.query)
                folder = job_dir(core.resolve(query["job"][0])).resolve()
                path = (folder / query["path"][0]).resolve()
                if folder not in path.parents or path.suffix not in (".md", ".log") or not path.is_file():
                    return self.send(404, {"error": "no such file"})
                return self.send(200, path.read_bytes(), "text/plain")
        except (Refused, KeyError) as e:
            return self.send(400, {"error": str(e)})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.local_request() or "application/json" not in self.headers.get("Content-Type", ""):
            return self.send(403, {"error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_BODY:
                return self.send(413, {"error": "too large"})
            body = json.loads(self.rfile.read(length) or b"{}")
            url = urlparse(self.path)
            if url.path == "/api/jobs":
                job = core.create_job({"source": "manual", "ref": "dashboard",
                                       "title": str(body.get("title", "")), "body": str(body.get("body", ""))})
                return self.send(200, {"job": job["id"]})
            if url.path == "/api/decide":
                job = core.load(core.resolve(str(body.get("job", ""))))
                pipeline.decide(job, str(body.get("decision", "")), str(body.get("comment", "")),
                                body.get("profile") or None)
                core.save(job)
                return self.send(200, {"ok": True})
        except Refused as e:
            return self.send(400, {"error": str(e)})
        except ValueError:
            return self.send(400, {"error": "bad request"})
        self.send(404, {"error": "not found"})


def serve(port=None):
    port = port or config().get("dashboard_port", 8700)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Forge dashboard: http://127.0.0.1:{port}/  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
