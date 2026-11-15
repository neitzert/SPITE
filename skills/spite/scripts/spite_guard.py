#!/usr/bin/env python3
"""SPITE guard: a PreToolUse hook that confines one session to one repo.

Usage (as a hook command):  python -I spite_guard.py <policy.json>

The hook reads the tool call on stdin and decides from the policy file alone.
It is default-deny: a tool that the policy does not list is refused, which is
what keeps shell tools, web tools, MCP tools and sub-sessions off the clean
side without having to enumerate them. Any error also refuses the call, so a
broken guard never silently opens the gate.

Policy keys:
  role         label written to the audit log
  root         repo this session is confined to (absolute, or relative to the
               project dir for the copy installed inside a repo)
  tools        tool names this role may call
  read_dirs    directories (relative to root) the role may read; "." = all
  read_files   single files the role may read in addition
  write_dirs   directories the role may write
  write_files  single files the role may write in addition
  deny_dirs    directories no rule above can open
  write_deny_dirs  directories (or files) the role may read but never write
  spec_lock    optional {"path": ..., "sha256": ...} or {"path": ...,
               "sha256_file": ...}; when the file's hash differs, every call
               is refused. This is the "no build without the frozen spec" check.
  log          JSONL audit log (one line per decision, no file contents)
"""
import datetime
import hashlib
import json
import os
import sys

READ_TOOLS = {"Read", "Glob", "Grep"}
WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}
PATH_KEYS = ("file_path", "notebook_path", "path")


def norm(p):
    return os.path.normcase(os.path.realpath(p))


def inside(path, base):
    path, base = norm(path), norm(base)
    try:
        return os.path.commonpath([path, base]) == base
    except ValueError:  # different drives
        return False


def canon(p):
    """Form used for comparisons: forward slashes, and lower case where the OS ignores case."""
    return os.path.normcase(str(p)).replace("\\", "/").strip("/")


def rel(path, root):
    """Path relative to root, in its real spelling (for messages and the log)."""
    return os.path.relpath(os.path.realpath(path), os.path.realpath(root)).replace("\\", "/")


def under(relpath, dirs):
    relpath = canon(relpath)
    for d in dirs:
        d = canon(d)
        if d in ("", "."):
            return True
        if relpath == d or relpath.startswith(d + "/"):
            return True
    return False


def listed(relpath, files):
    return canon(relpath) in {canon(f) for f in files}


def static_prefix(pattern):
    """Directory part of a glob pattern before its first wildcard."""
    parts = []
    for seg in pattern.replace("\\", "/").split("/"):
        if any(c in seg for c in "*?[{"):
            break
        parts.append(seg)
    return "/".join(parts)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def check_spec_lock(policy, root):
    lock = policy.get("spec_lock")
    if not lock:
        return None
    path = os.path.join(root, lock["path"])
    want = lock.get("sha256")
    if not want and lock.get("sha256_file"):
        with open(os.path.join(root, lock["sha256_file"]), encoding="utf-8") as fh:
            want = fh.read().split()[0]
    if not os.path.isfile(path):
        return "frozen spec is missing; nothing may run on the clean side without it"
    if sha256(path) != (want or "").strip().lower():
        return "frozen spec hash mismatch; refusing to work from a spec that is not the frozen one"
    return None


def decide(policy, root, call):
    tool = call.get("tool_name", "")
    args = call.get("tool_input") or {}
    cwd = call.get("cwd") or root

    if tool not in policy.get("tools", []):
        return False, f"tool {tool} is not available to the {policy.get('role')} role", tool

    lock_problem = check_spec_lock(policy, root)
    if lock_problem:
        return False, lock_problem, ""

    if tool not in READ_TOOLS and tool not in WRITE_TOOLS:
        target = args.get("url") or args.get("query") or ""
        return True, "allowed tool", str(target)[:300]

    def resolve(p):
        return p if os.path.isabs(p) else os.path.join(cwd, p)

    deny_dirs = policy.get("deny_dirs", [])
    writing = tool in WRITE_TOOLS
    dirs = policy.get("write_dirs" if writing else "read_dirs", [])
    files = policy.get("write_files" if writing else "read_files", [])

    # Search tools take a base directory plus a pattern; both are checked.
    if tool in ("Glob", "Grep"):
        for key in ("pattern", "glob"):
            value = str(args.get(key) or "")
            if tool == "Grep" and key == "pattern":
                continue  # a regex, not a path
            if ".." in value.replace("\\", "/").split("/"):
                return False, "search patterns may not climb out with '..'", value
        base = args.get("path") or cwd
        if tool == "Glob":
            prefix = static_prefix(str(args.get("pattern") or ""))
            if prefix:
                base = prefix if os.path.isabs(prefix) else os.path.join(resolve(base), prefix)
        base = resolve(str(base))
        if not inside(base, root):
            return False, "search base is outside this repo", str(base)
        r = rel(base, root)
        if under(r, deny_dirs):
            return False, f"{r} is closed to every role", r
        if os.path.isfile(base):
            ok = under(r, dirs) or listed(r, files)
        else:
            ok = under(r, dirs)
        if not ok:
            allowed = ", ".join(dirs) or "(none)"
            return False, f"the {policy.get('role')} role may only search inside: {allowed}. Pass one of them as the search path.", r
        return True, "search inside allowed directory", r

    target = None
    for key in PATH_KEYS:
        if args.get(key):
            target = resolve(str(args[key]))
            break
    if target is None:
        return False, "no path in the tool call", ""
    if not inside(target, root):
        return False, "path is outside this repo; this session is confined to it", str(target)
    r = rel(target, root)
    if under(r, deny_dirs):
        return False, f"{r} is closed to every role", r
    if writing and under(r, policy.get("write_deny_dirs", [])):
        return False, f"{r} is read-only for the {policy.get('role')} role", r
    if under(r, dirs) or listed(r, files):
        return True, "path inside allowed scope", r
    verb = "write" if writing else "read"
    allowed = ", ".join(list(dirs) + list(files)) or "(nothing)"
    return False, f"the {policy.get('role')} role may not {verb} {r}. Allowed: {allowed}", r


def main():
    policy_path = sys.argv[1]
    with open(policy_path, encoding="utf-8") as fh:
        policy = json.load(fh)
    call = json.load(sys.stdin)

    root = policy.get("root", ".")
    if not os.path.isabs(root):
        base = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(policy_path))))
        root = os.path.join(base, root)

    allowed, reason, target = decide(policy, root, call)

    log = policy.get("log")
    if log:
        if not os.path.isabs(log):
            log = os.path.join(root, log)
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "role": policy.get("role"),
                "tool": call.get("tool_name"),
                "decision": "allow" if allowed else "deny",
                "reason": reason,
                "target": target,
            }) + "\n")

    if allowed:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": f"SPITE guard: {reason}",
        }}))
        return 0
    sys.stderr.write(f"SPITE guard refused this call: {reason}\n")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # fail closed
        sys.stderr.write(f"SPITE guard error, refusing the call: {exc}\n")
        sys.exit(2)
