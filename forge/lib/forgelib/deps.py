"""New dependencies a job adds, and whether the registry knows them.

An agent can name a package that doesn't exist, or one registered last week by
someone who guessed the name an agent would invent. The gate lists what a job
adds to the repo's manifests and, when allowed, asks the public registry
whether each package exists and how old it is.
"""

import datetime as dt
import fnmatch
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import core

TIMEOUT = 10
MAX_BYTES = 40_000_000


# ---- What each manifest declares -------------------------------------------


def names_package_json(text):
    data = json.loads(text)
    out = set()
    for block in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        out |= set(data.get(block) or {})
    return out


def names_composer_json(text):
    data = json.loads(text)
    out = set(data.get("require") or {}) | set(data.get("require-dev") or {})
    return {n for n in out if "/" in n}  # skips php and ext-*


def names_requirements(text):
    out = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if match and not line.startswith("-"):
            out.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return out


def names_go_mod(text):
    return set(re.findall(r"^\s*(?:require\s+)?([a-z0-9.-]+\.[a-z]{2,}/\S+)\s+v\S+", text, re.M))


def names_gemfile(text):
    return set(re.findall(r"""^\s*gem\s+['"]([^'"]+)['"]""", text, re.M))


# pattern on the file name -> (ecosystem, parser). None: we can only say the file changed.
MANIFESTS = [
    ("package.json", "npm", names_package_json),
    ("composer.json", "packagist", names_composer_json),
    ("requirements*.txt", "pypi", names_requirements),
    ("go.mod", "go", names_go_mod),
    ("Gemfile", "rubygems", names_gemfile),
    ("pyproject.toml", None, None),
    ("Cargo.toml", None, None),
    ("pom.xml", None, None),
    ("build.gradle*", None, None),
]


def manifest_kind(path):
    name = path.rsplit("/", 1)[-1]
    for pattern, ecosystem, parser in MANIFESTS:
        if fnmatch.fnmatch(name, pattern):
            return ecosystem, parser
    return None


def added(worktree, base_commit):
    """Dependencies the staged change adds: [{key, manifest, ecosystem, name}]."""
    out = []
    diff = core.git(worktree, "diff", "--cached", "--name-status", base_commit)
    for line in diff.splitlines():
        status, *paths = line.split("\t")
        path = paths[-1]
        kind = manifest_kind(path)
        if status.startswith("D") or kind is None or "node_modules/" in path or "vendor/" in path:
            continue
        ecosystem, parser = kind
        if parser is None:
            out.append({"key": f"file:{path}", "manifest": path, "ecosystem": None, "name": None})
            continue
        try:
            after = parser((core.Path(worktree) / path).read_text(errors="replace"))
            before = set()
            if not status.startswith("A"):
                old = core.run(["git", "show", f"{base_commit}:{paths[0]}"], cwd=worktree, check=False)
                before = parser(old.stdout) if old.returncode == 0 else set()
        except (ValueError, AttributeError, TypeError):
            out.append({"key": f"file:{path}", "manifest": path, "ecosystem": None, "name": None})
            continue
        for name in sorted(after - before):
            out.append({"key": f"{ecosystem}:{name}", "manifest": path, "ecosystem": ecosystem, "name": name})
    return out


# ---- What the registry says ------------------------------------------------


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "forge-dependency-check", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        return res.read(MAX_BYTES)


def age_days(stamp):
    try:
        then = dt.datetime.strptime(stamp[:19].replace(" ", "T"), "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None
    return max(0, (dt.datetime.utcnow() - then).days)


def first_release(ecosystem, name):
    """When the package was first published, as text, or None when the registry doesn't say."""
    quote = urllib.parse.quote
    if ecosystem == "npm":
        return json.loads(fetch("https://registry.npmjs.org/" + quote(name, safe="@"))).get("time", {}).get("created")
    if ecosystem == "pypi":
        releases = json.loads(fetch(f"https://pypi.org/pypi/{quote(name)}/json")).get("releases", {})
        times = [f["upload_time"] for files in releases.values() for f in files if f.get("upload_time")]
        return min(times) if times else None
    if ecosystem == "packagist":
        data = json.loads(fetch(f"https://repo.packagist.org/p2/{quote(name.lower())}.json"))
        times = [v["time"] for v in data.get("packages", {}).get(name.lower(), []) if v.get("time")]
        return min(times) if times else None
    if ecosystem == "rubygems":
        fetch(f"https://rubygems.org/api/v1/gems/{quote(name)}.json")
        return None
    if ecosystem == "go":
        escaped = re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), name)
        fetch(f"https://proxy.golang.org/{quote(escaped)}/@v/list")
        return None
    return None


def lookup(dep):
    """{'found': True|False|None, 'age_days': int|None}. None means the check couldn't be made."""
    if not dep["ecosystem"]:
        return {"found": None, "age_days": None}
    try:
        return {"found": True, "age_days": age_days(first_release(dep["ecosystem"], dep["name"]))}
    except urllib.error.HTTPError as e:
        return {"found": False if e.code in (404, 410) else None, "age_days": None}
    except (OSError, ValueError, KeyError, TypeError):
        return {"found": None, "age_days": None}


def describe(dep, check, min_age):
    if dep["name"] is None:
        return f"{dep['manifest']} changed (Forge can't read which packages this file adds)"
    text = f"{dep['name']} ({dep['ecosystem']}, in {dep['manifest']})"
    if check is None:
        return text + ": not looked up"
    if check["found"] is False:
        return text + ": NOT FOUND in the registry"
    if check["found"] is None:
        return text + ": the registry couldn't be reached"
    if check["age_days"] is None:
        return text + ": exists"
    young = f", newer than {min_age} days" if check["age_days"] < min_age else ""
    return text + f": exists, first published {check['age_days']} days ago{young}"
