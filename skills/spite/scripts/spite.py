#!/usr/bin/env python3
"""spite: runner for the SPITE clean-room pipeline.

    Survey -> Partition -> Isolate -> Test -> Emit

One command per step. Each step launches one isolated session with the tools
its role is allowed, checks what the session did, and writes a gate record.

The runner is the only thing that touches both repos. It carries exactly one
kind of file across the gate (the frozen spec, and later reviewed answers to
clean-side questions) and it never passes text from one side's session to the
other's. Standard library only.
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I keeps the script's folder off the path
import spite_checks as sc  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
AGENTS = SKILL_DIR / "agents"
ASSETS = SKILL_DIR / "assets"
GUARD = SKILL_DIR / "scripts" / "spite_guard.py"

ALWAYS_DENY = [".git", ".claude", ".spite", "records"]
ROLES = {
    "survey": dict(side="dirty", read_dirs=["survey"], read_files=["spite.json"],
                   write_dirs=["survey"], write_files=[]),
    "partition": dict(side="dirty", read_dirs=["survey", "spec"], read_files=["spite.json"],
                      write_dirs=["spec"], write_files=[]),
    "reviewer": dict(side="dirty", read_dirs=["survey", "spec", "qa"], read_files=["spite.json"],
                     write_dirs=["spec", "qa"], write_files=[]),
    "builder": dict(side="clean", read_dirs=["."], read_files=[], write_dirs=["."], write_files=[],
                    write_deny_dirs=["spec", "tests", "provenance", "qa/answers", "reports",
                                     "LICENSE", "PROVENANCE.md", "spite.json"]),
    "tester": dict(side="clean", read_dirs=["spec", "tests", "qa/answers", "reports"],
                   read_files=["INTERFACE.md", "README.md", "spite.json"],
                   write_dirs=["tests"], write_files=[]),
    "emit": dict(side="clean", read_dirs=["."], read_files=[], write_dirs=["docs"],
                 write_files=["README.md"]),
    # A second, adversarial reader of the reviewed spec. It may write its findings and nothing else.
    "auditor": dict(side="dirty", read_dirs=["survey", "spec"], read_files=["spite.json"],
                    write_dirs=[], write_files=["spec/audit-findings.md"]),
    # A blind reader: it is given the reviewed spec alone, in an empty folder, and asked what
    # product it describes. It belongs to neither side and can write nothing.
    "identify": dict(side="blind", read_dirs=["."], read_files=[], write_dirs=[], write_files=[]),
    # Looks a list of candidates over against the target gate, from search results only.
    "scout": dict(side="dirty", read_dirs=["."], read_files=[], write_dirs=["batches"], write_files=[]),
}
CODENAMES = ["anvil", "beacon", "cedar", "delta", "ember", "fjord", "garnet", "harbor", "indigo",
             "juniper", "kestrel", "lantern", "meadow", "nimbus", "orchard", "pumice", "quarry",
             "russet", "sable", "tundra", "umber", "vesper", "willow", "yarrow", "zephyr"]
SOURCES_HEADER = ["id", "url", "title", "accessed", "kind", "access"]
LICENSES = {"AGPL-3.0": "AGPL-3.0.txt", "Apache-2.0": "Apache-2.0.txt"}
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
WORD = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")
SKIP_DIRS = {".git", ".claude", ".spite", "__pycache__", "node_modules", ".venv", "target", "dist"}


# ----------------------------------------------------------------- utilities

def die(msg, code=1):
    print(f"spite: {msg}", file=sys.stderr)
    sys.exit(code)


def now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def stamp():
    return now().strftime("%Y%m%dT%H%M%SZ")


def posix(p):
    return str(p).replace("\\", "/")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def read_text(path):
    """Text of a file, or None when it is binary or too large to compare."""
    try:
        if path.stat().st_size > 2_000_000:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def walk_files(root, skip_top=()):
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                       and posix(rel_dir / d) not in skip_top]
        for name in filenames:
            p = Path(dirpath) / name
            if posix(p.relative_to(root)) in skip_top:
                continue
            yield p


def words(text, drop=frozenset()):
    """Word tokens for overlap checks. Bare numbers are dropped: dates and version numbers
    line up by coincidence and carry no expression, and they made data samples look copied."""
    return [w for w in WORD.findall(text.lower()) if not w.isdigit() and w not in drop]


def spec_identifiers(spec_text):
    """Short identifiers the frozen spec hands to the builders in code formatting (field names,
    option names, format tags). Data samples and code repeat them in the same order as the
    target's own samples do, which is structure, not copied expression, so the clean-side scan
    leaves them out of its overlap count. The spec lint never uses this: there, wrapping copied
    wording in code formatting must not hide it."""
    ident = set()
    for line in spec_text.splitlines():
        for span in re.findall(r"`([^`]+)`", line):
            tokens = WORD.findall(span.lower())
            if 1 <= len(tokens) <= 3:
                ident.update(tokens)
    return frozenset(ident)


def shingles(ws, n):
    return {" ".join(ws[i:i + n]) for i in range(len(ws) - n + 1)}


def overlaps(text, corpus, n, exempt=frozenset(), drop=frozenset()):
    """Maximal runs of at least n consecutive words that text shares with corpus."""
    ws = words(text, drop)
    hits, run_start = [], None
    for i in range(len(ws) - n + 1):
        s = " ".join(ws[i:i + n])
        if s in corpus and s not in exempt:
            if run_start is None:
                run_start = i
            run_end = i + n
        elif run_start is not None:
            hits.append(" ".join(ws[run_start:run_end]))
            run_start = None
    if run_start is not None:
        hits.append(" ".join(ws[run_start:run_end]))
    return hits


def mark_hits(text, marks, literals=()):
    """Lines that name the target. Approved interop literals are masked first."""
    for lit in literals:
        if lit:  # in any letter case: a test for "wrong case is refused" still spells the literal
            text = re.sub(re.escape(lit), "_" * len(lit), text, flags=re.I)
    found = []
    for mark in marks:
        if not mark.strip():
            continue
        pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(mark.strip()) + r"(?![A-Za-z0-9])", re.I)
        for lineno, line in enumerate(text.splitlines(), 1):
            if pat.search(line):
                found.append((mark, lineno))
    return found


def git(repo, *args, check=False):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=check)


def changed_paths(repo):
    out = git(repo, "status", "--porcelain", "--untracked-files=all").stdout
    paths = set()
    for line in out.splitlines():
        if len(line) > 3:
            p = line[3:].strip().strip('"')
            if " -> " in p:
                p = p.split(" -> ")[1]
            paths.add(p)
    return paths


def commit(repo, message):
    """Commit a step under the operator's own git identity. No trailers, ever."""
    if not (Path(repo) / ".git").exists():
        return
    git(repo, "add", "-A")
    if git(repo, "diff", "--cached", "--quiet").returncode == 0:
        return
    res = git(repo, "commit", "-m", message)
    if res.returncode != 0:
        die(f"could not commit in {repo}: {(res.stderr or res.stdout).strip()[:300]}\n"
            "Every step is committed so that the record stands on its own. Check that git has a user name "
            "and email, and, if commits are signed, that the signing key works without a prompt.")


# ------------------------------------------------------------------ workspace

class Ctx:
    def __init__(self, args):
        target = args.target
        if not SLUG.match(target):
            die("target must be a short lowercase slug, e.g. 'acme-notes'")
        self.target = target
        self.root = Path(args.root or os.environ.get("SPITE_ROOT") or os.getcwd()).resolve()
        self.dirty = self.root / f"spite-{target}-dirty"
        self.state = self.root / ".spite" / target
        self.args = args
        self._cfg = None

    @property
    def cfg(self):
        if self._cfg is None:
            path = self.dirty / "spite.json"
            if not path.exists():
                die(f"no workspace for '{self.target}' under {self.root}. Run: spite init {self.target}")
            self._cfg = load_json(path)
        return self._cfg

    @property
    def clean(self):
        if self.cfg.get("clean_path"):  # the clean repo was placed under a root of its own
            return Path(self.cfg["clean_path"])
        return self.root / self.cfg["clean_dir"]

    @property
    def marks(self):
        return self.cfg.get("marks", [])

    @property
    def literals(self):
        return self.cfg.get("interop_literals", [])

    def frozen_hash(self):
        rec = latest_record(self.dirty, "freeze")
        return rec["checks"]["sha256"] if rec else None


def records_of(repo, step=None):
    d = Path(repo) / "records"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            rec = load_json(p)
        except (OSError, ValueError):
            continue
        if step is None or rec.get("step") == step:
            out.append(rec)
    return out


def latest_record(repo, step):
    recs = records_of(repo, step)
    return recs[-1] if recs else None


def file_entries(repo, paths):
    out = []
    for p in sorted(set(paths)):
        full = Path(repo) / p
        if full.is_file():
            out.append({"path": posix(p), "sha256": sha256_file(full)})
    return out


STEP_NO = {"gate": "00", "terms": "01", "survey": "02", "survey-set-aside": "02", "partition": "03", "review": "04",
           "blind-test": "05", "freeze": "05", "config": "06", "scan-accept": "10", "update-start": "12", "scout": "00",
           "spec-received": "06", "build": "07", "test": "08", "qa": "09", "scan": "10", "emit": "11"}


def write_record(repo, step, side, inputs, outputs, checks, result, session=None, notes=None, other=None):
    """Write one gate record. Each record carries the hash of the record before it on its side,
    so a record cannot be changed, dropped or slipped in later without breaking the chain. Where
    the two sides meet (freeze, questions and answers, release) pass `other`, the other side's
    repo, and the record also pins the head of that side's chain."""
    link = sc.chain_fields(Path(repo) / "records", (Path(other) / "records") if other else None)
    rec = {
        "spite_record": 2,
        "seq": link["seq"],
        "prev_sha256": link["prev_sha256"],
        "step": step,
        "side": side,
        "date": now().isoformat(),
        "inputs": file_entries(repo, inputs),
        "outputs": file_entries(repo, outputs),
        "checks": checks,
        "result": result,
    }
    if other:
        rec["other_side_head_sha256"] = link.get("other_side_head_sha256")
    if session:
        rec["access"] = session
    if notes:
        rec["notes"] = notes
    base = Path(repo) / "records" / f"{STEP_NO[step]}-{step}-{stamp()}"
    path, n = base.with_suffix(".json"), 1
    while path.exists():  # two runs in one second must not overwrite each other
        n += 1
        path = Path(f"{base}-{n}.json")
    save_json(path, rec)
    print(f"  record: {posix(path.relative_to(Path(repo).parent))}  [{result}]")
    return rec


# ------------------------------------------------------------------- sessions

def load_agent(role):
    text = (AGENTS / f"spite-{role}.md").read_text(encoding="utf-8")
    if not text.startswith("---"):
        die(f"agent file for {role} has no frontmatter")
    _, front, body = text.split("---", 2)
    meta = {}
    for line in front.strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    meta["tools"] = [t.strip() for t in meta.get("tools", "").split(",") if t.strip()]
    return meta, body.strip() + "\n"


def claude_cmd():
    exe = shutil.which("claude")
    if not exe:
        die("the 'claude' command is not on PATH; the runner launches each role as a headless Claude Code session")
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe]
    return [exe]


def session_env(ctx, side):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("SPITE_"):
            del env[key]
    if side in ("clean", "blind"):
        needle = posix(ctx.dirty).lower()
        for key, value in list(env.items()):
            if needle in posix(value).lower():
                del env[key]
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
    return env


def session_model(ctx, side):
    """The model a side's sessions use: the command line wins, then the per-side setting. The
    blind reader uses the build side's model, because the question it answers is what the
    builders would recognise."""
    key = "dirty_model" if side == "dirty" else "clean_model"
    return ctx.args.model or ctx.cfg.get(key) or None


def note_model(ctx, side, model):
    """Keep, in the runner's private folder only, which models each side has used."""
    path = ctx.state / "models.json"
    data = load_json(path) if path.exists() else {}
    used = set(data.get(side, []))
    used.add(model or "default")
    data[side] = sorted(used)
    save_json(path, data)


def sides_used_different_models(ctx):
    """True only when both sides named their models and no model served both. Private."""
    path = ctx.state / "models.json"
    data = load_json(path) if path.exists() else {}
    dirty, clean = set(data.get("dirty", [])), set(data.get("clean", [])) | set(data.get("blind", []))
    if not dirty or not clean or "default" in dirty | clean:
        return None
    return not (dirty & clean)


def run_session(ctx, role, task, no_web=False, repo=None):
    """Launch one role as a fresh, restricted, headless session in its own repo."""
    spec = ROLES[role]
    side = spec["side"]
    repo = repo or (ctx.dirty if side == "dirty" else ctx.clean)
    meta, body = load_agent(role)
    tools = [t for t in meta["tools"] if not (no_web and t in ("WebFetch", "WebSearch"))]

    # The session's policy and settings files are named on its command line and show up in the
    # guard's refusal messages, so their path must not carry the target's name on the clean side.
    if side == "dirty":
        base = ctx.state
    elif side == "clean":
        base = ctx.root / ".spite" / f"clean-{repo.name}"
    else:
        base = ctx.root / ".spite" / "reader"
    run_dir = base / "sessions" / f"{stamp()}-{role}"
    run_dir.mkdir(parents=True, exist_ok=True)
    audit = run_dir / "audit.jsonl"
    policy = {
        "role": role, "root": posix(repo), "tools": tools,
        "read_dirs": spec["read_dirs"], "read_files": spec["read_files"],
        "write_dirs": spec["write_dirs"], "write_files": spec["write_files"],
        "write_deny_dirs": spec.get("write_deny_dirs", []),
        "deny_dirs": ALWAYS_DENY, "log": posix(audit),
    }
    if side == "clean":
        frozen = ctx.frozen_hash()
        if not frozen:
            die("no frozen spec on record; the clean side cannot start")
        policy["spec_lock"] = {"path": "spec/frozen-spec.md", "sha256": frozen}
    save_json(run_dir / "policy.json", policy)
    settings = {
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{
            "type": "command",
            "command": f'"{posix(sys.executable)}" -I "{posix(GUARD)}" "{posix(run_dir / "policy.json")}"',
        }]}]},
        "attribution": False,
        "autoMemoryEnabled": False,
        "disableClaudeAiConnectors": True,
    }
    save_json(run_dir / "settings.json", settings)
    (run_dir / "role.md").write_text(body, encoding="utf-8", newline="\n")
    (run_dir / "task.md").write_text(task, encoding="utf-8", newline="\n")

    cmd = claude_cmd() + [
        "-p", "--restricted", "--strict-mcp-config",
        "--tools", ",".join(tools),
        "--disallowedTools", "mcp__*",
        "--settings", str(run_dir / "settings.json"),
        "--append-system-prompt-file", str(run_dir / "role.md"),
        "--permission-prompts", "none",
        "--output-format", "json",
    ]
    model = session_model(ctx, side)
    if model:
        cmd += ["--model", model]
    note_model(ctx, side, model)
    if ctx.args.budget:
        cmd += ["--max-budget-usd", str(ctx.args.budget)]

    print(f"  session: {role} ({side} side) in {repo.name}; tools: {', '.join(tools)}")
    try:
        proc = subprocess.run(cmd, cwd=repo, input=task, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=session_env(ctx, side),
                              timeout=ctx.args.timeout)
    except subprocess.TimeoutExpired:
        die(f"the {role} session ran past {ctx.args.timeout}s and was stopped")
    (run_dir / "stdout.txt").write_text(proc.stdout or "", encoding="utf-8")
    (run_dir / "stderr.txt").write_text(proc.stderr or "", encoding="utf-8")

    result = {}
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith("{"):
            try:
                result = json.loads(line)
                break
            except ValueError:
                continue
    calls = refused = 0
    fetched = []
    if audit.exists():
        for line in audit.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            calls += 1
            refused += entry.get("decision") == "deny"
            if entry.get("tool") == "WebFetch" and entry.get("decision") == "allow":
                fetched.append(entry.get("target", ""))
    ok = proc.returncode == 0 and bool(result) and not result.get("is_error")
    if not ok:
        tail = (proc.stderr or proc.stdout or "")[-600:]
        print(f"  session failed (exit {proc.returncode}). Details in {posix(run_dir)}\n{tail}")
    else:
        print(f"  session done: {calls} tool calls, {refused} refused by the guard")
    return {
        "ok": ok, "run_dir": run_dir, "fetched": fetched, "final": result.get("result", ""),
        # What goes into the published record. It states what the role could reach and nothing
        # about how the work was carried out; the call counts and logs stay in the private folder.
        "public": {
            "role": role,
            "isolation": "separate working environment; no shared history with the other side",
            "web_access": any(t in tools for t in ("WebFetch", "WebSearch")),
            "command_execution": False,
            "could_read": spec["read_dirs"] + spec["read_files"],
            "could_write": spec["write_dirs"] + spec["write_files"],
        },
    }


def strays(repo, before, allowed_dirs, allowed_files=()):
    """Paths a session changed outside what its step is supposed to produce."""
    bad = []
    for p in sorted(changed_paths(repo) - before):
        if p in allowed_files or any(p == d or p.startswith(d.rstrip("/") + "/") for d in allowed_dirs):
            continue
        bad.append(p)
    return bad


# ----------------------------------------------------------------------- init

def install_agents(repo, roles):
    dest = Path(repo) / ".claude" / "agents"
    dest.mkdir(parents=True, exist_ok=True)
    for role in roles:
        shutil.copyfile(AGENTS / f"spite-{role}.md", dest / f"spite-{role}.md")


def cmd_init(ctx):
    a = ctx.args
    marks = [m for m in ([a.name or ctx.target] + (a.mark or [])) if m]
    # The clean repo never carries the target's name, not even in its folder name: builders who
    # can see the name can draw on whatever they already know about the product.
    named = [m.lower() for m in marks + [ctx.target]]

    def names_target(code):
        return any(m in code or code in m for m in named)

    if a.codename:
        codename = a.codename
        if not SLUG.match(codename):
            die("codename must be a short lowercase slug")
        if names_target(codename):
            die(f"codename '{codename}' overlaps the target's name or marks; pick a neutral one")
    else:
        free = [c for c in CODENAMES if not names_target(c) and not (ctx.root / f"spite-{c}").exists()]
        if not free:
            die("no built-in codename is free; pass --codename <neutral-name>")
        codename = random.choice(free)
    clean_name = f"spite-{codename}"
    clean_root = Path(a.clean_root).resolve() if a.clean_root else ctx.root
    dirty, clean = ctx.dirty, clean_root / clean_name
    if dirty.exists() or clean.exists():
        die(f"{dirty.name} or {clean.name} already exists under {ctx.root}")
    if clean_name == dirty.name:
        die("the clean repo cannot share the dirty repo's name")

    for d in ("survey/raw", "spec", "qa", "records"):
        (dirty / d).mkdir(parents=True)
    save_json(dirty / "spite.json", {
        "target": ctx.target, "target_name": a.name or ctx.target, "target_url": a.url or "",
        "marks": marks, "clean_dir": clean_name, "created": now().isoformat(),
        "clean_path": posix(clean) if a.clean_root else None,
        "dirty_model": a.dirty_model or None, "clean_model": a.clean_model or None,
        "allow_account_observation": False, "min_overlap_words": 8, "exec_prefix": [],
        "interop_literals": a.interop_literal or [],
    })
    with open(dirty / "survey" / "sources.csv", "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, lineterminator="\n").writerow(SOURCES_HEADER)
    (dirty / ".gitignore").write_text("__pycache__/\n", encoding="utf-8", newline="\n")
    install_agents(dirty, ["survey", "partition", "reviewer"])
    save_json(dirty / ".claude" / "settings.json", {"attribution": False})

    for d in ("spec", "tests", "qa/questions", "qa/answers", "records", "reports"):
        (clean / d).mkdir(parents=True)
    save_json(clean / "spite.json", {
        "test_cmd": a.test_cmd.split() if a.test_cmd else ["python" if os.name == "nt" else "python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
        "setup_cmd": [], "test_timeout": 900,
        "sandbox": {"kind": "docker", "image": "python:3.12-slim"} if shutil.which("docker") else {"kind": "none"},
    })
    (clean / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8", newline="\n")
    (clean / ".gitattributes").write_text("spec/frozen-spec.md -text\nprovenance/** -text\n", encoding="utf-8", newline="\n")
    install_agents(clean, ["builder", "tester", "emit"])
    hooks = clean / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copyfile(GUARD, hooks / "spite_guard.py")
    save_json(hooks / "policy.json", {
        "role": "clean-side", "root": ".",
        "tools": ["Read", "Write", "Edit", "Glob", "Grep", "Agent", "Task", "TodoWrite"],
        "read_dirs": ["."], "read_files": [], "write_dirs": ["."], "write_files": [],
        "write_deny_dirs": ["spec", "provenance", "LICENSE", "PROVENANCE.md"],
        "deny_dirs": ALWAYS_DENY,
        "spec_lock": {"path": "spec/frozen-spec.md", "sha256_file": "spec/frozen-spec.sha256"},
        "log": ".spite/audit-interactive.jsonl",
    })
    py = posix(sys.executable)
    save_json(clean / ".claude" / "settings.json", {
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{
            "type": "command",
            "command": f'"{py}" -I "$CLAUDE_PROJECT_DIR/.claude/hooks/spite_guard.py" "$CLAUDE_PROJECT_DIR/.claude/hooks/policy.json"',
        }]}]},
        "permissions": {"deny": ["WebFetch", "WebSearch", "Bash", "PowerShell"]},
        "attribution": False, "autoMemoryEnabled": False,
    })
    for repo in (dirty, clean):
        if shutil.which("git"):
            git(repo, "init", "-q", "-b", "main")
            # Local tooling folders are kept out of git through the untracked exclude file, not
            # .gitignore: a tracked ignore list would publish the names of the tools used.
            info = repo / ".git" / "info"
            info.mkdir(parents=True, exist_ok=True)
            with open(info / "exclude", "a", encoding="utf-8", newline="\n") as fh:
                fh.write(".claude/\n.spite/\n")
    install_dirty_prepush(dirty, [git(SKILL_DIR, "remote", "get-url", "origin").stdout.strip()])
    commit(dirty, "init: dirty-side workspace")
    commit(clean, "init: clean-side workspace")
    ctx.state.mkdir(parents=True, exist_ok=True)
    print(f"created {dirty}\ncreated {clean}")
    print(f"clean repo codename: {codename}  (neutral on purpose: the target's name stays off the clean side)")
    print("The dirty repo is the research: what was read, the drafts, the questions and answers. It is the\n"
          "evidence behind the release and where later updates start, so keep it as long as the product lives.\n"
          "It is local only for now, and a push guard lets it go to private repos only. Whether to back it up,\n"
          f"and where, is the operator's choice: spite remote {ctx.target}")
    print(f"next: spite gate {ctx.target} ...")


# ----------------------------------------------------------------------- gate

def cmd_gate(ctx):
    a = ctx.args
    answers = {
        "core_value_is_in_code": a.core_in_code,
        "buildable_in_weeks": a.weeks,
        "no_strong_oss_equivalent": a.no_strong_oss,
        "paying_users_named": a.paying_users,
    }
    missing = [k for k, v in answers.items() if v in (None, "")]
    qualifies = not missing and a.weeks < 4
    rec = write_record(ctx.dirty, "gate", "dirty", ["spite.json"], [], {
        "criteria": answers, "missing": missing, "under_four_weeks": (a.weeks or 99) < 4,
        "operator_prior_exposure": getattr(a, "prior_exposure", None) or "not stated",
    }, "pass" if qualifies else "fail",
        notes="The answers are the operator's own assessment, recorded as given. Prior exposure is "
              "what the operator says they have seen of the target beyond public material.")
    commit(ctx.dirty, "gate: target assessment")
    if not qualifies:
        die("target does not pass the gate (all four criteria must hold, and the build must be under four weeks)")
    if not getattr(a, "quiet_next", False):
        print(f"next: spite terms {ctx.target}   (the target's terms of use come before any survey)")
    return rec


def require_gate(ctx):
    rec = latest_record(ctx.dirty, "gate")
    if not rec or rec["result"] != "pass":
        die(f"no passing target gate on record. Run: spite gate {ctx.target} ...")


# --------------------------------------------------------------------- survey

def check_sources(ctx):
    path = ctx.dirty / "survey" / "sources.csv"
    problems, rows = [], []
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if [h.strip() for h in (reader.fieldnames or [])] != SOURCES_HEADER:
            return [f"sources.csv header must be exactly: {','.join(SOURCES_HEADER)}"], []
        for i, row in enumerate(reader, 2):
            rows.append(row)
            if not (row.get("url") or "").strip():
                problems.append(f"line {i}: no url")
            try:
                datetime.date.fromisoformat((row.get("accessed") or "").strip())
            except ValueError:
                problems.append(f"line {i}: 'accessed' is not a YYYY-MM-DD date")
            access = (row.get("access") or "").strip()
            if access not in ("public", "account"):
                problems.append(f"line {i}: 'access' must be public or account")
            if access == "account" and not ctx.cfg.get("allow_account_observation"):
                problems.append(f"line {i}: account-based observation is off for this target "
                                "(set allow_account_observation only after checking the target's terms)")
    if not rows:
        problems.append("sources.csv has no sources")
    return problems, rows


def cmd_terms(ctx):
    """Deal with the target's terms of use before any survey reads its pages."""
    require_gate(ctx)
    a = ctx.args
    cfg_path = ctx.dirty / "spite.json"
    if a.decision:
        if not a.note:
            die("--decision needs --note: who read the terms and what they concluded")
        cfg = load_json(cfg_path)
        cfg["survey_mode"], cfg["terms_note"] = a.decision, a.note
        save_json(cfg_path, cfg)
        ctx._cfg = None
        present = (ctx.dirty / "survey" / "terms.md").is_file()
        write_record(ctx.dirty, "terms", "dirty", ["survey/terms.md"] if present else [], ["spite.json"],
                     {"decision": a.decision, "note": a.note, "terms_note_on_file": present}, "pass",
                     notes="The decision is the operator's own, recorded as given.")
        commit(ctx.dirty, f"terms: operator decision ({a.decision})")
        nxt = {"live": f"spite survey {ctx.target}",
               "saved": f"spite survey {ctx.target} --from-dir <folder of saved public pages>",
               "stop": "(nothing: this target is stopped)"}[a.decision]
        print(f"survey mode for {ctx.target}: {a.decision}\nnext: {nxt}")
        return
    before = changed_paths(ctx.dirty)
    task = ("Find and read only the target's terms of use, plus any acceptable-use or API terms that page links "
            "to. The target is described in spite.json. Fetch at most four pages and nothing else: no product, "
            f"documentation or pricing pages. Today's date is {now().date()}. Write survey/terms.md with the "
            "address of each page and, for each topic below, the relevant clause quoted with its section "
            "number, or 'none found': (1) automated access, robots or scraping; (2) reverse engineering; "
            "(3) building or benchmarking competing products; (4) who the terms bind (any visitor, or only "
            "account holders) and how they define the site, service or platform they cover. Say plainly "
            "whether you read the full text or a summary of it. Add the pages to survey/sources.csv with "
            "kind 'terms'.")
    res = run_session(ctx, "survey", task)
    problems = []
    terms = ctx.dirty / "survey" / "terms.md"
    if not terms.is_file():
        problems.append("survey/terms.md was not written")
    stray = strays(ctx.dirty, before, ["survey"])
    if stray:
        problems.append(f"files changed outside survey/: {stray}")
    if len(res["fetched"]) > 4:
        problems.append(f"{len(res['fetched'])} pages were fetched; the terms step allows four")
    write_record(ctx.dirty, "terms", "dirty", ["spite.json"], ["survey/terms.md", "survey/sources.csv"],
                 {"pages_fetched": len(res["fetched"]), "problems": problems, "decision": None},
                 "pass" if res["ok"] and not problems else "fail", session=res["public"])
    commit(ctx.dirty, "terms: note on the target's terms of use")
    if problems or not res["ok"]:
        die("terms step did not pass:\n  " + "\n  ".join(problems or ["session failed"]))
    print(f"terms note: {posix(terms)}")
    print("This note is a pointer, not a substitute: a person should read the terms page itself.")
    print(f"then record the decision: spite terms {ctx.target} --decision live|saved|stop --note \"...\"")


def require_terms(ctx, offline):
    mode = ctx.cfg.get("survey_mode")
    if not mode:
        die("the target's terms of use have not been dealt with, so no survey may read its pages yet.\n"
            f"  spite terms {ctx.target}                      fetches only the terms pages and notes what they say\n"
            f"  spite terms {ctx.target} --decision live|saved|stop --note \"...\"   records the operator's decision\n"
            "  live  = the survey may read the public site directly\n"
            "  saved = the survey may only read copies a person saved (survey --from-dir)\n"
            "  stop  = do not survey this target")
    if mode == "stop":
        die("the operator's decision on this target's terms was to stop")
    if mode == "saved" and not offline:
        die("the operator's decision was saved material only; run: "
            f"spite survey {ctx.target} --from-dir <folder>")
    return mode


def cmd_survey(ctx):
    require_gate(ctx)
    a = ctx.args
    offline = bool(a.from_dir)
    if a.set_aside:
        return set_aside_survey(ctx)
    survey_mode = require_terms(ctx, offline)
    before = changed_paths(ctx.dirty)
    if offline:
        src = Path(a.from_dir).resolve()
        if not src.is_dir():
            die(f"--from-dir {src} is not a directory")
        for p in walk_files(src):
            dest = ctx.dirty / "survey" / "raw" / p.relative_to(src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, dest)
        task = (f"Survey the target using only the saved public material in survey/raw/. You have no web access "
                f"in this run. Today's date is {now().date()}. Log each file you use as a source in "
                f"survey/sources.csv (url = the page's original address if the file states one, otherwise "
                f"file:survey/raw/<name>). Write the observation log to survey/observations.md.")
    else:
        task = (f"Survey the target described in spite.json using public material only. Today's date is "
                f"{now().date()}. Log every source in survey/sources.csv and write the observation log to "
                f"survey/observations.md. The terms of use are already noted in survey/terms.md; do not "
                f"rewrite that file.")
    if update_cycle(ctx):
        task += (" This is an update survey. survey/baseline-observations.md is the log the current release was "
                 "built from; do not edit it. Bring survey/observations.md up to date with what the new material "
                 "shows, and write survey/changes.md: three lists, Added, Changed and Removed since the baseline, "
                 "each entry tagged with its source id.")
    task += (" If survey/observations.md or survey/sources.csv already hold earlier work, keep it and extend "
             "it: add new source rows with new ids, and fill the gaps the log lists.")
    res = run_session(ctx, "survey", task, no_web=offline)
    problems, rows = check_sources(ctx)
    obs = ctx.dirty / "survey" / "observations.md"
    if not obs.is_file() or len(obs.read_text(encoding="utf-8", errors="replace").split()) < 50:
        problems.append("survey/observations.md is missing or nearly empty")
    stray = strays(ctx.dirty, before, ["survey"])
    if stray:
        problems.append(f"files changed outside survey/: {stray}")
    logged = {(r.get("url") or "").strip() for r in rows}
    unlogged = sorted({u for u in res["fetched"] if u and u not in logged})
    outputs = [posix(p.relative_to(ctx.dirty)) for p in walk_files(ctx.dirty / "survey")]
    write_record(ctx.dirty, "survey", "dirty", ["spite.json"], outputs, {
        "sources": len(rows), "problems": problems, "pages_fetched_but_not_logged": unlogged,
        "mode": "saved material" if offline else "live web", "terms_decision": survey_mode,
    }, "pass" if res["ok"] and not problems else "fail", session=res["public"])
    commit(ctx.dirty, "survey: observation log and source list")
    if unlogged:
        print(f"  warning: {len(unlogged)} fetched page(s) are not in sources.csv; see the record")
    if problems or not res["ok"]:
        die("survey did not pass:\n  " + "\n  ".join(problems or ["session failed"]))
    print(f"next: spite partition {ctx.target}")


# ------------------------------------------------------------------ partition

def set_aside_survey(ctx):
    """Take an earlier survey out of use without erasing that it happened.

    The log, the source list and any saved pages move to set-aside/<time>/, which no role can
    read, so no spec is written from them. The earlier survey's record stays in the chain and a
    new record says what was set aside and why. The scan still treats the material as dirty."""
    a = ctx.args
    if not a.note:
        die("--set-aside needs --note: why the earlier survey is being taken out of use")
    survey = ctx.dirty / "survey"
    dest = ctx.dirty / "set-aside" / stamp()
    moved = []
    for name in ("observations.md", "sources.csv", "raw"):
        src = survey / name
        if src.exists() and (src.is_file() or any(src.iterdir())):
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest / name))
            moved.append(f"survey/{name}")
    if not moved:
        die("there is no survey material to set aside")
    (survey / "raw").mkdir(parents=True, exist_ok=True)
    with open(survey / "sources.csv", "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh, lineterminator="\n").writerow(SOURCES_HEADER)
    kept = [posix(p.relative_to(ctx.dirty)) for p in walk_files(dest)]
    write_record(ctx.dirty, "survey-set-aside", "dirty", [], kept,
                 {"set_aside": moved, "moved_to": posix(dest.relative_to(ctx.dirty)), "note": a.note}, "pass",
                 notes="The earlier survey stays on record. Its output was taken out of use and no spec was "
                       "written from it.")
    commit(ctx.dirty, "survey: earlier survey set aside")
    print(f"set aside: {', '.join(moved)} -> {posix(dest.relative_to(ctx.dirty))}")
    print(f"next: spite survey {ctx.target} ...")


def cmd_partition(ctx):
    surveys = sorted(records_of(ctx.dirty, "survey") + records_of(ctx.dirty, "survey-set-aside"),
                     key=lambda r: r["date"])
    rec = surveys[-1] if surveys else None
    if not rec or rec["step"] != "survey" or rec["result"] != "pass":
        die("no passing survey in use (none was run, the last one failed, or it was set aside)")
    before = changed_paths(ctx.dirty)
    task = ("Turn the observation log in survey/ into a functional specification, in your own words, and "
            "write it to spec/draft-spec.md. Follow the spec outline in your instructions.")
    if update_cycle(ctx):
        task = ("This is an update of a product that has already been released. spec/baseline-spec.md is the frozen "
                "spec that release was built from; do not edit it. survey/changes.md lists what the survey found "
                "added, changed and removed. Write spec/draft-spec.md as the complete new spec, in your own words, "
                "following the outline in your instructions. Keep every checklist item that still holds under its "
                "existing id and wording; give new requirements new ids after the highest existing id; never reuse "
                "or renumber an id; leave removed requirements out. Then write spec/draft-delta.md: three lists, "
                "Added, Changed and Removed, each entry a checklist id and one line saying what is different. The "
                "delta crosses the gate with the spec, so it follows the same rules as the spec.")
    res = run_session(ctx, "partition", task)
    problems = []
    draft = ctx.dirty / "spec" / "draft-spec.md"
    if not draft.is_file():
        problems.append("spec/draft-spec.md was not written")
    stray = strays(ctx.dirty, before, ["spec"])
    if stray:
        problems.append(f"files changed outside spec/: {stray}")
    inputs = [posix(p.relative_to(ctx.dirty)) for p in walk_files(ctx.dirty / "survey")]
    write_record(ctx.dirty, "partition", "dirty", inputs, ["spec/draft-spec.md"], {"problems": problems},
                 "pass" if res["ok"] and not problems else "fail", session=res["public"])
    commit(ctx.dirty, "partition: draft functional spec")
    if problems or not res["ok"]:
        die("partition did not pass:\n  " + "\n  ".join(problems or ["session failed"]))
    print(f"next: spite review {ctx.target}")


# --------------------------------------------------------------------- review

def dirty_corpus(ctx, n, include_spec_drafts=False, drop=frozenset()):
    corpus = set()
    paths = list(walk_files(ctx.dirty / "survey"))
    if (ctx.dirty / "set-aside").is_dir():
        paths += list(walk_files(ctx.dirty / "set-aside"))  # out of use, still dirty
    if include_spec_drafts:
        paths += [ctx.dirty / "spec" / "draft-spec.md", ctx.dirty / "spec" / "reviewer-notes.md"]
    for p in paths:
        if p.is_file():
            text = read_text(p)
            if text:
                corpus |= shingles(words(text, drop), n)
    return corpus


def source_urls(ctx):
    path = ctx.dirty / "survey" / "sources.csv"
    urls = set()
    if path.is_file():
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                u = (row.get("url") or "").strip()
                if u.startswith("http"):
                    urls.add(u)
                    host = re.sub(r"^https?://", "", u).split("/")[0].lower()
                    if host:
                        urls.add(host)
    return urls


def lint_crossing(ctx, text):
    """Checks on any text about to cross the gate. Returns (failures, warnings)."""
    n = int(ctx.cfg.get("min_overlap_words", 8))
    failures, warnings = [], []
    for mark, lineno in mark_hits(text, ctx.marks, ctx.literals):
        failures.append(f"line {lineno}: names the target or one of its marks ('{mark}')")
    for phrase in overlaps(text, dirty_corpus(ctx, n), n)[:25]:
        failures.append(f"copies {n}+ consecutive words from the survey material: \"{phrase[:140]}\"")
    low = text.lower()
    for url in sorted(source_urls(ctx)):
        if url.lower() in low:
            failures.append(f"contains a source address: {url}")
    for m in re.finditer(r'"([^"\s][^"\n]{38,}[^"\s])"', text):
        if len(m.group(1).split()) >= 6:
            warnings.append(f"long quoted string, check it is not interface copy: \"{m.group(1)[:100]}\"")
    # Interop literals are a person's decision. Anything the text lists under an "Interop literals"
    # heading must already be on the operator's approved list, and nothing may cross with a
    # decision still marked as open.
    in_section = False
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            in_section = "interop literal" in line.lower()
            continue
        if in_section:
            for literal in re.findall(r"`([^`]+)`", line):
                if literal not in ctx.literals:
                    failures.append(
                        f"lists the interop literal `{literal}`, which the operator has not approved. "
                        "Remove it and describe the thing by what it does, or have the operator add it "
                        "to interop_literals in spite.json")
    for m in re.finditer(r"(?i:\bneeds? (?:a )?(?:human|person'?s?|operator'?s?) decision\b)|\bTBD\b|\bTODO\b", text):
        failures.append(f"carries an open decision (\"{m.group(0)}\"); settle it on this side before the spec crosses")
    return failures, warnings


def spec_shape_failures(text):
    """What a spec must contain for the later gates to work: a numbered conformance checklist
    with core items marked (the tests and the ship gate are counted against it), and a statement
    about interchange formats even when none is published."""
    failures = []
    ids = sc.checklist_ids(text)
    if not ids:
        failures.append("has no conformance checklist items; each must start with an id in the form C1, C2, ...")
    elif not any(ids.values()):
        failures.append("marks no checklist item as (core); the ship gate needs to know which ones are")
    if not re.search(r"(?im)^#+ .*interchange", text):
        failures.append("has no Interchange section; it must state the published import and export formats, "
                        "or say that none is published")
    return failures


def cmd_review(ctx):
    draft = ctx.dirty / "spec" / "draft-spec.md"
    if not draft.is_file():
        die("no draft spec; run partition first")
    draft_hash = sha256_file(draft)
    before = changed_paths(ctx.dirty)
    task = ("Review spec/draft-spec.md. Remove every piece of expressive material and every name or mark of "
            "the target, keep every functional requirement, and write the result to spec/reviewed-spec.md. "
            "Write what you removed and why to spec/reviewer-notes.md. Do not edit the draft.")
    lint_file = ctx.dirty / "spec" / "lint-report.txt"
    if (ctx.dirty / "spec" / "reviewed-spec.md").is_file() and lint_file.is_file()             and "(none)" not in lint_file.read_text(encoding="utf-8").split("WARNINGS")[0]:
        task = ("An earlier review of this spec failed the automated crossing check. Read spec/lint-report.txt "
                "and spec/reviewed-spec.md, fix every listed failure in spec/reviewed-spec.md without losing "
                "any functional requirement, and append what you changed to spec/reviewer-notes.md. "
                "Do not edit the draft.")
    if update_cycle(ctx):
        task += (" This is an update: also turn spec/draft-delta.md into spec/reviewed-delta.md under the same "
                 "rules, keeping its three lists and its checklist ids.")
    if ctx.args.note:
        task += " Decision from the operator, which you must apply: " + ctx.args.note
    if ctx.args.skip_audit and not ctx.args.note:
        die("--skip-audit needs --note: the operator's reason for going without the second reader")
    if ctx.args.lint_only:
        res = {"ok": True, "public": None}
    else:
        res = run_session(ctx, "reviewer", task)
    problems = []
    reviewed = ctx.dirty / "spec" / "reviewed-spec.md"
    if sha256_file(draft) != draft_hash:
        problems.append("the reviewer changed the draft; it must stay as written")
    failures, warnings = [], []
    second_reader = "not run"
    if not reviewed.is_file():
        problems.append("spec/reviewed-spec.md was not written")
    else:
        spec_text = reviewed.read_text(encoding="utf-8", errors="replace")
        failures, warnings = lint_crossing(ctx, spec_text)
        failures += spec_shape_failures(spec_text)
        if update_cycle(ctx):
            delta = ctx.dirty / "spec" / "reviewed-delta.md"
            if not delta.is_file():
                problems.append("this is an update, and spec/reviewed-delta.md was not written")
            else:
                dfail, dwarn = lint_crossing(ctx, delta.read_text(encoding="utf-8", errors="replace"))
                failures += [f"in the delta: {x}" for x in dfail]
                warnings += dwarn
            old_ids = set(sc.checklist_ids(read_text(ctx.dirty / "spec" / "baseline-spec.md") or ""))
            new_ids = sc.checklist_ids(spec_text)
            reused = sorted(i for i in new_ids if i not in old_ids
                            and int(i[1:]) <= max((int(o[1:]) for o in old_ids), default=0))
            if reused:
                failures.append("new checklist items reuse numbers from the released spec (" + ", ".join(reused) +
                                "); new requirements take ids after the highest existing one")
        # A second, adversarial reader whose only job is to find what the reviewer left in.
        audit_file = ctx.dirty / "spec" / "audit-findings.md"
        if ctx.args.lint_only:
            second_reader = "not rerun (crossing checks only)"
        elif ctx.args.skip_audit:
            second_reader = "skipped by the operator"
        elif res["ok"]:
            if audit_file.exists():
                audit_file.unlink()
            ares = run_session(ctx, "auditor",
                               "Audit spec/reviewed-spec.md against the material in survey/, as your instructions "
                               "describe, and write your findings to spec/audit-findings.md.")
            found = []
            if audit_file.is_file():
                for line in audit_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    m = re.match(r"^\W*FINDING:\s*(.+)$", line.strip())
                    if m:
                        found.append(m.group(1).strip())
            elif ares["ok"]:
                problems.append("the second reader wrote no findings file")
            if not ares["ok"]:
                problems.append("the second reader's session failed")
            failures += [f"second reader: {f}" for f in found]
            second_reader = f"ran: {len(found)} finding(s)"
    stray = strays(ctx.dirty, before, ["spec"])
    if stray:
        problems.append(f"files changed outside spec/: {stray}")
    lint_path = ctx.dirty / "spec" / "lint-report.txt"
    lint_path.write_text("\n".join(
        ["FAILURES"] + (failures or ["(none)"]) + ["", "WARNINGS"] + (warnings or ["(none)"])) + "\n",
        encoding="utf-8", newline="\n")
    passed = res["ok"] and not problems and not failures
    write_record(ctx.dirty, "review", "dirty", ["spec/draft-spec.md"],
                 ["spec/reviewed-spec.md", "spec/reviewer-notes.md", "spec/lint-report.txt",
                  "spec/audit-findings.md"],
                 {"problems": problems, "lint_failures": len(failures), "lint_warnings": len(warnings),
                  "second_reader": second_reader, "operator_decision": ctx.args.note or None},
                 "pass" if passed else "fail", session=res["public"])
    commit(ctx.dirty, "review: reviewed spec and reviewer notes")
    for line in failures[:10]:
        print(f"  lint: {line}")
    for line in warnings[:5]:
        print(f"  lint warning: {line}")
    if not passed:
        rounds = 0
        for r in reversed(sorted(records_of(ctx.dirty, "review") + records_of(ctx.dirty, "partition"),
                                 key=lambda r: r["date"])):
            if r["step"] != "review" or r["result"] == "pass":
                break
            rounds += 1
        if rounds >= 5:
            print(f"  {rounds} review rounds in a row have not passed. That usually means the readers are arguing "
                  "over something a person should settle. Read spec/lint-report.txt and decide: pass a ruling with "
                  "--note, approve a literal, or go without the second reader with --skip-audit --note.")
        die(f"review did not pass. Rerun: spite review {ctx.target}  (the reviewer is handed the lint report; "
            "add --note \"...\" to pass a decision of the operator's). If you edited "
            "spec/reviewed-spec.md by hand on the dirty side, recheck it with --lint-only.")
    print(f"reviewed spec is ready for a human read: {posix(reviewed)}")
    print(f"next: spite freeze {ctx.target}")


# --------------------------------------------------------------------- freeze

def require_untouched_clean(ctx):
    """The clean repo changes only through the runner, which commits after every step. Anything
    uncommitted there was put in by something else (most likely the session driving the runner,
    which has seen the dirty side), so every clean-side command stops when it finds any."""
    if not (ctx.clean / ".git").exists():
        return
    stray = sorted(changed_paths(ctx.clean))
    if stray:
        die("the clean repo has changes the runner did not make:\n  " + "\n  ".join(stray[:20]) +
            "\nNothing may be written there by hand or by the session driving the runner. Remove them "
            "(git -C <clean repo> stash, or delete the files) and tell the operator what they were.")


def blind_identification(ctx, text):
    """Ask a reader who has seen nothing but the spec which product it describes.

    This is the one check aimed at what the builders may already know: if a reader can name the
    target from the spec alone, so can they. The reader works in an empty folder under a neutral
    path, with the build side's model."""
    import tempfile
    folder = Path(tempfile.mkdtemp(prefix="reader-"))
    (folder / "spec.md").write_bytes(text.encode("utf-8"))
    try:
        res = run_session(ctx, "identify", "Read spec.md and answer exactly as your instructions describe.",
                          repo=folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    guesses = []
    for line in (res["final"] or "").splitlines():
        m = re.match(r"^\W*GUESS:\s*(.*?)\s*\|\s*(\d+)\s*\|\s*(.*)$", line.strip())
        if m:
            guesses.append({"name": m.group(1), "confidence": int(m.group(2)), "why": m.group(3).strip()})
    if not res["ok"] or not guesses:
        return {"ran": False, "identified": None, "guesses": [], "reasons": []}
    named = [m.lower() for m in ctx.marks + [ctx.cfg.get("target_name", ""), ctx.target] if m]
    hits = [g for g in guesses if any(n in g["name"].lower() or g["name"].lower() in n
                                      for n in named if g["name"].lower() != "unknown")]
    return {"ran": True, "identified": bool(hits), "guesses": len(guesses),
            "top_confidence": max((g["confidence"] for g in hits), default=0),
            "reasons": [f"{g['confidence']}% sure: {g['why']}" for g in hits]}


def keep_frozen_spec(ctx, data):
    """File a frozen spec under its own hash on the dirty side. Specs are replaced by updates;
    the records of earlier releases still name the old ones, so they are kept to be checked against."""
    history = ctx.dirty / "spec" / "frozen-history"
    history.mkdir(parents=True, exist_ok=True)
    (history / f"{hashlib.sha256(data).hexdigest()}.md").write_bytes(data)


def cmd_freeze(ctx):
    rec = latest_record(ctx.dirty, "review")
    if not rec or rec["result"] != "pass":
        die("no passing review on record")
    require_untouched_clean(ctx)
    a = ctx.args
    if (a.accept_identifiable or a.no_blind_test) and not a.note:
        die("--accept-identifiable and --no-blind-test need --note: the operator's reason")
    reviewed = ctx.dirty / "spec" / "reviewed-spec.md"
    text = reviewed.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    failures, _ = lint_crossing(ctx, text)
    failures += spec_shape_failures(text)
    if failures:
        die("the reviewed spec no longer passes the crossing checks; rerun review")
    second = (rec.get("checks") or {}).get("second_reader", "not run")
    if not second.startswith("ran") and not second.startswith("skipped"):
        print(f"  note: the second reader has not read this version of the spec ({second})")
    last_blind = latest_record(ctx.dirty, "blind-test")
    # Order on one side is the chain's sequence number: dates only resolve to the second.
    if last_blind and last_blind["result"] == "fail" \
            and (last_blind.get("seq", 0), last_blind["date"]) > (rec.get("seq", 0), rec["date"]) \
            and not a.accept_identifiable:
        die("a blind reader named the target from this version of the spec. The answer varies from run to "
            f"run, so asking again until it passes proves nothing. Rerun: spite review {ctx.target}")
    blind = {"ran": False} if a.no_blind_test else blind_identification(ctx, text)
    if not blind["ran"]:
        if not (a.accept_identifiable or a.no_blind_test):
            die("the blind reader gave no usable answer. Run freeze again, or pass "
                "--accept-identifiable --note \"...\" to freeze without that check.")
        blind_summary = {"ran": False, "operator_override": a.note}
    elif blind["identified"] and not a.accept_identifiable:
        lint_path = ctx.dirty / "spec" / "lint-report.txt"
        lines = ["FAILURES"] + [f"a reader given only this spec named the target ({r})" for r in blind["reasons"]]
        lint_path.write_text("\n".join(lines + ["", "WARNINGS", "(none)"]) + "\n", encoding="utf-8", newline="\n")
        write_record(ctx.dirty, "blind-test", "dirty", ["spec/reviewed-spec.md"], ["spec/lint-report.txt"],
                     {"identified": True, "top_confidence": blind["top_confidence"]}, "fail",
                     session=None)
        commit(ctx.dirty, "blind test: the spec gives the target away")
        for r in blind["reasons"]:
            print(f"  blind reader: {r}")
        die("a reader who saw only the spec named the target, so the builders could too. Rerun: "
            f"spite review {ctx.target}  (the reviewer is handed the reasons). If the identifying parts are "
            "needed for interoperability, the operator can freeze anyway with "
            "--accept-identifiable --note \"...\"; that choice is recorded.")
    else:
        blind_summary = {"ran": True, "identified": blind["identified"],
                         "top_confidence": blind.get("top_confidence", 0)}
        if blind["identified"]:
            blind_summary["operator_override"] = a.note
            print("  blind reader named the target; frozen anyway on the operator's recorded decision")
        else:
            print("  blind reader could not name the target from the spec")
    prior = ctx.frozen_hash()
    new_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cycle = update_cycle(ctx)
    delta_text = None
    if cycle:
        delta_file = ctx.dirty / "spec" / "reviewed-delta.md"
        if not delta_file.is_file():
            die("this is an update, and there is no reviewed delta; rerun review")
        delta_text = delta_file.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
        if lint_crossing(ctx, delta_text)[0]:
            die("the reviewed delta no longer passes the crossing checks; rerun review")
    if prior and prior != new_hash and not ctx.args.refreeze and not cycle:
        die("a different spec is already frozen. Refreezing invalidates any build made from the old one; "
            "pass --refreeze if that is intended")
    for repo in (ctx.dirty, ctx.clean):
        (repo / "spec").mkdir(exist_ok=True)
        (repo / "spec" / "frozen-spec.md").write_bytes(text.encode("utf-8"))
        (repo / "spec" / "frozen-spec.sha256").write_text(f"{new_hash}  frozen-spec.md\n", encoding="utf-8", newline="\n")
        if delta_text is not None:  # what changed since the last release, for the builders
            (repo / "spec" / "frozen-delta.md").write_bytes(delta_text.encode("utf-8"))
    keep_frozen_spec(ctx, text.encode("utf-8"))
    write_record(ctx.dirty, "freeze", "dirty", ["spec/reviewed-spec.md"],
                 ["spec/frozen-spec.md", "spec/frozen-spec.sha256"],
                 {"sha256": new_hash, "supersedes": prior if prior and prior != new_hash else None,
                  "update_number": cycle["number"] if cycle else None,
                  "delta_sha256": hashlib.sha256(delta_text.encode("utf-8")).hexdigest() if delta_text is not None else None,
                  "interop_literals_approved": ctx.literals, "blind_identification": blind_summary,
                  "crossed_the_gate": ["spec/frozen-spec.md", "spec/frozen-spec.sha256"]}, "pass",
                 other=ctx.clean)
    write_record(ctx.clean, "spec-received", "clean", [], ["spec/frozen-spec.md", "spec/frozen-spec.sha256"],
                 {"sha256": new_hash}, "pass",
                 notes="The frozen spec is the only material that entered this repo from outside.",
                 other=ctx.dirty)
    commit(ctx.dirty, f"freeze: spec frozen at {new_hash[:12]}")
    commit(ctx.clean, f"spec: frozen spec {new_hash[:12]}")
    print(f"frozen: sha256 {new_hash}")
    print(f"next: spite build {ctx.target}")


def verify_spec(ctx):
    """The check that refuses to start clean-side work unless the spec is the frozen one."""
    frozen = ctx.frozen_hash()
    if not frozen:
        die("no frozen spec on record; run freeze first")
    spec = ctx.clean / "spec" / "frozen-spec.md"
    sha = ctx.clean / "spec" / "frozen-spec.sha256"
    if not spec.is_file() or not sha.is_file():
        die("the clean repo has no frozen spec")
    actual = sha256_file(spec)
    stated = sha.read_text(encoding="utf-8").split()[0].strip().lower()
    if not (actual == stated == frozen):
        die(f"spec hash mismatch; refusing to continue.\n  frozen on record: {frozen}\n"
            f"  stated in clean repo: {stated}\n  actual file:      {actual}")
    return frozen


# ---------------------------------------------------------------------- build

def cmd_build(ctx):
    frozen = verify_spec(ctx)
    require_untouched_clean(ctx)
    before = changed_paths(ctx.clean)
    cfg = load_json(ctx.clean / "spite.json")
    test_cmd = " ".join(cfg["test_cmd"])
    if ctx.args.fix:
        task = (f"Read reports/test-report.txt. If the conformance run failed, find the cause in your implementation "
                f"and fix it so the product meets spec/frozen-spec.md. The tests are run with: {test_cmd}. "
                f"You cannot edit the tests or the spec. If a test contradicts the spec, say so in "
                f"qa/questions/ instead of bending the product to it.")
        todo = scan_fix_list(ctx, in_tests=False)
        if todo:
            task += (" Separately, a check on this repo requires these changes to your own files. Make each one, "
                     "choosing fresh wording or a fresh name of your own, and keep INTERFACE.md and the code "
                     "consistent afterwards. Do not ask why: " + " ; ".join(todo))
    else:
        task = (f"Build the product described in spec/frozen-spec.md in this repo. The spec is your only source. "
                f"When you are done, write INTERFACE.md saying exactly how the product is started and called. "
                f"Conformance tests will later be run from the repo root with: {test_cmd}")
        cycle = update_cycle(ctx)
        last_build = latest_record(ctx.clean, "build")
        last_freeze = latest_record(ctx.dirty, "freeze")
        if cycle and last_build and last_freeze and last_build["date"] < last_freeze["date"]:
            task = ("The frozen spec has been replaced by a new version, and this repo holds the product built from "
                    "the old one. spec/frozen-delta.md lists the checklist items that were added, changed and "
                    "removed. Bring the implementation and INTERFACE.md into line with the new spec/frozen-spec.md. "
                    "Change what the delta requires and leave the rest as it is. The spec and the delta are your "
                    f"only sources. Conformance tests will be run from the repo root with: {test_cmd}")
    res = run_session(ctx, "builder", task)
    problems = []
    if sha256_file(ctx.clean / "spec" / "frozen-spec.md") != frozen:
        problems.append("the frozen spec changed during the build")
    if not (ctx.clean / "INTERFACE.md").is_file():
        problems.append("INTERFACE.md was not written")
    changed = sorted(changed_paths(ctx.clean) - before)
    stray = [p for p in changed if p.startswith(("spec/", "tests/", "records/", "provenance/"))]
    if stray:
        problems.append(f"the builder changed files it must not touch: {stray}")
    write_record(ctx.clean, "build", "clean", ["spec/frozen-spec.md"], changed,
                 {"spec_sha256": frozen, "mode": "fix" if ctx.args.fix else "build", "problems": problems},
                 "pass" if res["ok"] and not problems else "fail", session=res["public"])
    commit(ctx.clean, f"build: {'fixes' if ctx.args.fix else 'implementation'} from frozen spec {frozen[:12]}")
    if problems or not res["ok"]:
        die("build did not pass:\n  " + "\n  ".join(problems or ["session failed"]))
    pending = pending_questions(ctx)
    if pending:
        print(f"  the builder left {len(pending)} question(s); answer them with: spite qa {ctx.target}")
    print(f"next: spite test {ctx.target}")


# ----------------------------------------------------------------------- scan

def scan(ctx):
    """Provenance scan: is anything from the dirty side present in the clean repo?"""
    n = int(ctx.cfg.get("min_overlap_words", 8))
    frozen_text = read_text(ctx.clean / "spec" / "frozen-spec.md") or ""
    drop = spec_identifiers(frozen_text)
    corpus = dirty_corpus(ctx, n, include_spec_drafts=True, drop=drop)
    exempt = set()
    allowed_hashes = set()
    crossings = [ctx.clean / "spec" / "frozen-spec.md", ctx.clean / "spec" / "frozen-delta.md"] \
        + sorted((ctx.clean / "qa" / "answers").glob("*"))
    history = ctx.dirty / "spec" / "frozen-history"
    earlier_specs = sorted(history.glob("*.md")) if history.is_dir() else []
    for p in crossings + earlier_specs:
        if p.is_file():
            allowed_hashes.add(sha256_file(p))
            text = read_text(p)
            if text:
                exempt |= shingles(words(text, drop), n)
    dirty_hashes = {}
    for p in walk_files(ctx.dirty):
        rel_dirty = posix(p.relative_to(ctx.dirty))
        if rel_dirty.startswith("qa/question-"):
            continue  # the builders' own questions, copied over for the reviewer: clean in origin
        if p.stat().st_size > 0:
            dirty_hashes[sha256_file(p)] = rel_dirty
    urls = source_urls(ctx)
    skip = {"provenance", "LICENSE", "records", "spec/frozen-spec.md", "spec/frozen-spec.sha256",
            "spec/frozen-delta.md"}
    findings = []
    clean_texts = []
    scanned = 0
    for p in walk_files(ctx.clean, skip_top=skip):
        rel = posix(p.relative_to(ctx.clean))
        if rel.startswith("qa/answers/"):
            continue
        scanned += 1
        for mark, _ in mark_hits(rel, ctx.marks, ctx.literals):
            findings.append({"file": rel, "kind": "mark-in-filename", "detail": mark})
        if p.stat().st_size > 0:
            h = sha256_file(p)
            if h in dirty_hashes and h not in allowed_hashes:
                findings.append({"file": rel, "kind": "identical-to-dirty-file", "detail": dirty_hashes[h]})
        text = read_text(p)
        if text is None:
            continue
        for mark, lineno in mark_hits(text, ctx.marks, ctx.literals):
            findings.append({"file": rel, "kind": "mark", "detail": f"line {lineno}: {mark}"})
        low = text.lower()
        for url in urls:
            if url.lower() in low:
                findings.append({"file": rel, "kind": "source-address", "detail": url})
        for phrase in overlaps(text, corpus, n, exempt, drop)[:10]:
            findings.append({"file": rel, "kind": "copied-text", "detail": phrase[:160]})
        clean_texts.append((rel, text))
    # Structure, not just wording: identifiers (routes, field names, flags, config keys) and short
    # quoted wording that exist in the survey material but never crossed the gate. If the clean
    # repo has them anyway, they came from somewhere other than the frozen spec.
    dirty_texts = []
    dirty_paths = list(walk_files(ctx.dirty / "survey"))
    if (ctx.dirty / "set-aside").is_dir():
        dirty_paths += list(walk_files(ctx.dirty / "set-aside"))
    dirty_paths += [ctx.dirty / "spec" / "draft-spec.md", ctx.dirty / "spec" / "reviewer-notes.md",
                    ctx.dirty / "spec" / "audit-findings.md"]
    for p in dirty_paths:
        if p.is_file() and p.suffix.lower() not in (".csv",):
            t = read_text(p)
            if t:
                dirty_texts.append(t)
    allowed = frozen_text + "\n" + "\n".join(ctx.literals) + "\n" \
        + (read_text(ctx.clean / "spec" / "frozen-delta.md") or "") \
        + "".join("\n" + (read_text(p) or "") for p in earlier_specs)
    for p in sorted((ctx.clean / "qa" / "answers").glob("*")) + sorted((ctx.clean / "qa" / "questions").glob("*")):
        allowed += "\n" + (read_text(p) or "")
    findings += sc.structural_findings(clean_texts, sc.survey_identifiers(dirty_texts),
                                       sc.survey_strings(dirty_texts), allowed)
    return {"files_scanned": scanned, "findings": findings, "min_overlap_words": n}


ADVISORY_KINDS = {"generic-name"}  # reported, never blocking: e.g. a one-word flag such as --date


def run_scan(ctx, quiet=False):
    result = scan(ctx)
    accepted_rules = ctx.cfg.get("accepted_findings", [])
    blocking, advisory, accepted = [], [], []
    for f in result["findings"]:
        rule = next((r for r in accepted_rules if r["kind"] == f["kind"]
                     and r["match"].lower() in f["detail"].lower()), None)
        if f["kind"] in ADVISORY_KINDS:
            advisory.append(f)
        elif rule:
            accepted.append(f)
        else:
            blocking.append(f)
    result.update({"blocking": blocking, "advisory": advisory, "accepted": accepted})
    report = ctx.state / "scans" / f"scan-{stamp()}.json"
    save_json(report, result)  # details stay off the clean side: they quote dirty material
    save_json(ctx.state / "scans" / "latest.json", result)
    by_kind = {}
    for f in blocking:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
    if not quiet:
        if blocking:
            print(f"  provenance scan: {len(blocking)} finding(s) in {result['files_scanned']} files")
            for f in blocking[:12]:
                print(f"    {f['file']}: {f['kind']}")
            print(f"    details (dirty side only): {posix(report)}")
        else:
            print(f"  provenance scan: clean ({result['files_scanned']} files)")
        if advisory:
            names = sorted({f["detail"] for f in advisory})
            print(f"    for information, not blocking: {len(names)} common name(s) also present on the dirty side")
        if accepted:
            print(f"    {len(accepted)} finding(s) accepted by the operator")
    return {"files_scanned": result["files_scanned"], "findings": len(blocking), "by_kind": by_kind,
            "accepted_by_operator": len(accepted), "advisory": len(advisory)}


def scan_fix_list(ctx, in_tests):
    """What a build-side role must change to clear the last scan, in terms of its own files only.

    Each entry names a clean-side file and a string that is already in it. Nothing else from the
    dirty side is passed, and no reason is given beyond "change this"."""
    path = ctx.state / "scans" / "latest.json"
    if not path.exists():
        return []
    verbs = {"copied-text": "reword the passage that reads like", "mark": "remove the name at",
             "mark-in-filename": "rename the file; its name contains", "source-address": "remove the address",
             "survey-identifier": "rename the identifier", "survey-wording": "reword the text",
             "identical-to-dirty-file": "replace this whole file; it must be written afresh"}
    out = []
    for f in load_json(path).get("blocking", []):
        if f["file"].startswith("tests/") != in_tests:
            continue
        out.append(f["file"] + ": " + verbs.get(f["kind"], "change") + " '" + f["detail"] + "'")
    return sorted(set(out))


def cmd_scan(ctx):
    verify_spec(ctx)
    a = ctx.args
    if a.accept:
        # A finding can be a coincidence: two people naming an option --date. Only the operator
        # may say so, per finding, with a reason, and the reason is recorded.
        if not a.match or not a.note:
            die("--accept needs --match (text from the finding's detail) and --note (why this is not a leak)")
        cfg = load_json(ctx.dirty / "spite.json")
        cfg.setdefault("accepted_findings", []).append(
            {"kind": a.accept, "match": a.match, "note": a.note, "date": now().isoformat()})
        save_json(ctx.dirty / "spite.json", cfg)
        ctx._cfg = None
        write_record(ctx.dirty, "scan-accept", "dirty", [], ["spite.json"],
                     {"kind": a.accept, "note": a.note}, "pass",
                     notes="The operator judged a scan finding not to be a leak. The decision is theirs, recorded as given.")
        commit(ctx.dirty, "scan: operator accepted a finding")
    require_untouched_clean(ctx)
    summary = run_scan(ctx)
    write_record(ctx.clean, "scan", "clean", ["spec/frozen-spec.md"], [], {"provenance_scan": summary},
                 "pass" if summary["findings"] == 0 else "fail")
    commit(ctx.clean, "scan: provenance scan")
    if summary["findings"]:
        sys.exit(1)


# ----------------------------------------------------------------------- test

def scrubbed_env(ctx):
    env = session_env(ctx, "clean")
    return env


def test_environment(ctx, cfg, unsandboxed):
    """How code written on the build side gets executed, and a plain description for the record.

    That code runs with whatever access its environment gives it. Run directly, it could read
    the dirty repo and print it into the test report, which the builder reads in a fix round:
    a way round the wall. So the default is a container that has only the clean repo, read-only,
    and no network, and running without one takes an explicit, recorded choice."""
    sb = cfg.get("sandbox") or {}
    kind = sb.get("kind", "none")
    legacy_prefix = list(ctx.cfg.get("exec_prefix") or [])
    if unsandboxed:
        return {"kind": "none", "sandboxed": False,
                "description": "no sandbox: run directly on the operator's machine, at the operator's explicit choice"}
    if kind == "docker":
        if not shutil.which("docker"):
            die("the clean repo is set to run tests in a container, but the docker command was not found")
        return {"kind": "docker", "sandboxed": True, "image": sb.get("image", "python:3.12-slim"),
                "env": sb.get("env", {}),
                "description": "an isolated container holding only this repository, read-only, with no network"}
    if kind == "custom" or legacy_prefix:
        return {"kind": "custom", "sandboxed": True, "prefix": list(sb.get("prefix") or legacy_prefix),
                "description": sb.get("description", "a sandbox set up by the operator")}
    die("this would run build-side code directly on your machine, where it could read the dirty repo.\n"
        "Set a sandbox for the clean repo first:\n"
        f"  spite config {ctx.target} --sandbox docker [--image <image>]\n"
        "or accept the risk for this run, which is written into the record and the provenance file:\n"
        f"  spite test {ctx.target} --unsandboxed")


def sandboxed_argv(ctx, env_spec, cmdline, phase):
    if env_spec["kind"] == "docker":
        argv = ["docker", "run", "--rm", "-w", "/work", "--tmpfs", "/tmp:exec",
                "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1"]
        for key, value in (env_spec.get("env") or {}).items():
            argv += ["-e", f"{key}={value}"]
        deps = f"spite-deps-{ctx.clean.name}"
        argv += ["-v", f"{posix(ctx.clean)}:/work:ro"]
        if phase == "setup":  # dependencies may need the network; they go to a volume, not the repo
            argv += ["-v", f"{deps}:/deps"]
        else:
            argv += ["--network", "none", "-v", f"{deps}:/deps:ro"]
        return argv + [env_spec["image"]] + list(cmdline)
    if env_spec["kind"] == "custom":
        return env_spec["prefix"] + list(cmdline)
    return list(cmdline)


def cmd_test(ctx):
    frozen = verify_spec(ctx)
    require_untouched_clean(ctx)
    a = ctx.args
    cfg = load_json(ctx.clean / "spite.json")
    env_spec = test_environment(ctx, cfg, a.unsandboxed)
    tests_dir = ctx.clean / "tests"
    have_tests = any(p.is_file() for p in tests_dir.rglob("*"))
    session = None
    problems = []
    cycle = update_cycle(ctx)
    last_test, last_freeze = latest_record(ctx.clean, "test"), latest_record(ctx.dirty, "freeze")
    tests_predate_spec = bool(cycle and have_tests and last_freeze
                              and (not last_test or last_test["date"] < last_freeze["date"]))
    if a.write_tests or a.fix_tests or not have_tests or tests_predate_spec:
        before = changed_paths(ctx.clean)
        task = ("Write black-box conformance tests for the product described in spec/frozen-spec.md. Take every "
                "expected behaviour from the spec, never from the implementation. INTERFACE.md says how the "
                f"product is started and called. Put the tests in tests/. They will be run from the repo root "
                f"with: {' '.join(cfg['test_cmd'])}")
        gaps = sc.coverage(read_text(ctx.clean / "spec" / "frozen-spec.md") or "", tests_dir)["core_without_a_test"]
        if have_tests and gaps and not a.fix_tests:
            task = ("The conformance tests in tests/ do not yet cover these core checklist items of "
                    f"spec/frozen-spec.md: {', '.join(gaps)}. Add at least one black-box test for each, with the "
                    "item's id in the test's name, and keep the existing tests as they are. Take every expected "
                    "behaviour from the spec. INTERFACE.md says how the product is started and called. The tests "
                    f"are run from the repo root with: {' '.join(cfg['test_cmd'])}")
        if tests_predate_spec and not a.fix_tests:
            task = ("The frozen spec has been replaced by a new version, and the tests in tests/ were written for "
                    "the old one. spec/frozen-delta.md lists the checklist items that were added, changed and "
                    "removed. Add tests for the added items, correct the tests of the changed items, and delete the "
                    "tests of the removed items, with each item's id in its test's name. Leave the other tests as "
                    "they are. Take every expected behaviour from spec/frozen-spec.md. INTERFACE.md says how the "
                    f"product is started and called. The tests are run from the repo root with: {' '.join(cfg['test_cmd'])}")
        todo = scan_fix_list(ctx, in_tests=True)
        if a.fix_tests and have_tests:
            task = ("The last conformance run failed or needs attention. Read reports/test-report.txt and any reviewed answers in "
                    "qa/answers/. For each failing test, decide from spec/frozen-spec.md and those answers "
                    "whether the test's expectation follows from the spec. Correct a test only where its "
                    "expectation does not follow from the spec; leave a correct test alone even though it "
                    "fails, because then the product is what is wrong. Never weaken a test to make it pass. "
                    "Finish with a list of the tests you changed and the spec text that justifies each change.")
            task += (" INTERFACE.md may have changed since you wrote the tests; follow it for how the product is "
                     "called.")
            if todo:
                task += (" Separately, a check on this repo requires these changes in tests/. Make each one with "
                         "fresh wording or values of your own. Do not ask why: " + " ; ".join(todo))
        res = run_session(ctx, "tester", task)
        session = res["public"]
        stray = strays(ctx.clean, before, ["tests"])
        if stray:
            problems.append(f"the tester changed files outside tests/: {stray}")
        if not res["ok"]:
            problems.append("tester session failed")
        if not any(p.is_file() for p in tests_dir.rglob("*")):
            problems.append("the tester wrote no tests: " + " ".join(res["final"].split())[:300])
        commit(ctx.clean, "test: black-box conformance tests from the frozen spec")

    env = scrubbed_env(ctx)
    report_lines = []
    rc = None
    for label, cmdline in (("setup", cfg.get("setup_cmd") or []), ("test", cfg["test_cmd"])):
        if not cmdline or (label == "setup" and not a.setup):
            continue
        try:
            proc = subprocess.run(sandboxed_argv(ctx, env_spec, cmdline, label), cwd=ctx.clean,
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=env,
                                  timeout=int(cfg.get("test_timeout", 900)))
            out, code = (proc.stdout or "") + (proc.stderr or ""), proc.returncode
        except subprocess.TimeoutExpired:
            out, code = "timed out", 124
        except OSError as exc:
            out, code = f"could not run: {exc}", 127
        # The report is committed, so keep this machine's folder layout out of it.
        for real, shown in ((str(ctx.clean), "."), (posix(ctx.clean), "."),
                            (str(Path.home()), "~"), (posix(Path.home()), "~")):
            out = out.replace(real, shown)
        report_lines += [f"$ {' '.join(cmdline)}", f"exit code: {code}", out[-20000:], ""]
        if label == "test":
            rc = code
    (ctx.clean / "reports").mkdir(exist_ok=True)
    (ctx.clean / "reports" / "test-report.txt").write_text("\n".join(report_lines), encoding="utf-8", newline="\n")
    tail = [ln for ln in "\n".join(report_lines).splitlines() if ln.strip()][-3:]
    print(f"  conformance run: exit code {rc}  ({' | '.join(t.strip()[:80] for t in tail)})")
    print(f"  run in: {env_spec['description']}")
    summary = run_scan(ctx)
    # The gate is counted against the spec's own checklist, not against whatever tests exist.
    cov = sc.coverage(read_text(ctx.clean / "spec" / "frozen-spec.md") or "", tests_dir)
    if cov["core_without_a_test"]:
        print(f"  core requirements with no test: {', '.join(cov['core_without_a_test'])}")
    if cov["cited_but_not_in_spec"]:
        print(f"  tests cite ids the spec does not have: {', '.join(cov['cited_but_not_in_spec'])}")
    if cov["other_without_a_test"]:
        print(f"  non-core requirements with no test: {', '.join(cov['other_without_a_test'])}")
    covered = not cov["core_without_a_test"]
    passed = rc == 0 and summary["findings"] == 0 and not problems and covered
    test_files = [posix(p.relative_to(ctx.clean)) for p in tests_dir.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    write_record(ctx.clean, "test", "clean", ["spec/frozen-spec.md", "INTERFACE.md"] + test_files,
                 ["reports/test-report.txt"],
                 {"spec_sha256": frozen, "test_command": cfg["test_cmd"], "exit_code": rc,
                  "test_environment": env_spec["description"], "sandboxed": env_spec["sandboxed"],
                  "requirement_coverage": cov,
                  "provenance_scan": summary, "problems": problems,
                  "ship_gate": "pass" if passed else "fail"},
                 "pass" if passed else "fail", session=session)
    commit(ctx.clean, f"test: conformance run ({'pass' if passed else 'fail'})")
    if not passed:
        why = []
        if rc != 0:
            why.append(f"tests failed (see reports/test-report.txt); try: spite build {ctx.target} --fix")
        if not covered:
            why.append("core requirements have no test (" + ", ".join(cov["core_without_a_test"]) +
                       f"); have the tester add them: spite test {ctx.target} --write-tests")
        if summary["findings"]:
            why.append("the provenance scan found dirty material in the clean repo")
        die("ship gate not passed:\n  " + "\n  ".join(why + problems))
    print(f"ship gate passed.\nnext: spite emit {ctx.target} --name <new-name> --license AGPL-3.0|Apache-2.0")


# ------------------------------------------------------------------------- qa

def pending_questions(ctx):
    qdir = ctx.clean / "qa" / "questions"
    adir = ctx.clean / "qa" / "answers"
    if not qdir.is_dir():
        return []
    return [q for q in sorted(qdir.glob("*.md")) if not (adir / q.name).exists()]


def cmd_qa(ctx):
    verify_spec(ctx)
    require_untouched_clean(ctx)
    pending = pending_questions(ctx)
    if not pending:
        print("no open questions from the clean side")
        return
    for q in pending:
        qtext = q.read_text(encoding="utf-8", errors="replace")
        (ctx.dirty / "qa").mkdir(exist_ok=True)
        dq = ctx.dirty / "qa" / f"question-{q.name}"
        da = ctx.dirty / "qa" / f"answer-{q.name}"
        dq.write_text(qtext, encoding="utf-8", newline="\n")
        before = changed_paths(ctx.dirty)
        task = (f"The clean side asked the question in qa/{dq.name}. Answer it in qa/{da.name}, in your own "
                f"words, as a functional statement only. Use spec/ and survey/ as your sources. The answer "
                f"crosses the gate, so it must carry no expressive material and no name or mark of the target. "
                f"If the question cannot be answered from public material, say that and nothing more.")
        res = run_session(ctx, "reviewer", task)
        problems = []
        if not da.is_file():
            problems.append("no answer was written")
            failures = []
        else:
            atext = da.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
            failures, _ = lint_crossing(ctx, atext)
        stray = strays(ctx.dirty, before, ["qa"])
        if stray:
            problems.append(f"files changed outside qa/: {stray}")
        passed = res["ok"] and not problems and not failures
        if passed:
            dest = ctx.clean / "qa" / "answers" / q.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(atext.encode("utf-8"))
        write_record(ctx.dirty, "qa", "dirty", [f"qa/{dq.name}"], [f"qa/{da.name}"],
                     {"question": q.name, "lint_failures": failures, "problems": problems,
                      "crossed_the_gate": [f"qa/answers/{q.name}"] if passed else []},
                     "pass" if passed else "fail", session=res["public"], other=ctx.clean)
        commit(ctx.dirty, f"qa: answer to {q.name}")
        if passed:
            write_record(ctx.clean, "qa", "clean", [f"qa/questions/{q.name}"], [f"qa/answers/{q.name}"],
                         {"question": q.name}, "pass",
                         notes="A reviewed written answer; with the frozen spec, the only material to cross the gate.",
                         other=ctx.dirty)
            commit(ctx.clean, f"qa: reviewed answer to {q.name}")
            print(f"  answered {q.name}")
        else:
            print(f"  {q.name}: answer did not pass the crossing checks and stays on the dirty side")


# ----------------------------------------------------------------------- emit

def cmd_emit(ctx):
    a = ctx.args
    frozen = verify_spec(ctx)
    if a.license not in LICENSES:
        die(f"--license must be one of: {', '.join(LICENSES)}")
    name = a.name.strip()
    low = name.lower()
    for mark in ctx.marks + [ctx.target]:
        m = mark.strip().lower()
        if m and (m in low or low in m):
            die(f"the new name '{name}' overlaps the target's mark '{mark}'; pick a name with no reference to it")
    test = latest_record(ctx.clean, "test")
    if not test or test["result"] != "pass" or test["checks"].get("spec_sha256") != frozen:
        die("the ship gate has not passed for the current frozen spec; run test first")
    build = latest_record(ctx.clean, "build")
    if build and (build.get("seq", 0), build["date"]) > (test.get("seq", 0), test["date"]):
        die("the build changed after the last passing test; run test again")
    require_untouched_clean(ctx)
    deps = sc.declared_dependencies(ctx.clean)
    n_deps = sum(len(v) for v in deps.values())
    if n_deps and not a.deps_reviewed:
        listing = "; ".join(f"{m}: {', '.join(d[:8])}" for m, d in deps.items() if d)
        die(f"the product declares {n_deps} third-party dependencies ({listing[:400]}).\n"
            f"Their licences have to be compatible with {a.license} and their notices carried. This tool does "
            "not judge that. Once a person has checked, rerun with --deps-reviewed \"who checked and what they "
            "found\", which is recorded.")
    collisions = {} if a.no_name_check else sc.name_collisions(name)
    taken = [reg for reg, state in collisions.items() if state == "taken"]
    if taken:
        print(f"  note: a package named '{name.lower()}' already exists on {', '.join(taken)}. This is a report, "
              "not a block; trade mark and naming are the operator's call.")

    before = changed_paths(ctx.clean)
    task = (f"Prepare this repo for release under the name \"{name}\" and the {a.license} licence. Write README.md "
            f"(what it does, how to install and run it, how to run the conformance tests, the licence). If the "
            f"frozen spec describes an import or interchange format and the code implements it, document the "
            f"importer in docs/. Describe only what the code in this repo does. Do not mention any other product "
            f"or company by name.")
    todo = [t for t in scan_fix_list(ctx, in_tests=False) if t.startswith(("README.md", "docs/"))]
    if todo:
        task += (" A README and docs from an earlier attempt are already here. Keep what is sound and make these "
                 "required changes, each with fresh wording of your own. Do not ask why: " + " ; ".join(todo))
    res = run_session(ctx, "emit", task)
    problems = []
    stray = strays(ctx.clean, before, ["docs"], ["README.md"])
    if stray:
        problems.append(f"the emit session changed files outside README.md and docs/: {stray}")
    if not (ctx.clean / "README.md").is_file():
        problems.append("README.md was not written")

    summary = run_scan(ctx)  # before the provenance bundle (which names sources) goes in
    if summary["findings"]:
        problems.append("the provenance scan found dirty material")

    outputs = ["README.md", "LICENSE", "PROVENANCE.md"]
    if not problems and res["ok"]:
        shutil.copyfile(ASSETS / "licenses" / LICENSES[a.license], ctx.clean / "LICENSE")
        prov = ctx.clean / "provenance"
        (prov / "records").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ctx.dirty / "survey" / "sources.csv", prov / "sources.csv")
        shutil.copyfile(ctx.clean / "spec" / "frozen-spec.md", prov / "frozen-spec.md")
        shutil.copyfile(ctx.clean / "spec" / "frozen-spec.sha256", prov / "frozen-spec.sha256")
        if (ctx.clean / "spec" / "frozen-delta.md").is_file() and update_cycle(ctx):
            shutil.copyfile(ctx.clean / "spec" / "frozen-delta.md", prov / "frozen-delta.md")
        baseline = ctx.dirty / "spec" / "baseline-spec.md"
        if baseline.is_file():  # workspaces frozen before specs were filed by hash
            keep_frozen_spec(ctx, baseline.read_bytes())
        history = ctx.dirty / "spec" / "frozen-history"
        if history.is_dir():
            (prov / "frozen-history").mkdir(exist_ok=True)
            for old_spec in sorted(history.glob("*.md")):
                if old_spec.stem != frozen:
                    shutil.copyfile(old_spec, prov / "frozen-history" / old_spec.name)
        rows = []
        for repo, side in ((ctx.dirty, "dirty"), (ctx.clean, "clean")):
            for p in sorted((repo / "records").glob("*.json")):
                shutil.copyfile(p, prov / "records" / f"{side}-{p.name}")
                rec = load_json(p)
                rows.append((rec["date"], side, rec["step"], rec["result"], f"{side}-{p.name}"))
        qa_log = sorted((ctx.clean / "qa" / "answers").glob("*.md"))
        if qa_log:
            (prov / "qa").mkdir(exist_ok=True)
            for ans in qa_log:
                shutil.copyfile(ctx.clean / "qa" / "questions" / ans.name, prov / "qa" / f"question-{ans.name}")
                shutil.copyfile(ans, prov / "qa" / f"answer-{ans.name}")
        rows.sort()
        table = "\n".join(f"| {d} | {side} | {step} | {result} | `provenance/records/{fn}` |"
                          for d, side, step, result, fn in rows)
        with open(ctx.dirty / "survey" / "sources.csv", encoding="utf-8", newline="") as fh:
            n_sources = sum(1 for _ in csv.DictReader(fh))
        template = (ASSETS / "PROVENANCE.template.md").read_text(encoding="utf-8")
        text = (template.replace("{{NAME}}", name).replace("{{LICENSE}}", a.license)
                .replace("{{SPEC_SHA256}}", frozen).replace("{{DATE}}", str(now().date()))
                .replace("{{SOURCES}}", str(n_sources)).replace("{{QA}}", str(len(qa_log)))
                .replace("{{TEST_ENVIRONMENT}}", test["checks"].get("test_environment", "not recorded"))
                .replace("{{RECORDS_TABLE}}", table))
        (ctx.clean / "PROVENANCE.md").write_text(text, encoding="utf-8", newline="\n")
        shutil.copyfile(Path(__file__).resolve().parent / "spite_checks.py", prov / "verify.py")
        save_json(prov / "dependencies.json", {
            "declared_third_party_dependencies": deps,
            "licence_review": a.deps_reviewed or ("none needed: no third-party dependencies are declared"
                                                  if not n_deps else "not recorded"),
        })
        outputs += [posix(p.relative_to(ctx.clean)) for p in walk_files(prov)]

    passed = res["ok"] and not problems
    write_record(ctx.clean, "emit", "clean", ["spec/frozen-spec.md"], outputs,
                 {"name": name, "license": a.license, "spec_sha256": frozen, "provenance_scan": summary,
                  "test_environment": test["checks"].get("test_environment"),
                  "tests_were_sandboxed": test["checks"].get("sandboxed"),
                  "third_party_dependencies": n_deps, "dependency_licence_review": a.deps_reviewed or None,
                  "commits_signed_by_operator": bool(ctx.cfg.get("signing_key")),
                  "name_in_package_registries": collisions or "not checked",
                  "update_number": (update_cycle(ctx) or {}).get("number"),
                  "previous_release_record_sha256": (update_cycle(ctx) or {}).get("prior_release_record_sha256"),
                  "previous_frozen_spec_sha256": (update_cycle(ctx) or {}).get("prior_frozen_sha256"),
                  "problems": problems, "published": False},
                 "pass" if passed else "fail", session=res["public"], other=ctx.dirty)
    if not passed:
        commit(ctx.clean, "release attempt (not passed)")
        die("emit did not pass:\n  " + "\n  ".join(problems or ["session failed"]))
    final = sorted((ctx.clean / "records").glob(f"{STEP_NO['emit']}-emit-*.json"))[-1]
    shutil.copyfile(final, ctx.clean / "provenance" / "records" / f"clean-{final.name}")
    commit(ctx.clean, f"release: {name} under {a.license}")
    dcfg = load_json(ctx.dirty / "spite.json")
    closed = dcfg.pop("update_cycle", None)
    dcfg.pop("releases", None)
    if closed:
        print(f"release {closed['number']} (an update) is prepared; the update cycle is closed")
    save_json(ctx.dirty / "spite.json", dcfg)
    commit(ctx.dirty, "release recorded")
    print(f"release prepared locally in {ctx.clean}")
    print("nothing has been pushed or published. Read PROVENANCE.md and the repo, then publish it yourself.")


# --------------------------------------------------------------------- status

def _same_repo(a, b):
    def norm(u):
        u = u.strip().lower().rstrip("/")
        u = u[:-4] if u.endswith(".git") else u
        u = re.sub(r"^(https?://|ssh://|git@)", "", u).replace(":", "/")
        return u.split("@")[-1]
    return bool(a and b) and norm(a) == norm(b)


DIRTY_PREPUSH = r'''#!/bin/sh
# The dirty repo holds research on another company's product. It may only be pushed to a
# private repo. This hook checks the destination before anything is sent.
url="$2"
case "$url" in
  *github.com*)
    slug=$(printf '%s' "$url" | sed -E 's#^(https?://|ssh://)?([^@/]*@)?github\.com[:/]+##; s#\.git$##; s#/$##')
    closed="$(git rev-parse --git-dir)/spite-closed-destinations"
    low=$(printf '%s' "$slug" | tr 'A-Z' 'a-z')
    if [ -f "$closed" ]; then
      while IFS= read -r line || [ -n "$line" ]; do
        if [ "$line" = "$low" ]; then
          echo "Refusing to push the dirty repo to $slug: that repo is for the product or for the skill." >&2
          echo "The dirty repo never shares a home with either." >&2
          exit 1
        fi
      done < "$closed"
    fi
    vis=$(gh repo view "$slug" --json visibility --jq .visibility 2>/dev/null)
    if [ "$vis" = "PRIVATE" ]; then exit 0; fi
    echo "Refusing to push the dirty repo: $slug is '${vis:-not checkable}', not PRIVATE." >&2
    exit 1 ;;
  *)
    if [ "$SPITE_DIRTY_DESTINATION_IS_PRIVATE" = "yes" ]; then exit 0; fi
    echo "Cannot check whether $url is private, and the dirty repo may only go to a private repo." >&2
    echo "If you have checked it yourself, push with SPITE_DIRTY_DESTINATION_IS_PRIVATE=yes set." >&2
    exit 1 ;;
esac
'''


def install_dirty_prepush(repo, closed=()):
    """Put the push guard in the dirty repo: private destinations only, and never the repos in
    `closed` (the product's own destination and the skill's repo). Safe to run again."""
    hooks = Path(repo) / ".git" / "hooks"
    if not hooks.parent.is_dir():
        return False
    hooks.mkdir(exist_ok=True)
    slugs = []
    for url in closed:
        m = re.search(r"github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?/?$", (url or "").strip())
        if m:
            slugs.append(m.group(1).lower())
    (hooks.parent / "spite-closed-destinations").write_text(
        "".join(s + "\n" for s in sorted(set(slugs))), encoding="utf-8", newline="\n")
    path = hooks / "pre-push"
    path.write_text(DIRTY_PREPUSH, encoding="utf-8", newline="\n")
    try:
        path.chmod(0o755)
    except OSError:
        pass
    return True


def repo_visibility(url):
    """PRIVATE, PUBLIC or INTERNAL for a GitHub repo, or None when it cannot be checked."""
    m = re.search(r"github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url.strip())
    if not m or not shutil.which("gh"):
        return None
    res = subprocess.run(["gh", "repo", "view", m.group(1), "--json", "visibility", "--jq", ".visibility"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    return res.stdout.strip() or None


def cmd_remote(ctx):
    """Show or set where a target's two repos are checked in. Never pushes.

    No destination is assumed. The skill's own repo holds only the skill, so it is refused, and
    the dirty and clean repos may never share a destination. The dirty repo may only have a
    private destination, and a push guard in the repo itself enforces that."""
    a = ctx.args
    repos = {"dirty": ctx.dirty, "clean": ctx.clean}
    current = {side: git(repo, "remote", "get-url", "origin").stdout.strip() for side, repo in repos.items()}
    wanted = {"dirty": a.dirty, "clean": a.clean}
    skill_origin = git(SKILL_DIR, "remote", "get-url", "origin").stdout.strip()
    guarded = install_dirty_prepush(ctx.dirty, [wanted["clean"] or current["clean"], skill_origin])
    if not any(wanted.values()) and not a.dirty_on_branch and not a.auto_push:
        kept = ctx.cfg.get("dirty_backup")
        print(f"push after each step: {'on' if ctx.cfg.get('auto_push') else 'off'}")
        for side in repos:
            where = current[side] or "(local only, no destination)"
            if side == "dirty" and not current[side] and kept:
                where = (f"snapshots to branch '{kept['branch']}' of the clean destination, "
                         f"in folder {kept['dir']}/  (spite backup {ctx.target})")
            print(f"{side}: {repos[side].name} -> {where}")
        print(f"dirty repo push guard (private destinations only): {'in place' if guarded else 'not installed (no git repo)'}")
        if not current["dirty"] and not kept:
            print("The dirty repo is the evidence behind the release. The operator chooses what happens to it:\n"
                  f"  keep it local                        (nothing to do)\n"
                  f"  its own private repo                 spite remote {ctx.target} --dirty <private repo url>\n"
                  f"  a branch of the product's dev repo   spite remote {ctx.target} --dirty-on-branch\n"
                  "Never a public repo.")
        return
    final = {side: wanted[side] or current[side] for side in repos}
    for side, url in wanted.items():
        if url and _same_repo(url, skill_origin):
            die(f"{url} is the SPITE skill's own repo, which holds only the skill. "
                f"Ask the operator for a separate destination for the {side} repo.")
    if _same_repo(final["dirty"], final["clean"]):
        die("the dirty and clean repos cannot share a destination; they are never mixed")
    dirty_privacy = None
    if wanted["dirty"]:
        vis = repo_visibility(wanted["dirty"])
        if vis == "PRIVATE":
            dirty_privacy = "checked with the host: private"
        elif vis:
            die(f"{wanted['dirty']} is {vis}. The dirty repo holds research on another company's product "
                "and may only go to a private repo.")
        elif a.dirty_is_private:
            dirty_privacy = "could not be checked; the operator states it is private"
        else:
            die(f"cannot check whether {wanted['dirty']} is private. If the operator has checked it, "
                "rerun with --dirty-is-private, which records that they said so.")
    backup = None
    if a.dirty_on_branch:
        # The simple option: research snapshots live on their own branch of the product's private
        # dev repo, under one folder. They never enter the product branch or the clean working tree.
        if final["dirty"]:
            die("the dirty repo already has its own destination; choose one way of backing it up")
        if not final["clean"]:
            die("set the clean destination first (--clean <url>); the research branch lives in that repo")
        if a.dirty_on_branch.lower() in ("main", "master", "trunk"):
            die("the research branch cannot be the product's own branch")
        vis = repo_visibility(final["clean"])
        if vis == "PRIVATE":
            privacy = "checked with the host: private"
        elif vis:
            die(f"{final['clean']} is {vis}. Research on another company's product may only be kept in a private repo.")
        elif a.dirty_is_private:
            privacy = "could not be checked; the operator states it is private"
        else:
            die(f"cannot check whether {final['clean']} is private. If the operator has checked it, "
                "rerun with --dirty-is-private, which records that they said so.")
        backup = {"mode": "branch", "branch": a.dirty_on_branch, "dir": "provenance", "privacy": privacy}
        print(f"dirty: research snapshots -> {final['clean']}, branch '{a.dirty_on_branch}', folder provenance/")
    for side, url in wanted.items():
        if not url:
            continue
        verb = "set-url" if current[side] else "add"
        res = git(repos[side], "remote", verb, "origin", url)
        if res.returncode != 0:
            die(f"could not set the {side} destination: {res.stderr.strip()[:200]}")
        print(f"{side}: {repos[side].name} -> {url}")
    cfg = load_json(ctx.dirty / "spite.json")
    cfg["destinations"] = {side: final[side] or None for side in repos}
    if dirty_privacy:
        cfg["dirty_destination_privacy"] = dirty_privacy
        print(f"dirty destination: {dirty_privacy}")
    if backup:
        cfg["dirty_backup"] = backup
    if a.auto_push:
        cfg["auto_push"] = a.auto_push == "on"
        print(f"push to the private repos after each step: {a.auto_push}")
    save_json(ctx.dirty / "spite.json", cfg)
    commit(ctx.dirty, "config: destination repos")
    print("destinations set. Nothing was pushed; pushing is the operator's decision each time.\n"
          "The dirty repo holds survey material and must go only to a private repo.")


def push_clean(ctx):
    """Push the product branch to its destination, but only while that destination is private.

    A push to a private repo is a backup and an outside record of when each step existed. A push
    to a public repo is publishing, which stays the operator's own act."""
    if not ctx.clean.exists() or not (ctx.clean / ".git").exists():
        return "no clean repo"
    url = git(ctx.clean, "remote", "get-url", "origin").stdout.strip()
    if not url:
        return "the clean repo has no destination"
    vis = repo_visibility(url)
    if vis != "PRIVATE":
        return (f"not pushed: {url} is {vis or 'not checkable'}, and pushing the product anywhere "
                "that is not private is publishing")
    if not git(ctx.clean, "log", "-1", "--format=%H").stdout.strip():
        return "the clean repo has no commits yet"
    res = git(ctx.clean, "push", "-u", "origin", "HEAD")
    if res.returncode != 0:
        return "push of the product branch failed: " + (res.stderr or res.stdout).strip()[-300:]
    return f"product branch pushed to {url} (private)"


def cmd_backup(ctx):
    """Push both sides to the private places the operator chose: the research, and the product
    branch while its destination is private. The only runner command that pushes."""
    print(push_clean(ctx))
    push_research(ctx)


def anchor(ctx):
    """After a step, when the operator has turned it on: push to the private repos, so an outside
    party holds a dated copy of every record. Failure here never fails the step."""
    try:
        if not ctx.dirty.exists() or not ctx.cfg.get("auto_push"):
            return
        print("  " + push_clean(ctx))
        if git(ctx.dirty, "remote", "get-url", "origin").stdout.strip() or ctx.cfg.get("dirty_backup"):
            push_research(ctx)
    except SystemExit:
        print("  note: the push after this step did not go through; run: spite backup " + ctx.target)


def push_research(ctx):
    cfg = ctx.cfg
    commit(ctx.dirty, "notes: changes not yet committed")
    origin = git(ctx.dirty, "remote", "get-url", "origin").stdout.strip()
    if origin:  # the dirty repo has a private repo of its own; its push guard checks the destination
        res = git(ctx.dirty, "push", "-u", "origin", "HEAD")
        if res.returncode != 0:
            die("backup push refused or failed:\n" + (res.stderr or res.stdout).strip()[-600:])
        print(f"research pushed to {origin}")
        return
    plan = cfg.get("dirty_backup")
    if not plan:
        die("no backup destination is set for the research, so it stays local. The operator chooses:\n"
            f"  spite remote {ctx.target} --dirty <private repo url>     a private repo of its own\n"
            f"  spite remote {ctx.target} --dirty-on-branch              a branch of the product's private dev repo")
    url = git(ctx.clean, "remote", "get-url", "origin").stdout.strip()
    if not url:
        die("the clean repo has no destination, so there is no repo to hold the research branch")
    vis = repo_visibility(url)
    if vis and vis != "PRIVATE":
        die(f"{url} is now {vis}. Research is not pushed to a repo that is not private.")
    if not vis and "operator states" not in plan.get("privacy", ""):
        die(f"cannot check whether {url} is private; set the backup again with --dirty-is-private if it is")
    branch, folder = plan["branch"], plan["dir"]
    heads = git(ctx.dirty, "ls-remote", "--heads", url).stdout
    if not [h for h in heads.splitlines() if not h.endswith(f"refs/heads/{branch}")]:
        die(f"{url} has no product branch yet. Push the clean repo's branch there first; otherwise the "
            f"research branch '{branch}' would become the repo's default branch.")

    work = ctx.state / "backup"  # runner-private; outside both repos
    if not (work / ".git").is_dir():
        work.mkdir(parents=True, exist_ok=True)
        git(work, "init", "-q")
        git(work, "remote", "add", "origin", url)
        apply_signing(ctx, work)
    else:
        git(work, "remote", "set-url", "origin", url)
    if git(work, "fetch", "-q", "origin", branch).returncode == 0:
        git(work, "checkout", "-q", "-B", branch, "FETCH_HEAD")
    else:
        git(work, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    dest = work / folder
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    listed = git(ctx.dirty, "ls-files", "-co", "--exclude-standard", "-z").stdout
    count = 0
    for rel_path in [p for p in listed.split("\0") if p]:
        src = ctx.dirty / rel_path
        if src.is_file():
            out = dest / rel_path
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, out)
            count += 1
    git(ctx.dirty, "bundle", "create", str(dest / "history.bundle"), "--all")
    (dest / "ABOUT.txt").write_text(
        "Research side of a clean-room build: what was read, the drafts, the questions and answers.\n"
        "Private. It is not part of the product and must never be pushed to a public repo.\n"
        "history.bundle holds the full history of this material (git clone history.bundle).\n",
        encoding="utf-8", newline="\n")
    git(work, "add", "-A")
    if git(work, "diff", "--cached", "--quiet").returncode == 0:
        print("research snapshot is already up to date")
    else:
        res = git(work, "commit", "-q", "-m", f"provenance: research snapshot {now().isoformat()}")
        if res.returncode != 0:
            die("could not commit the snapshot: " + (res.stderr or res.stdout).strip()[-300:])
    res = git(work, "push", "-u", "origin", f"HEAD:refs/heads/{branch}")
    if res.returncode != 0:
        die("backup push failed:\n" + (res.stderr or res.stdout).strip()[-600:])
    print(f"research snapshot ({count} files) pushed to {url}, branch '{branch}', folder {folder}/")
    print("That branch is private research. It must never go to a public repo.")


def apply_signing(ctx, repo):
    """Make every commit in a repo carry the operator's signature, when they have set a key."""
    key = ctx.cfg.get("signing_key")
    if not key or not (Path(repo) / ".git").exists():
        return
    signers = Path(repo) / ".git" / "allowed-signers"
    pub = Path(key).read_text(encoding="utf-8").strip()
    email = git(repo, "config", "user.email").stdout.strip() or "operator"
    signers.write_text(f"{email} {pub}\n", encoding="utf-8", newline="\n")
    for name, value in (("gpg.format", "ssh"), ("user.signingkey", posix(key)), ("commit.gpgsign", "true"),
                        ("gpg.ssh.allowedSignersFile", posix(signers))):
        git(repo, "config", name, value)


def signed_state(repo):
    """Whether the newest commit in a repo carries a good signature."""
    if not (Path(repo) / ".git").exists():
        return None
    res = git(repo, "verify-commit", "HEAD")
    return res.returncode == 0


def update_cycle(ctx):
    """The open update cycle, if the operator has started one with `spite update`."""
    return ctx.cfg.get("update_cycle") if (ctx.dirty / "spite.json").exists() else None


def cmd_update(ctx):
    """Start an update of a product that has already been released.

    Nothing is rebuilt from scratch. The survey is brought up to date, the spec is rewritten with
    its requirement ids kept stable, a delta that names what was added, changed and removed
    crosses the gate with the new spec, the builders change only what the delta calls for, and
    the new release's record points at the release before it."""
    released = latest_record(ctx.clean, "emit")
    if not released or released["result"] != "pass":
        die("there is no release to update; `spite update` starts from a passing emit")
    if update_cycle(ctx):
        die(f"update {update_cycle(ctx)['number']} is already open; finish it with emit")
    require_untouched_clean(ctx)
    frozen = verify_spec(ctx)
    emit_file = sorted((ctx.clean / "records").glob(f"{STEP_NO['emit']}-emit-*.json"))[-1]
    cfg = load_json(ctx.dirty / "spite.json")
    # Releases are counted from the record: one per frozen spec that reached a passing emit.
    number = len({r["checks"].get("spec_sha256") for r in records_of(ctx.clean, "emit") if r["result"] == "pass"}) + 1
    cfg["update_cycle"] = {"number": number, "started": now().isoformat(), "prior_frozen_sha256": frozen,
                           "prior_release_record_sha256": sha256_file(emit_file)}
    save_json(ctx.dirty / "spite.json", cfg)
    ctx._cfg = None
    shutil.copyfile(ctx.dirty / "spec" / "frozen-spec.md", ctx.dirty / "spec" / "baseline-spec.md")
    keep_frozen_spec(ctx, (ctx.dirty / "spec" / "frozen-spec.md").read_bytes())
    obs = ctx.dirty / "survey" / "observations.md"
    if obs.is_file():
        shutil.copyfile(obs, ctx.dirty / "survey" / "baseline-observations.md")
    write_record(ctx.dirty, "update-start", "dirty", ["spec/frozen-spec.md"],
                 ["spec/baseline-spec.md", "survey/baseline-observations.md"],
                 {"update_number": number, "previous_frozen_spec_sha256": frozen,
                  "previous_release_record_sha256": sha256_file(emit_file)}, "pass", other=ctx.clean)
    commit(ctx.dirty, f"update {number}: started")
    mode = ctx.cfg.get("survey_mode")
    how = (f"spite survey {ctx.target} --from-dir <folder of newly saved pages>" if mode != "live"
           else f"spite survey {ctx.target}")
    print(f"update {number} is open. The steps are the same ones, and each knows it is an update:\n"
          f"  {how}\n"
          f"  spite partition {ctx.target}     new spec with ids kept stable, plus a delta\n"
          f"  spite review {ctx.target}        spec and delta\n"
          f"  spite freeze {ctx.target}        the delta crosses with the spec\n"
          f"  spite build {ctx.target}         changes only what the delta calls for\n"
          f"  spite test {ctx.target}          tests follow the delta\n"
          f"  spite emit {ctx.target} --name <same name> --license <same licence>")


SCOUT_HEADER = ["candidate", "what_they_sell", "core_value_in_code", "weeks_to_build_core", "open_source_equivalents",
                "paying_users", "qualifies", "score", "main_doubt"]


def cmd_scout(ctx):
    """Look a list of candidate products over against the target gate. Starts no survey.

    The scout works from search results only and has no tool for opening a candidate's own
    pages, so nothing here touches a candidate's site. Its table is a first sift for the
    operator, who still records the gate for any target they pick."""
    a = ctx.args
    ctx.dirty = ctx.root / f"spite-scout-{ctx.target}"
    ctx.state = ctx.root / ".spite" / f"scout-{ctx.target}"
    work = ctx.dirty
    if not work.exists():
        if not a.list:
            die("a new scout needs --list <file>: one candidate per line (name, optionally a site and a note)")
        (work / "batches").mkdir(parents=True)
        (work / "records").mkdir()
        save_json(work / "spite.json", {"target": ctx.target, "kind": "scout", "marks": [], "created": now().isoformat(),
                                        "dirty_model": None, "clean_model": None})
        if shutil.which("git"):
            git(work, "init", "-q", "-b", "main")
            with open(work / ".git" / "info" / "exclude", "a", encoding="utf-8", newline="\n") as fh:
                fh.write(".claude/\n.spite/\n")
    ctx._cfg = None
    if a.list:
        lines = [ln.strip() for ln in Path(a.list).read_text(encoding="utf-8", errors="replace").splitlines()]
        lines = [ln for ln in lines if ln and not ln.startswith("#")]
        (work / "candidates.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    candidates = [ln for ln in (work / "candidates.txt").read_text(encoding="utf-8").splitlines() if ln.strip()]
    done = set()
    table = work / "table.csv"
    rows = []
    if table.is_file():
        with open(table, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        done = {r["candidate"].strip().lower() for r in rows}
    todo = [c for c in candidates if c.split(",")[0].strip().lower() not in done]
    if a.limit:
        todo = todo[:a.limit]
    if not todo:
        print("every candidate on the list has been looked at")
    size = max(1, a.batch)
    problems = []
    for start in range(0, len(todo), size):
        batch = todo[start:start + size]
        n = len(list((work / "batches").glob("batch-*.csv"))) + 1
        out_csv, out_md = f"batches/batch-{n:02d}.csv", f"batches/batch-{n:02d}.md"
        task = ("Assess each candidate below against the four-part target gate in your instructions, using search "
                f"results only. Today's date is {now().date()}. Write one row per candidate to {out_csv} with exactly "
                f"this header: {','.join(SCOUT_HEADER)}. Write your notes, the searches you ran and the result links "
                f"behind each judgement to {out_md}. Candidates:\n" + "\n".join(f"- {c}" for c in batch))
        res = run_session(ctx, "scout", task)
        got = []
        path = work / out_csv
        if path.is_file():
            with open(path, encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                if [h.strip() for h in (reader.fieldnames or [])] != SCOUT_HEADER:
                    problems.append(f"{out_csv}: wrong header")
                else:
                    got = [r for r in reader if (r.get("candidate") or "").strip()]
        else:
            problems.append(f"{out_csv} was not written")
        if not res["ok"]:
            problems.append(f"the scout session for batch {n} failed")
        rows += got
        print(f"  batch {n}: {len(got)} of {len(batch)} candidates assessed")

    def score(r):
        try:
            return float(r.get("score") or 0)
        except ValueError:
            return 0.0
    rows.sort(key=score, reverse=True)
    with open(table, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SCOUT_HEADER, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    lines = ["# Scout report", "",
             "A first sift against the target gate, from search results only. No candidate's own pages were opened "
             "and no survey was started. Check anything you mean to act on; the gate itself is recorded per target "
             "with `spite gate`.", "",
             "| # | Candidate | Qualifies | Score | Weeks | What they sell | Open-source equivalents | Main doubt |",
             "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        cell = lambda k: (r.get(k) or "").replace("|", "/").strip()[:140]
        lines.append(f"| {i} | {cell('candidate')} | {cell('qualifies')} | {cell('score')} | {cell('weeks_to_build_core')} | "
                     f"{cell('what_they_sell')} | {cell('open_source_equivalents')} | {cell('main_doubt')} |")
    (work / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    write_record(work, "scout", "dirty", ["candidates.txt"], ["table.csv", "report.md"],
                 {"candidates_listed": len(candidates), "assessed_in_total": len(rows),
                  "left": len(candidates) - len(rows), "problems": problems,
                  "method": "search results only; no candidate's own pages opened; no survey started"},
                 "pass" if not problems else "fail")
    commit(work, "scout: candidates assessed against the target gate")
    print(f"report: {posix(work / 'report.md')}   ({len(rows)} assessed, {len(candidates) - len(rows)} left)")
    for i, r in enumerate(rows[:10], 1):
        print(f"  {i:2}. {r.get('candidate', '')[:28]:28} {r.get('qualifies', ''):10} score {r.get('score', '')}")
    if problems:
        die("scout finished with problems:\n  " + "\n  ".join(problems))


def cmd_config(ctx):
    """Change how the clean repo's tests are run, or which model each side uses.

    The clean repo's settings file is closed to the build side, and the driving session must not
    edit the clean repo by hand, so this is the one way to change it. Each change is recorded."""
    a = ctx.args
    changed = {}
    wants_clean = any(v is not None for v in (a.sandbox, a.image, a.test_cmd, a.setup_cmd))
    if wants_clean:
        require_untouched_clean(ctx)
        path = ctx.clean / "spite.json"
        cfg = load_json(path)
        sandbox = dict(cfg.get("sandbox") or {"kind": "none"})
        if a.sandbox is not None:
            sandbox = {"kind": a.sandbox}
            if a.sandbox == "docker":
                sandbox["image"] = a.image or (cfg.get("sandbox") or {}).get("image", "python:3.12-slim")
            changed["sandbox"] = a.sandbox
        if a.image is not None:
            sandbox["image"] = a.image
            changed["image"] = a.image
        cfg["sandbox"] = sandbox
        if a.test_cmd is not None:
            cfg["test_cmd"] = a.test_cmd.split()
            changed["test_cmd"] = cfg["test_cmd"]
        if a.setup_cmd is not None:
            cfg["setup_cmd"] = a.setup_cmd.split()
            changed["setup_cmd"] = cfg["setup_cmd"]
        save_json(path, cfg)
        write_record(ctx.clean, "config", "clean", [], ["spite.json"], {"changed": changed}, "pass",
                     notes="A change to how the tests are run, made by the operator through the runner.")
        commit(ctx.clean, "config: how the tests are run")
    if a.sign_with is not None:
        dcfg = load_json(ctx.dirty / "spite.json")
        if a.sign_with in ("", "off"):
            dcfg["signing_key"] = None
            for repo in (ctx.dirty, ctx.clean):
                git(repo, "config", "--unset", "commit.gpgsign")
            print("commit signing: off")
        else:
            key = Path(a.sign_with).expanduser()
            if key.suffix != ".pub":
                key = Path(str(key) + ".pub")
            if not key.is_file():
                die(f"no public key at {key}; pass the path of an SSH public key whose private key is beside it")
            dcfg["signing_key"] = posix(key.resolve())
        save_json(ctx.dirty / "spite.json", dcfg)
        ctx._cfg = None
        if ctx.cfg.get("signing_key"):
            for repo in (ctx.dirty, ctx.clean):
                apply_signing(ctx, repo)
        commit(ctx.dirty, "config: commit signing")
        if ctx.cfg.get("signing_key") and not signed_state(ctx.dirty):
            die("signing was set, but a test commit did not carry a good signature. The key may need a "
                "passphrase; the runner cannot answer a prompt. Turn it off with: "
                f"spite config {ctx.target} --sign-with off")
    if a.dirty_model is not None or a.clean_model is not None:
        dcfg = load_json(ctx.dirty / "spite.json")
        if a.dirty_model is not None:
            dcfg["dirty_model"] = a.dirty_model or None
        if a.clean_model is not None:
            dcfg["clean_model"] = a.clean_model or None
        save_json(ctx.dirty / "spite.json", dcfg)
        commit(ctx.dirty, "config: models per side")
        ctx._cfg = None
    cfg = load_json(ctx.clean / "spite.json")
    sb = cfg.get("sandbox") or {"kind": "none"}
    print(f"tests run with: {' '.join(cfg['test_cmd'])}")
    print(f"setup command:  {' '.join(cfg.get('setup_cmd') or []) or '(none)'}")
    print(f"sandbox:        {sb.get('kind')}" + (f" ({sb.get('image')})" if sb.get("kind") == "docker" else ""))
    d, c = ctx.cfg.get("dirty_model") or "(default)", ctx.cfg.get("clean_model") or "(default)"
    state = {True: "on, last commit verified", False: "set, but the last commit is not signed", None: "n/a"}
    print(f"commit signing: {state[signed_state(ctx.dirty)] if ctx.cfg.get('signing_key') else 'off'}")
    print(f"models:         dirty side {d}, clean side {c}"
          + ("" if d != c and "(default)" not in (d, c) else "   <- set two different ones for a stronger wall"))


def cmd_help(args):
    """Print the plain-language guide for people (HELP.md)."""
    text = (SKILL_DIR / "HELP.md").read_text(encoding="utf-8")
    sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))


MIN_CLAUDE = (2, 1, 259)  # restricted sessions and --permission-prompts both need this


def cmd_doctor(args):
    """Check that this machine has what the pipeline needs, before anything is started."""
    rows = []

    def row(name, ok, detail, fix=""):
        rows.append((name, ok, detail, fix))

    v = sys.version_info
    row("Python 3.9 or later", v >= (3, 9), f"{v.major}.{v.minor}.{v.micro}", "install a newer Python")

    exe = shutil.which("claude")
    if not exe:
        row("Claude Code on the PATH", False, "the `claude` command was not found",
            "install Claude Code; the runner starts each role as a `claude` session")
    else:
        out = subprocess.run(claude_cmd() + ["--version"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
        found = tuple(int(x) for x in m.groups()) if m else None
        need = ".".join(str(x) for x in MIN_CLAUDE)
        row(f"Claude Code {need} or later", bool(found and found >= MIN_CLAUDE),
            ".".join(str(x) for x in found) if found else "version not readable",
            "update Claude Code; older versions lack the restricted sessions the wall depends on")

    if not shutil.which("git"):
        row("git", False, "not found", "install git; every step is committed")
    else:
        name = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True).stdout.strip()
        email = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True).stdout.strip()
        row("git with a name and email", bool(name and email), "set" if name and email else "user.name or user.email is not set",
            'git config --global user.name "..." and git config --global user.email "..."')

    if not shutil.which("docker"):
        row("a sandbox for the test run (Docker)", False, "the `docker` command was not found",
            "install a container runtime, or accept the risk per run with: spite test <target> --unsandboxed")
    else:
        try:
            up = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=30)
            ok, detail = up.returncode == 0, (up.stdout.strip() or "installed, but the daemon is not answering")
        except (subprocess.TimeoutExpired, OSError):
            ok, detail = False, "installed, but the daemon is not answering"
        row("a sandbox for the test run (Docker)", ok, detail, "start the container runtime")

    row("gh, to check that a repo is private (optional)", bool(shutil.which("gh")),
        "found" if shutil.which("gh") else "not found",
        "without it, the operator has to state that a research destination is private")

    width = max(len(r[0]) for r in rows)
    needed_ok = True
    for name, ok, detail, fix in rows:
        optional = "(optional)" in name
        mark = "ok  " if ok else ("note" if optional else "MISSING")
        print(f"{mark:8}{name.ljust(width)}  {detail}")
        if not ok:
            print(f"{'':8}{'':{width}}  -> {fix}")
            needed_ok = needed_ok and optional
    print(f"\nthis skill is reached as: {slash_command()}")
    print("ready" if needed_ok else "not ready: fix the items marked MISSING, then run this again")
    sys.exit(0 if needed_ok else 1)


PORTABLE_KEYS = ("name", "description", "license", "allowed-tools", "metadata", "compatibility")


def cmd_package(args):
    """Write the skill as one file, spite.skill, for uploading or handing to someone.

    Upload forms accept a .skill or .zip that contains SKILL.md, and they accept only a small
    set of frontmatter keys. The copy of SKILL.md inside the package keeps those keys and drops
    the rest; the folder you run from is left as it is."""
    import zipfile
    out_dir = Path(args.out or os.getcwd()).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "spite.skill"
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    _, front, body = text.split("---", 2)
    kept, dropped, keep_line = [], [], True
    for line in front.strip().splitlines():
        if line[:1] not in (" ", "\t") and ":" in line:
            key = line.split(":", 1)[0].strip()
            keep_line = key in PORTABLE_KEYS
            if not keep_line:
                dropped.append(key)
        if keep_line:
            kept.append(line)
    portable = "---\n" + "\n".join(kept) + "\n---" + body
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(SKILL_DIR.rglob("*")):
            rel = f.relative_to(SKILL_DIR)
            if not f.is_file() or "__pycache__" in rel.parts or f.suffix == ".pyc" or rel.parts[0] == "evals":
                continue
            arc = (Path(SKILL_DIR.name) / rel).as_posix()
            if rel.as_posix() == "SKILL.md":
                z.writestr(arc, portable)
            else:
                z.write(f, arc)
            count += 1
    print(f"packaged {count} files: {target}")
    if dropped:
        print(f"left out of the packaged SKILL.md (not accepted by upload forms): {', '.join(dropped)}")
    print("This skill drives Claude Code on your own machine: it starts sessions with the `claude` command and\n"
          "uses git and a container runtime. Uploaded to the Claude apps it can explain itself, but the pipeline\n"
          "only runs where those are available.")


HINT_MARK = "spite_hint.py"


def slash_command():
    """What the user types to reach this skill: /spite as a plain skill, /spite:spite when it
    was installed as a plugin (a plugin's skills carry the plugin's name in front)."""
    plugin_root = SKILL_DIR.parent.parent
    if SKILL_DIR.parent.name == "skills" and (plugin_root / ".claude-plugin" / "plugin.json").is_file():
        try:
            name = json.loads((plugin_root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")).get("name")
        except ValueError:
            name = None
        if name:
            return f"/{name}:{SKILL_DIR.name}"
    return f"/{SKILL_DIR.name}"


def cmd_hint(args):
    """Turn the on-screen reminder "type /spite help for help" on or off.

    It is a pair of hooks in a Claude Code settings file: one shows the line when a session
    starts, the other every Nth prompt. By default the settings file is the current folder's
    (.claude/settings.json), so the reminder appears only in sessions started there. --user puts
    it in the user's own settings instead, for every session. The line is shown to the person
    only; the model does not see it."""
    base = (Path.home() / ".claude") if args.user else (Path.cwd() / ".claude")
    settings = base / "settings.json"
    data = {}
    if settings.is_file():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except ValueError:
            die(f"{settings} is not valid JSON; fix it by hand before changing it with this command")
    hooks = data.get("hooks") or {}

    def ours(entry):
        return any(HINT_MARK in (h.get("command") or "") for h in entry.get("hooks", []))

    present = any(ours(e) for ev in ("SessionStart", "UserPromptSubmit") for e in hooks.get(ev, []))
    if args.state is None:
        where = "your user settings (every session)" if args.user else f"this folder ({Path.cwd()})"
        print(f"reminder in {where}: {'on' if present else 'off'}")
        print("turn it on:  spite hint on [--every N] [--user]      off:  spite hint off [--user]")
        return
    for ev in ("SessionStart", "UserPromptSubmit"):
        kept = [e for e in hooks.get(ev, []) if not ours(e)]
        if kept:
            hooks[ev] = kept
        else:
            hooks.pop(ev, None)
    script = base / "hooks" / HINT_MARK
    if args.state == "on":
        # The script is copied beside the settings file so the reminder does not depend on where
        # the skill lives, and the command is written so that a missing script is silence, not an error.
        script.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SKILL_DIR / "scripts" / HINT_MARK, script)
        py, sp = posix(sys.executable), posix(script)
        loader = (f"import os,sys; p=r'{sp}'; "
                  "exec(compile(open(p,encoding='utf-8').read(),p,'exec')) if os.path.exists(p) else None")
        command = f'"{py}" -I -c "{loader}" {int(args.every)} {slash_command()}'
        for ev in ("SessionStart", "UserPromptSubmit"):
            hooks.setdefault(ev, []).append({"hooks": [{"type": "command", "command": command}]})
    elif script.exists():
        script.unlink()
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    if settings.is_file():
        shutil.copyfile(settings, settings.with_name("settings.json.before-spite-hint"))
    base.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    if args.state == "on":
        print(f"reminder on: shown when a session starts and every {int(args.every)} prompts")
        print(f"it will read: SPITE is set up here. Type {slash_command()} help for help.")
        print(f"settings file: {settings}   (the earlier version is kept beside it)")
        print("It takes effect in sessions started after this.")
    else:
        print(f"reminder off ({settings})")


def cmd_verify(args):
    """Check a published provenance folder. For anyone; needs no access to either repo."""
    ok, lines = sc.verify_bundle(args.path)
    print(f"provenance check of {args.path}")
    print("\n".join(lines))
    print("RESULT: " + ("consistent" if ok else "PROBLEMS FOUND"))
    sys.exit(0 if ok else 1)


def cmd_status(ctx):
    print(f"target: {ctx.cfg.get('target_name')}  ({ctx.target})")
    print(f"dirty:  {ctx.dirty}\nclean:  {ctx.clean}")
    frozen = ctx.frozen_hash()
    print(f"frozen spec: {frozen or '(not frozen)'}")
    rows = [(rec["date"], side, rec["step"], rec["result"])
            for repo, side in ((ctx.dirty, "dirty"), (ctx.clean, "clean")) for rec in records_of(repo)]
    for date, side, step, result in sorted(rows):
        print(f"  {date}  {side:5}  {step:14} {result}")
    pending = pending_questions(ctx) if ctx.clean.exists() else []
    if pending:
        print(f"open clean-side questions: {len(pending)}")


def cmd_demo(ctx):
    """Set up the made-up demo target so the whole pipeline can be exercised safely."""
    a = ctx.args
    a.name, a.url, a.codename, a.test_cmd = "Pebblecount", "https://pebblecount.example", a.codename or "tally", None
    a.clean_root = a.dirty_model = a.clean_model = None
    a.mark = ["pebblecount.example", "Streaks that stick", "pebble", "Skip Stone"]
    a.interop_literal = ["pebblecount-export"]
    cmd_init(ctx)
    ctx._cfg = None
    a.core_in_code = "Made-up product: a small streak-tracking service whose whole value is its code."
    a.weeks = 1
    a.no_strong_oss = "Made-up product; nothing to compare."
    a.paying_users = "None; this target exists only to exercise the pipeline."
    a.quiet_next = True
    cmd_gate(ctx)
    cfg = load_json(ctx.dirty / "spite.json")  # a made-up target has no terms; its pages ship with the skill
    cfg["survey_mode"], cfg["terms_note"] = "saved", "Built-in demo target: made up, surveyed from bundled pages."
    save_json(ctx.dirty / "spite.json", cfg)
    commit(ctx.dirty, "terms: demo target, saved material only")
    ctx._cfg = None
    demo = posix(ASSETS / "demo-target")
    print(f"\ndemo ready. Run the steps:\n  spite survey {ctx.target} --from-dir \"{demo}\"\n"
          f"  spite partition {ctx.target}\n  spite review {ctx.target}\n  spite freeze {ctx.target}\n"
          f"  spite build {ctx.target}\n  spite test {ctx.target}\n"
          f"  spite emit {ctx.target} --name Tallymark --license Apache-2.0")


# ----------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(prog="spite", description="SPITE clean-room pipeline runner")
    ap.add_argument("--root", help="folder that holds the two repos (default: $SPITE_ROOT or the current folder)")
    ap.add_argument("--model", help="model for the sessions this command launches")
    ap.add_argument("--budget", type=float, help="spend cap in USD for each session")
    ap.add_argument("--timeout", type=int, default=3600, help="seconds before a session is stopped")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("target")
        p.set_defaults(fn=fn)
        return p

    p = add("init", cmd_init, "create the dirty and clean repos for a target")
    p.add_argument("--name", help="the target's display name (added to the marks list)")
    p.add_argument("--url", help="the target's public site")
    p.add_argument("--mark", action="append", help="another name, domain or slogan that must never cross the gate (repeatable)")
    p.add_argument("--codename", help="neutral name for the clean repo (default: one is picked for you)")
    p.add_argument("--interop-literal", action="append",
                   help="a functional identifier (format tag, header name) allowed to cross although it contains a mark (repeatable)")
    p.add_argument("--test-cmd", help="command that runs the conformance tests from the clean repo root")
    p.add_argument("--clean-root", help="put the clean repo under a different folder from the dirty one")
    p.add_argument("--dirty-model", help="model for the dirty side's sessions")
    p.add_argument("--clean-model", help="model for the clean side's sessions (use a different one from the dirty side)")

    p = add("gate", cmd_gate, "record the four-part target gate")
    p.add_argument("--core-in-code", help="why the core value is in code, not a licence, data set, network or model")
    p.add_argument("--weeks", type=float, help="estimated weeks to build the core")
    p.add_argument("--no-strong-oss", help="what open-source equivalents exist and why none is strong")
    p.add_argument("--paying-users", help="the paying users the release would take")
    p.add_argument("--prior-exposure",
                   help="what the operator has seen of the target beyond public material (interviews, NDAs, accounts), or 'none'")

    p = add("terms", cmd_terms, "dirty side: note the target's terms of use, or record the operator's decision on them")
    p.add_argument("--decision", choices=["live", "saved", "stop"],
                   help="the operator's decision after reading the terms: survey the live site, saved copies only, or stop")
    p.add_argument("--note", help="who read the terms and what they concluded (recorded)")
    p = add("survey", cmd_survey, "dirty side: gather public material into an observation log")
    p.add_argument("--from-dir", help="survey saved public material in this folder instead of the live web")
    p.add_argument("--set-aside", action="store_true",
                   help="take the earlier survey out of use (kept on record, moved where no role can read it)")
    p.add_argument("--note", help="with --set-aside: why (recorded)")
    add("partition", cmd_partition, "dirty side: turn the log into a draft functional spec")
    p = add("review", cmd_review, "dirty side: strip expressive material and lint the spec")
    p.add_argument("--lint-only", action="store_true", help="rerun the crossing checks without a new review session")
    p.add_argument("--skip-audit", action="store_true", help="go without the second, adversarial reader (needs --note)")
    p.add_argument("--note", help="a decision from the operator for the reviewer to apply (recorded)")
    p = add("freeze", cmd_freeze, "freeze and hash the reviewed spec and carry it across the gate")
    p.add_argument("--refreeze", action="store_true")
    p.add_argument("--accept-identifiable", action="store_true",
                   help="freeze although a blind reader can name the target from the spec (needs --note)")
    p.add_argument("--no-blind-test", action="store_true",
                   help="freeze without asking a blind reader what the spec describes (needs --note)")
    p.add_argument("--note", help="the operator's reason, recorded")
    p = add("build", cmd_build, "clean side: build from the frozen spec in a fresh isolated session")
    p.add_argument("--fix", action="store_true", help="fix the build against the last failing test report")
    p = add("test", cmd_test, "clean side: write and run conformance tests, then the provenance scan")
    p.add_argument("--write-tests", action="store_true", help="have the tester write tests again")
    p.add_argument("--fix-tests", action="store_true",
                   help="have the tester recheck failing tests against the spec and reviewed answers")
    p.add_argument("--setup", action="store_true", help="run the repo's setup_cmd first (may use the network)")
    p.add_argument("--unsandboxed", action="store_true",
                   help="run build-side code directly on this machine; recorded in the test record and PROVENANCE.md")
    add("qa", cmd_qa, "carry open clean-side questions to the reviewer and reviewed answers back")
    p = add("scan", cmd_scan, "run the provenance scan on the clean repo")
    p.add_argument("--accept", metavar="KIND", help="the operator accepts findings of this kind that match --match")
    p.add_argument("--match", help="text from the finding's detail that identifies it")
    p.add_argument("--note", help="why the finding is not a leak (recorded)")
    p = add("emit", cmd_emit, "prepare the release locally: licence, README, provenance bundle")
    p.add_argument("--name", required=True, help="the new product name (no reference to the target)")
    p.add_argument("--license", required=True, help="AGPL-3.0 or Apache-2.0")
    p.add_argument("--deps-reviewed", help="who checked the third-party dependencies' licences and what they found (recorded)")
    p.add_argument("--no-name-check", action="store_true", help="do not look the new name up in package registries")
    p = add("config", cmd_config, "show or change how the clean repo's tests are run and which model each side uses")
    p.add_argument("--sandbox", choices=["docker", "custom", "none"], help="where build-side code is executed")
    p.add_argument("--image", help="container image for the docker sandbox")
    p.add_argument("--test-cmd", help="command that runs the conformance tests from the clean repo root")
    p.add_argument("--setup-cmd", help="command that installs dependencies (run with network, before the tests)")
    p.add_argument("--dirty-model", help="model for the dirty side's sessions")
    p.add_argument("--clean-model", help="model for the clean side's sessions")
    p.add_argument("--sign-with", metavar="SSH_PUBLIC_KEY",
                   help="sign every commit the runner makes with this SSH key ('off' to stop)")
    add("status", cmd_status, "show the gate records for a target")
    add("update", cmd_update, "start an update of a released product: re-survey, spec delta, delta build")
    p = add("scout", cmd_scout, "sift a list of candidate products against the target gate; starts no survey")
    p.add_argument("--list", help="file with one candidate per line: name, optionally a site and a note")
    p.add_argument("--limit", type=int, help="look at no more than this many candidates in this run")
    p.add_argument("--batch", type=int, default=6, help="candidates per session (default 6)")
    p = add("remote", cmd_remote, "show or set the destination repo for a target's dirty and clean repos (never pushes)")
    p.add_argument("--dirty", help="destination for the dirty repo; must be private")
    p.add_argument("--clean", help="destination for the clean repo")
    p.add_argument("--dirty-is-private", action="store_true",
                   help="the operator states the dirty destination is private (for hosts that cannot be checked)")
    p.add_argument("--dirty-on-branch", nargs="?", const="provenance", metavar="BRANCH",
                   help="keep research snapshots on a branch of the clean destination repo, in a provenance/ folder "
                        "(default branch name: provenance)")
    p.add_argument("--auto-push", choices=["on", "off"],
                   help="after each step, push both sides to their private repos (an outside, dated copy of every record)")
    add("backup", cmd_backup, "push both sides to the private destinations the operator chose")
    p = add("demo", cmd_demo, "set up the built-in made-up target for a dry run")
    p.add_argument("--codename")

    h = sub.add_parser("help", help="what SPITE is and how to use it, in plain words")
    h.set_defaults(fn=cmd_help, standalone=True)
    h = sub.add_parser("doctor", help="check that this machine has what the pipeline needs")
    h.set_defaults(fn=cmd_doctor, standalone=True)
    h = sub.add_parser("package", help="write the skill as one file (spite.skill) for uploading or sharing")
    h.add_argument("--out", help="folder to write spite.skill into (default: the current folder)")
    h.set_defaults(fn=cmd_package, standalone=True)
    h = sub.add_parser("hint", help="show 'type /spite help for help' on screen at session start and every N prompts")
    h.add_argument("state", nargs="?", choices=["on", "off"], help="omit to see whether it is on")
    h.add_argument("--every", type=int, default=10, help="show it every N prompts (default 10; 0 = only at session start)")
    h.add_argument("--user", action="store_true", help="put it in your user settings, for every session, instead of this folder")
    h.set_defaults(fn=cmd_hint, standalone=True)
    v = sub.add_parser("verify", help="check a published provenance/ folder; needs nothing else")
    v.add_argument("path", help="the provenance folder, or the repo that contains it")
    v.set_defaults(fn=cmd_verify, standalone=True)

    args = ap.parse_args(argv)
    if getattr(args, "standalone", False):
        args.fn(args)
        return
    ctx = Ctx(args)
    try:
        args.fn(ctx)
    finally:
        if args.cmd not in ("backup", "status", "remote", "demo", "init", "scout"):
            ctx._cfg = None
            anchor(ctx)


if __name__ == "__main__":
    main()
