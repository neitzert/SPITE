"""Checks used by the SPITE runner that need no session and no repo access beyond files.

Kept apart from spite.py so that `verify` can be read and audited on its own: it is the part a
third party runs against a published provenance/ folder. Standard library only.
"""
import hashlib
import json
import re
import urllib.error
import urllib.request
from pathlib import Path


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------ requirement coverage

CHECK_ID = re.compile(r"^\W*(C\d+)\b")
CITED_ID = re.compile(r"(?<![A-Za-z0-9])[cC](\d+)(?![0-9A-Za-z])")


def checklist_ids(spec_text):
    """Conformance checklist ids in a spec: {"C4": True if the item is marked (core)}."""
    ids = {}
    for line in spec_text.splitlines():
        m = CHECK_ID.match(line)
        if m:
            ids[m.group(1)] = "(core)" in line.lower()
    return ids


def cited_ids(tests_dir):
    """Checklist ids that the tests cite, in names, docstrings or comments."""
    cited = set()
    tests_dir = Path(tests_dir)
    if tests_dir.is_dir():
        for p in tests_dir.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                cited.update(f"C{n}" for n in CITED_ID.findall(text))
    return cited


def coverage(spec_text, tests_dir):
    ids = checklist_ids(spec_text)
    cited = cited_ids(tests_dir)
    order = lambda i: int(i[1:])
    return {
        "requirements": len(ids),
        "core": sorted((i for i, core in ids.items() if core), key=order),
        "core_without_a_test": sorted((i for i, core in ids.items() if core and i not in cited), key=order),
        "other_without_a_test": sorted((i for i, core in ids.items() if not core and i not in cited), key=order),
        "cited_but_not_in_spec": sorted(cited - set(ids), key=order),
    }


# ----------------------------------------------------------------- structural scan

CODE_SPAN = re.compile(r"`([^`\n]{2,80})`")
SHAPED = re.compile(
    r"--[a-z][a-z0-9]*(?:-[a-z0-9]+)*"            # command-line flags
    r"|/[A-Za-z0-9_{}\-]+(?:/[A-Za-z0-9_{}\-.]+)+"  # routes and paths
    r"|[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+"    # snake_case
    r"|[a-z]+[a-z0-9]*[A-Z][A-Za-z0-9]+"          # camelCase
    r"|[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+"             # CONSTANT_CASE
)
QUOTED = re.compile(r"[\"“]([^\"”\n]{12,200})[\"”]")


def survey_identifiers(texts):
    """Identifier-shaped strings in survey material: flags, routes, field names, config keys."""
    found = set()
    for text in texts:
        for span in CODE_SPAN.findall(text):
            span = span.strip()
            if " " not in span and len(span) >= 4:
                found.add(span)
            found.update(SHAPED.findall(span))
        found.update(SHAPED.findall(text))
    return {f for f in found if len(f) >= 6}


def survey_strings(texts):
    """Quoted wording in survey material of three words or more (messages, labels, slogans)."""
    found = set()
    for text in texts:
        for s in QUOTED.findall(text):
            s = " ".join(s.split())
            if len(s.split()) >= 3:
                found.add(s)
        for s in CODE_SPAN.findall(text):
            s = " ".join(s.split())
            if len(s.split()) >= 3:
                found.add(s)
    return found


def structural_findings(clean_files, identifiers, strings, allowed_text):
    """Survey-side identifiers and wording that reached the clean repo without passing the gate.

    clean_files: iterable of (relative path, text). allowed_text: everything that legitimately
    crossed (the frozen spec, reviewed answers, approved interop literals) as one string.
    """
    allowed_low = allowed_text.lower()
    ids = sorted(i for i in identifiers if i not in allowed_text)
    strs = sorted(s for s in strings if s.lower() not in allowed_low)
    findings = []
    for rel, text in clean_files:
        low = text.lower()
        for ident in ids:
            pat = r"(?<![A-Za-z0-9_-])" + re.escape(ident) + r"(?![A-Za-z0-9_])"
            if re.search(pat, text):
                # A one-word flag (--date, --out) is a name two people reach independently.
                # It is reported for information; anything more specific counts.
                kind = "generic-name" if re.fullmatch(r"--[a-z0-9]+", ident) else "survey-identifier"
                findings.append({"file": rel, "kind": kind, "detail": ident})
        for s in strs:
            if s.lower() in low:
                findings.append({"file": rel, "kind": "survey-wording", "detail": s[:160]})
    return findings


# ------------------------------------------------------------------- record chain

def chain_fields(records_dir, other_records_dir=None):
    """prev_sha256 and seq for the next record on this side, and the other side's head."""
    def head(d):
        d = Path(d) if d else None
        best = None
        if d and d.is_dir():
            for p in d.glob("*.json"):
                try:
                    rec = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                key = (rec.get("seq", 0), rec.get("date", ""), p.name)
                if best is None or key > best[0]:
                    best = (key, p, rec)
        return best
    mine = head(records_dir)
    fields = {"seq": (mine[2].get("seq", 0) + 1) if mine else 1,
              "prev_sha256": sha256_file(mine[1]) if mine else None}
    if other_records_dir is not None:
        other = head(other_records_dir)
        fields["other_side_head_sha256"] = sha256_file(other[1]) if other else None
    return fields


def verify_bundle(path):
    """Check a published provenance folder. Needs nothing but that folder.

    Returns (ok, lines). Verifies the frozen spec against its stated hash, each side's record
    chain link by link, the cross-links between the sides, and that every build-side record
    names the frozen spec it worked from.
    """
    root = Path(path)
    if (root / "provenance").is_dir():
        root = root / "provenance"
    lines, problems = [], []

    def bad(msg):
        problems.append(msg)
        lines.append(f"  FAIL  {msg}")

    def good(msg):
        lines.append(f"  ok    {msg}")

    spec, sha = root / "frozen-spec.md", root / "frozen-spec.sha256"
    frozen = None
    if not spec.is_file() or not sha.is_file():
        bad("frozen-spec.md or frozen-spec.sha256 is missing")
    else:
        frozen = sha256_file(spec)
        stated = sha.read_text(encoding="utf-8").split()[0].strip().lower()
        if frozen == stated:
            good(f"frozen spec matches its stated hash {frozen[:16]}...")
        else:
            bad(f"frozen spec hash {frozen[:16]}... differs from the stated {stated[:16]}...")

    rec_dir = root / "records"
    sides = {"dirty": [], "clean": []}
    if not rec_dir.is_dir():
        bad("records/ is missing")
    else:
        for p in sorted(rec_dir.glob("*.json")):
            side = "dirty" if p.name.startswith("dirty-") else "clean" if p.name.startswith("clean-") else None
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                bad(f"{p.name} is not readable JSON")
                continue
            if side is None:
                side = rec.get("side")
            if side in sides:
                sides[side].append((rec.get("seq", 0), p, rec))
    all_hashes = {}
    for side, items in sides.items():
        items.sort(key=lambda t: (t[0], t[2].get("date", ""), t[1].name))
        for _, p, _ in items:
            all_hashes[sha256_file(p)] = p.name
    for side, items in sides.items():
        if not items:
            bad(f"no {side}-side records")
            continue
        chained = [t for t in items if "prev_sha256" in t[2]]
        if len(chained) != len(items):
            bad(f"{side}: {len(items) - len(chained)} record(s) carry no chain link")
        broken = 0
        prev = None
        for seq, p, rec in items:
            if "prev_sha256" not in rec:
                prev = sha256_file(p)
                continue
            if rec["prev_sha256"] != prev:
                broken += 1
                bad(f"{side}: {p.name} does not follow the record before it (a record was changed, removed or inserted)")
            if seq and items.index((seq, p, rec)) + 1 != seq:
                broken += 1
                bad(f"{side}: {p.name} has sequence number {seq} but is record {items.index((seq, p, rec)) + 1} in the folder")
            prev = sha256_file(p)
        if not broken:
            good(f"{side}: {len(items)} records form an unbroken chain")
        for _, p, rec in items:
            link = rec.get("other_side_head_sha256")
            if link and link not in all_hashes:
                bad(f"{side}: {p.name} points at a record of the other side that is not in this folder")
    crossed = [rec for items in sides.values() for _, _, rec in items if rec.get("other_side_head_sha256")]
    if crossed and not any(m.startswith("dirty: ") or m.startswith("clean: ") for m in problems):
        good(f"{len(crossed)} cross-links between the two sides resolve")
    if frozen:
        # A product can be updated: each update freezes a new spec. Walking the build side in
        # order, the spec in force is the one most recently received, and every record must name
        # that one. The last spec in force must be the one published here.
        named, in_force, history = 0, None, []
        for _, p, rec in sides["clean"]:
            checks = rec.get("checks") or {}
            if rec.get("step") == "spec-received":
                in_force = checks.get("sha256")
                if in_force and in_force not in history:
                    history.append(in_force)
                continue
            claimed = checks.get("spec_sha256")
            for entry in rec.get("inputs", []):
                if entry.get("path") == "spec/frozen-spec.md":
                    claimed = claimed or entry.get("sha256")
            if claimed:
                named += 1
                if in_force and claimed != in_force:
                    bad(f"clean: {p.name} names a frozen spec other than the one in force when it was written")
        if in_force and in_force != frozen:
            bad("the spec published here is not the one the build side last received")
        elif named:
            good(f"{named} build-side records name the frozen spec in force at the time")
        earlier = [h for h in history if h != frozen]
        for h in earlier:
            old = root / "frozen-history" / f"{h}.md"
            if not old.is_file():
                bad(f"an earlier frozen spec ({h[:16]}...) is named by the records but not in this folder")
            elif sha256_file(old) != h:
                bad(f"the earlier frozen spec {old.name} does not match its own hash")
        if earlier and not any("earlier frozen spec" in m for m in problems):
            good(f"{len(earlier)} earlier frozen spec(s) from before an update are present and match their hashes")
        steps = [rec.get("step") for _, _, rec in sides["clean"]]
        passed_test = any(rec.get("step") == "test" and rec.get("result") == "pass" for _, _, rec in sides["clean"])
        if "emit" in steps and not passed_test:
            bad("a release record exists but no passing test record does")
        elif passed_test:
            good("a passing test record precedes the release")
    lines.append("")
    lines.append("This checks that the published record is complete and unaltered since it was written.")
    lines.append("It cannot check that the record is true: that rests on who kept it and how.")
    return not problems, lines


# ------------------------------------------------------------------- emit helpers

REGISTRIES = {
    "PyPI": "https://pypi.org/pypi/{name}/json",
    "npm": "https://registry.npmjs.org/{name}",
    "crates.io": "https://crates.io/api/v1/crates/{name}",
}


def name_collisions(name, timeout=8):
    """Report-only: does a package of this name already exist in the common registries?"""
    out = {}
    slug = re.sub(r"[^a-z0-9._-]", "-", name.strip().lower())
    for registry, pattern in REGISTRIES.items():
        req = urllib.request.Request(pattern.format(name=slug), headers={"User-Agent": "name-availability-check"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out[registry] = "taken" if resp.status == 200 else f"unclear (status {resp.status})"
        except urllib.error.HTTPError as exc:
            out[registry] = "free" if exc.code == 404 else f"unclear (status {exc.code})"
        except (urllib.error.URLError, OSError, ValueError):
            out[registry] = "not checked (no answer)"
    return out


MANIFESTS = ["requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "package.json",
             "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "Gemfile", "composer.json"]


def declared_dependencies(repo):
    """Best-effort list of third-party dependencies the repo declares, by manifest."""
    repo = Path(repo)
    found = {}
    for name in MANIFESTS:
        p = repo / name
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        deps = []
        if name == "requirements.txt":
            deps = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith(("#", "-"))]
        elif name == "package.json":
            try:
                data = json.loads(text)
                for key in ("dependencies", "devDependencies", "peerDependencies"):
                    deps += [f"{k}@{v}" for k, v in (data.get(key) or {}).items()]
            except ValueError:
                deps = ["(unreadable package.json)"]
        elif name == "pyproject.toml":
            m = re.search(r"(?ms)^\s*dependencies\s*=\s*\[(.*?)\]", text)
            if m:
                deps = [d.strip().strip("\"'") for d in m.group(1).split(",") if d.strip().strip("\"'")]
        elif name == "Cargo.toml":
            m = re.search(r"(?ms)^\[dependencies\](.*?)(?:^\[|\Z)", text)
            if m:
                deps = [ln.split("=")[0].strip() for ln in m.group(1).splitlines() if "=" in ln]
        elif name == "go.mod":
            deps = re.findall(r"(?m)^\s*(?:require\s+)?([\w./-]+\.[\w./-]+)\s+v[\w.+-]+", text)
        else:
            deps = ["(manifest present; dependencies not parsed)"]
        found[name] = deps
    return found


if __name__ == "__main__":
    # Run as a script, this file checks a published provenance folder:  python verify.py <folder>
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent)
    ok, report = verify_bundle(target)
    print(f"provenance check of {target}")
    for line in report:
        print(line)
    print("RESULT: " + ("consistent" if ok else "PROBLEMS FOUND"))
    sys.exit(0 if ok else 1)
