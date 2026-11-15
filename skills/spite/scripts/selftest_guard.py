import json, subprocess, sys, tempfile, os, pathlib
guard = sys.argv[1]
root = pathlib.Path(tempfile.mkdtemp())
for d in ("spec", "tests", "src", ".claude", "records"):
    (root / d).mkdir()
(root / "INTERFACE.md").write_text("x"); (root / "spec" / "s.md").write_text("x")
outside = pathlib.Path(tempfile.mkdtemp()) / "o.txt"; outside.write_text("x")
policy = {"role": "tester", "root": str(root), "tools": ["Read", "Write", "Edit", "Glob", "Grep"],
          "read_dirs": ["spec", "tests"], "read_files": ["INTERFACE.md"], "write_dirs": ["tests"], "write_files": [],
          "write_deny_dirs": [], "deny_dirs": [".git", ".claude", ".spite", "records"]}
pp = root.parent / (root.name + "-policy.json"); pp.write_text(json.dumps(policy))
cases = [
    ("Read", {"file_path": str(root / "INTERFACE.md")}, True),
    ("Read", {"file_path": "interface.md"}, os.name == "nt"),
    ("Read", {"file_path": "spec/s.md"}, True),
    ("Read", {"file_path": "src/a.py"}, False),
    ("Read", {"file_path": str(outside)}, False),
    ("Read", {"file_path": "spec/../src/a.py"}, False),
    ("Read", {"file_path": "../" + root.name + "-policy.json"}, False),
    ("Read", {"file_path": ".claude/settings.json"}, False),
    ("Write", {"file_path": "tests/test_a.py"}, True),
    ("Write", {"file_path": "spec/s.md"}, False),
    ("Write", {"file_path": "records/x.json"}, False),
    ("Glob", {"pattern": "**/*.py"}, False),
    ("Glob", {"pattern": "**/*.py", "path": "tests"}, True),
    ("Glob", {"pattern": "tests/**/*.py"}, True),
    ("Glob", {"pattern": "../**/*"}, False),
    ("Glob", {"pattern": "src/**"}, False),
    ("Glob", {"pattern": str(outside.parent).replace("\\", "/") + "/*"}, False),
    ("Grep", {"pattern": "secret"}, False),
    ("Grep", {"pattern": "secret", "path": "spec"}, True),
    ("Grep", {"pattern": "secret", "path": "src"}, False),
    ("Bash", {"command": "cat ../x"}, False),
    ("WebFetch", {"url": "https://example.com"}, False),
    ("mcp__mempalace__mempalace_search", {"query": "x"}, False),
    ("Agent", {"prompt": "x"}, False),
]
bad = 0
for tool, args, want in cases:
    call = {"tool_name": tool, "tool_input": args, "cwd": str(root)}
    r = subprocess.run([sys.executable, "-I", guard, str(pp)], input=json.dumps(call), capture_output=True, text=True)
    got = r.returncode == 0
    flag = "ok  " if got == want else "FAIL"
    bad += got != want
    print(flag, tool, json.dumps(args)[:70], "->", "allow" if got else "deny")
# fail closed on a broken policy and on garbage input
r = subprocess.run([sys.executable, "-I", guard, str(root / "missing.json")], input="{}", capture_output=True, text=True)
print("ok  " if r.returncode == 2 else "FAIL", "missing policy -> deny"); bad += r.returncode != 2
r = subprocess.run([sys.executable, "-I", guard, str(pp)], input="not json", capture_output=True, text=True)
print("ok  " if r.returncode == 2 else "FAIL", "garbage input -> deny"); bad += r.returncode != 2
# spec lock
policy["spec_lock"] = {"path": "spec/s.md", "sha256": "0" * 64}; pp.write_text(json.dumps(policy))
r = subprocess.run([sys.executable, "-I", guard, str(pp)], input=json.dumps({"tool_name": "Read", "tool_input": {"file_path": "spec/s.md"}, "cwd": str(root)}), capture_output=True, text=True)
print("ok  " if r.returncode == 2 else "FAIL", "spec hash mismatch -> deny everything:", r.stderr.strip()[:90]); bad += r.returncode != 2
print("failures:", bad)
