#!/usr/bin/env python3
"""Self-test for the SPITE runner's checks. Launches no sessions and touches no network.

Each check gets a case that should pass and a planted case that must fail. Run after any change:

    python -I selftest_runner.py
"""
import argparse
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import spite  # noqa: E402
import spite_checks as sc  # noqa: E402

results = []


def check(name, ok):
    results.append((name, bool(ok)))
    print(("ok   " if ok else "FAIL ") + name)


def run(argv):
    """Run a runner command; return (exited_with_error, output)."""
    out = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            spite.main(argv)
        except SystemExit as exc:
            code = exc.code or 0
    return code != 0, out.getvalue()


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


SPEC = """# Spec

## 1. Purpose
Counts things.

## 4. Interfaces
A command-line tool. `--count N` sets the number.

## 5. Interchange formats
None is published.

## 8. Conformance checklist
C1 (core). Given nothing, when counting, then the answer is 0.
C2 (core). Given `--count 3`, then the answer is 3.
C3. Given a negative count, then the tool refuses.

## 9. Interop literals
none
"""

root = Path(tempfile.mkdtemp(prefix="spite-selftest-"))
R = ["--root", str(root)]
try:
    # ---- workspace
    failed, _ = run(R + ["init", "widget", "--name", "Widgetly", "--mark", "widgetly.example", "--codename", "plain"])
    check("init creates both repos", not failed and (root / "spite-widget-dirty").is_dir() and (root / "spite-plain").is_dir())
    failed, out = run(R + ["init", "gadget", "--name", "Gadgeto", "--codename", "gadgeto-two"])
    check("a codename that names the target is refused", failed)
    run(R + ["gate", "widget", "--core-in-code", "x", "--weeks", "1", "--no-strong-oss", "x", "--paying-users", "x"])
    failed, out = run(R + ["survey", "widget"])
    check("survey refuses before the terms are dealt with", failed and "terms" in out)
    run(R + ["terms", "widget", "--decision", "saved", "--note", "selftest"])
    failed, out = run(R + ["survey", "widget"])
    check("a live survey is refused under a saved-copies decision", failed)

    ctx = spite.Ctx(argparse.Namespace(target="widget", root=str(root), model=None, budget=None, timeout=60))
    dirty, clean = ctx.dirty, ctx.clean

    # ---- dirty side, fabricated (no sessions in a self-test)
    (dirty / "survey" / "observations.md").write_text(
        "Widgetly counts things. Its config key is `widget_secret_mode` and its flag is `--widget-turbo`.\n"
        "On a bad count it says \"Whoa there, that count is no good\".\n"
        "Walk the list one item at a time starting from the first and stop when the counter overflows.\n",
        encoding="utf-8")
    (dirty / "survey" / "sources.csv").write_text(
        "id,url,title,accessed,kind,access\nS1,https://widgetly.example/docs,Docs,2026-01-01,docs,public\n", encoding="utf-8")
    spite.write_record(dirty, "survey", "dirty", [], ["survey/observations.md"], {"sources": 1}, "pass")
    (dirty / "spec" / "draft-spec.md").write_text(SPEC, encoding="utf-8")
    (dirty / "spec" / "reviewed-spec.md").write_text(SPEC, encoding="utf-8")

    # ---- crossing checks
    f, _ = spite.lint_crossing(ctx, SPEC)
    check("a clean spec passes the crossing checks", not f and not spite.spec_shape_failures(SPEC))
    f, _ = spite.lint_crossing(ctx, SPEC + "\nIt works like Widgetly.\n")
    check("planted: the target's name fails the crossing checks", any("names the target" in x for x in f))
    f, _ = spite.lint_crossing(ctx, SPEC + "\nSee https://widgetly.example/docs for more.\n")
    check("planted: a source address fails", any("source address" in x for x in f))
    f, _ = spite.lint_crossing(ctx, SPEC + "\nWalk the list one item at a time starting from the first and stop.\n")
    check("planted: eight copied words fail", any("consecutive words" in x for x in f))
    f, _ = spite.lint_crossing(ctx, SPEC.replace("## 9. Interop literals\nnone", "## 9. Interop literals\n- `widgetly-export`"))
    check("planted: an unapproved interop literal fails", any("not approved" in x for x in f))
    f, _ = spite.lint_crossing(ctx, SPEC + "\nThe file name needs a human decision.\n")
    check("planted: an open decision fails", any("open decision" in x for x in f))
    check("planted: a spec with no checklist fails", spite.spec_shape_failures("# Spec\n## 5. Interchange formats\nnone\n"))
    check("planted: a spec with no core item fails", spite.spec_shape_failures(SPEC.replace(" (core)", "")))
    check("planted: a spec with no Interchange section fails",
          spite.spec_shape_failures(SPEC.replace("## 5. Interchange formats", "## 5. Other")))

    # ---- freeze
    spite.write_record(dirty, "review", "dirty", ["spec/draft-spec.md"], ["spec/reviewed-spec.md"],
                       {"second_reader": "skipped by the operator"}, "pass")
    failed, out = run(R + ["freeze", "widget", "--no-blind-test"])
    check("freeze without the blind reader needs the operator's reason", failed)
    (clean / "stray.txt").write_text("put here by hand", encoding="utf-8")
    failed, out = run(R + ["freeze", "widget", "--no-blind-test", "--note", "selftest"])
    check("planted: a hand-made change in the clean repo stops the runner", failed and "did not make" in out)
    (clean / "stray.txt").unlink()
    failed, out = run(R + ["freeze", "widget", "--no-blind-test", "--note", "selftest"])
    check("freeze carries the spec across", not failed and (clean / "spec" / "frozen-spec.md").is_file())

    # ---- clean side, fabricated and committed the way the runner commits
    (clean / "INTERFACE.md").write_text("Run: python counter.py --count N\n", encoding="utf-8")
    (clean / "counter.py").write_text("import sys\nprint(0)\n", encoding="utf-8")
    (clean / "tests").mkdir(exist_ok=True)
    (clean / "tests" / "test_counter.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n    def test_c1_zero(self):\n        self.assertEqual(0, 0)\n",
        encoding="utf-8")
    git(clean, "add", "-A"); git(clean, "commit", "-q", "-m", "build")

    cov = sc.coverage(SPEC, clean / "tests")
    check("planted: a core requirement with no test is reported", cov["core_without_a_test"] == ["C2"])
    check("non-core gaps are listed separately", cov["other_without_a_test"] == ["C3"])

    cfg_path = clean / "spite.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["sandbox"] = {"kind": "none"}
    cfg["test_cmd"] = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    git(clean, "add", "-A"); git(clean, "commit", "-q", "-m", "config")
    failed, out = run(R + ["test", "widget"])
    check("planted: with no sandbox the test run is refused", failed and "--unsandboxed" in out)
    failed, out = run(R + ["test", "widget", "--unsandboxed"])
    check("planted: tests pass but the ship gate fails on the uncovered core requirement", failed and "C2" in out)
    rec = spite.latest_record(clean, "test")
    check("the test record says the run was not sandboxed", rec["checks"]["sandboxed"] is False)
    (clean / "tests" / "test_counter.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n    def test_c1_zero(self):\n        self.assertEqual(0, 0)\n"
        "    def test_c2_three(self):\n        self.assertEqual(3, 3)\n", encoding="utf-8")
    git(clean, "add", "-A"); git(clean, "commit", "-q", "-m", "tests")
    failed, out = run(R + ["test", "widget", "--unsandboxed"])
    check("the ship gate passes once every core requirement has a passing test", not failed)

    # ---- scan
    def scan_kinds():
        return sorted({f["kind"] for f in spite.scan(ctx)["findings"]})
    check("a clean repo scans clean", scan_kinds() == [])
    (clean / "notes.md").write_text("Set widget_secret_mode and pass --widget-turbo.\n", encoding="utf-8")
    check("planted: survey-side identifiers in the clean repo are found", "survey-identifier" in scan_kinds())
    (clean / "notes.md").write_text("It prints: Whoa there, that count is no good\n", encoding="utf-8")
    check("planted: short survey-side wording is found", "survey-wording" in scan_kinds())
    (clean / "notes.md").write_text("Inspired by Widgetly.\n", encoding="utf-8")
    check("planted: the target's name is found", "mark" in scan_kinds())
    (clean / "notes.md").write_text("Use --count to set the number.\n", encoding="utf-8")
    check("identifiers that crossed in the frozen spec are not findings", scan_kinds() == [])
    (clean / "notes.md").unlink()

    # ---- record chain
    prov = root / "published"
    (prov / "records").mkdir(parents=True)
    shutil.copyfile(clean / "spec" / "frozen-spec.md", prov / "frozen-spec.md")
    shutil.copyfile(clean / "spec" / "frozen-spec.sha256", prov / "frozen-spec.sha256")
    for repo, side in ((dirty, "dirty"), (clean, "clean")):
        for p in sorted((repo / "records").glob("*.json")):
            shutil.copyfile(p, prov / "records" / f"{side}-{p.name}")
    ok, lines = sc.verify_bundle(prov)
    check("an untouched record bundle verifies", ok)
    # Pick a record in the middle of the chain. The last record of a chain can be cut off without
    # trace from inside the chain; what pins it is the release record that follows it.
    victim = next(p for p in sorted((prov / "records").glob("clean-*test*.json"))
                  if b'"result": "fail"' in p.read_bytes())
    original = victim.read_bytes()
    victim.write_bytes(original.replace(b'"result": "fail"', b'"result": "pass"', 1))
    ok, _ = sc.verify_bundle(prov)
    check("planted: an edited record breaks the chain", not ok)
    victim.write_bytes(original)
    hidden = victim.with_suffix(".hidden")
    victim.rename(hidden)
    ok, _ = sc.verify_bundle(prov)
    check("planted: a removed record breaks the chain", not ok)
    hidden.rename(victim)
    spec_file = prov / "frozen-spec.md"
    spec_bytes = spec_file.read_bytes()
    spec_file.write_bytes(spec_bytes + b"\nextra requirement\n")
    ok, _ = sc.verify_bundle(prov)
    check("planted: an edited frozen spec is caught", not ok)
    spec_file.write_bytes(spec_bytes)
    ok, _ = sc.verify_bundle(prov)
    check("the bundle verifies again once restored", ok)

    # ---- update and scout
    failed, out = run(R + ["update", "widget"])
    check("planted: an update is refused when there is no release to update", failed and "no release" in out)
    failed, out = run(R + ["scout", "longlist"])
    check("planted: a new scout is refused without a list of candidates", failed and "--list" in out)
    failed, out = run(R + ["backup", "widget"])
    check("planted: a backup is refused when the operator has chosen no destination", failed and "stays local" in out)
    failed, out = run(R + ["remote", "widget", "--dirty", "https://example.invalid/a/b.git"])
    check("planted: a dirty destination that cannot be checked needs the operator's word", failed)

    # ---- the on-screen reminder
    import os
    here = os.getcwd()
    spot = root / "hint-folder"
    (spot / ".claude").mkdir(parents=True)
    (spot / ".claude" / "settings.json").write_text('{"permissions": {"deny": ["WebSearch"]}}', encoding="utf-8")
    os.chdir(spot)
    try:
        run(["hint", "on", "--every", "3"])
        run(["hint", "on", "--every", "3"])
        data = json.loads((spot / ".claude" / "settings.json").read_text(encoding="utf-8"))
        check("the reminder installs once and keeps the folder's other settings",
              len(data["hooks"]["SessionStart"]) == 1 and len(data["hooks"]["UserPromptSubmit"]) == 1
              and data["permissions"] == {"deny": ["WebSearch"]})
        hook = spot / ".claude" / "hooks" / "spite_hint.py"

        def fire(payload, script=hook):
            return subprocess.run([sys.executable, "-I", str(script), "3"], input=payload, capture_output=True, text=True)
        r = fire('{"hook_event_name": "SessionStart", "session_id": "selftest"}')
        check("the reminder shows at session start", "/spite help" in r.stdout and r.returncode == 0)
        outs = [fire('{"hook_event_name": "UserPromptSubmit", "session_id": "selftest-%s", "prompt": "hi"}' % os.getpid()).stdout
                for _ in range(3)]
        check("the reminder shows on every third prompt only", [bool(o.strip()) for o in outs] == [False, False, True])
        r = fire("not json")
        check("planted: bad input leaves the reminder silent and never blocks a prompt", r.returncode == 0 and not r.stdout.strip())
        run(["hint", "off"])
        data = json.loads((spot / ".claude" / "settings.json").read_text(encoding="utf-8"))
        check("turning the reminder off restores the settings", "hooks" not in data and not hook.exists())
    finally:
        os.chdir(here)

    # ---- blind test cannot be retried until it passes
    spite.write_record(dirty, "blind-test", "dirty", [], [], {"identified": True}, "fail")
    failed, out = run(R + ["freeze", "widget", "--refreeze"])
    check("planted: after a blind reader named the target, freeze refuses until a new review", failed and "blind reader" in out)
finally:
    shutil.rmtree(root, ignore_errors=True)

bad = [name for name, ok in results if not ok]
print(f"\n{len(results) - len(bad)} of {len(results)} checks passed")
sys.exit(1 if bad else 0)
