# How the wall is enforced, and where it ends

Read this before telling anyone what SPITE guarantees.

## Mechanism

Every role runs as its own headless session, started by the runner, in the repo for its side. Four layers apply to each session.

1. **Restricted session.** The session starts with `--restricted --strict-mcp-config` and an explicit `--tools` list. That removes shell and code-running tools, removes web tools unless the role is the surveyor, ignores the user's, the project's and the local settings files, loads no MCP servers, and confines the file tools to the working folder.
2. **The guard hook** (`scripts/spite_guard.py`), passed in through `--settings` as a PreToolUse hook. It is default-deny: a tool the role's policy does not list is refused, so shell tools, web tools, MCP tools and sub-sessions are closed without having to be enumerated. For file tools it resolves the real path and allows it only inside the role's own folders. If the guard errors, it refuses.
3. **The spec lock.** On the clean side the guard checks the frozen spec's SHA-256 on every call and refuses everything if it differs. The runner makes the same check before `build`, `test`, `qa`, `scan` and `emit`.
4. **After-the-fact checks.** The runner compares what changed in the repo against what the step is allowed to produce, and fails the step otherwise.
5. **A sandbox for anything that runs.** No session can run code. The one place build-side code is executed is the conformance run, and the runner does that in a container that holds only the clean repo, read-only, with no network. Dependencies, when a product needs them, are installed in a separate phase that has the network and writes to a volume, not to the repo. Without a sandbox `spite test` refuses to run; `--unsandboxed` is an explicit choice that is written into the test record and the provenance file.
6. **Hands off the clean repo.** The runner commits after every step. Each clean-side command starts by checking that the clean repo has no uncommitted change, and stops if it finds one, because such a change was made by something other than the runner, most likely the session driving it.
7. **Two more readers before the gate.** After the reviewer, a second reader goes through the spec against the survey material looking only for what was left in. Before the freeze, a blind reader that has seen nothing but the spec is asked which product it describes; if it names the target, the spec does not cross. The blind reader uses the build side's model, since the question is what the builders would recognise.
8. **Neutral paths on the clean side.** A session's policy and settings files appear on its command line and in the guard's refusal messages. For clean-side and blind sessions those files live under a path that does not contain the target's name.

Each session also gets a scrubbed environment: auto memory off, hosted connectors off, attribution lines off, and on the clean side no environment variable that mentions the dirty repo.

### What each role can reach

| Role | Side | Web | Shell | May read | May write |
|---|---|---|---|---|---|
| survey | dirty | yes | no | `survey/`, `spite.json` | `survey/` |
| partition | dirty | no | no | `survey/`, `spec/` | `spec/` |
| reviewer | dirty | no | no | `survey/`, `spec/`, `qa/` | `spec/`, `qa/` |
| builder | clean | no | no | the clean repo | the clean repo except `spec/`, `tests/`, `reports/`, `qa/answers/`, `provenance/` |
| tester | clean | no | no | `spec/`, `tests/`, `qa/answers/`, `reports/`, `INTERFACE.md`, `README.md` | `tests/` |
| emit | clean | no | no | the clean repo | `README.md`, `docs/` |

No role can read or write `.git`, `.claude`, `.spite` or `records/`.

### Crossing the gate

Only the runner touches both repos. It copies the frozen spec and its hash into the clean repo at `freeze`, copies a reviewed answer at `qa`, and at `emit` copies the source list and the records into `provenance/`. It never puts text from one side into the other side's task message. Scan and lint details, which quote dirty material, are written only on the dirty side and in the runner's private folder; the clean repo's records carry counts.

### Backing the research up

The operator chooses one of three: no backup, a private repo of its own (the dirty repo is pushed there directly), or a branch of the product's private dev repo. In the third case the runner does not push the dirty repo itself. It keeps a small private working copy outside both repos, copies the dirty repo's files into a `provenance/` folder there together with a bundle of its full history, and pushes that as a `provenance` branch. The product branch and the folder the builders work in never contain it, so the wall holds locally. What changes is the remote: one repo now holds both sides on different branches, so that repo must stay private for good, and a public release needs a fresh repo that is given the product branch alone. `spite backup` refuses to push if the repo is no longer private, and refuses if the product branch does not exist yet, since the research branch would otherwise become the repo's default.

### Keeping the dirty repo private

The dirty repo is created with no destination. Two things keep it from reaching a public one. `spite remote` checks a proposed dirty destination with the host and refuses it unless it is private (or, where it cannot check, unless the operator states that it is). And a pre-push hook inside the dirty repo makes the same check at push time, so a destination added by hand is covered too. The hook also refuses the product's own destination and the skill's repo, whatever their visibility, because the dirty repo never shares a home with either. The hook is local to that copy of the repo: a fresh clone does not carry it, and `spite remote <target>` puts it back. It checks GitHub destinations through the `gh` command; for any other host it refuses unless the operator states the destination is private.

## What was verified

On the machine this was built on (Claude Code 2.1.321, native Windows):

- A restricted session listed exactly the five file tools it was given. It had no MCP tools, no sub-session or skill tools, no user-level instructions and no memory folder in its context.
- With the guard attached through `--settings`, the session could read and write inside its role's folders and was refused for a file in another folder of the same repo, for a file outside the repo, and for a write to a folder it may only read.
- `scripts/selftest_guard.py` runs 27 allow and deny cases against the guard, including `..` escapes, absolute paths, search patterns that climb out, shell, web and MCP tool names, a missing policy file, unparseable input and a spec hash mismatch. Run it after changing the guard: `python -I scripts/selftest_guard.py scripts/spite_guard.py`.
- Planted in a copy of a clean repo, each of these failed the provenance scan: a sentence copied from the target's documentation, the target's name in a code comment, a file copied from the dirty repo, and a source address. Editing the frozen spec on the clean side stopped `build`.

- The built-in demo ran end to end three times (survey from saved pages through emit). In one run the builder and the tester disagreed about a case; the builder filed a question, the reviewer answered from the spec, the tester corrected its test, and the ship gate then passed.
- A live-web survey of the reserved `example.com` domain confirmed that the surveyor's web tools work inside a restricted session with the guard attached, and that every page it fetched was in the source list.

- `scripts/selftest_runner.py` runs 42 checks of the runner without launching a session. Each gate has a planted failure: the target's name, a source address, copied words, an unapproved literal and an open decision in a spec; a spec with no checklist; a hand-made change in the clean repo; a test run with no sandbox; a core requirement with no test; survey-side identifiers and wording in the clean repo; an edited record, a removed record and an edited frozen spec in a published bundle.
- The demo was run again with all of this in place: the second reader forced four review rounds and found the target's id scheme, section order and close paraphrases that earlier runs had let through; 74 tests passed in the no-network container; the release bundle verified with its own checker and failed verification after one record was edited.
- A spec that described a well-known tool without naming it passed every automatic check and was then refused by the blind reader, which named the tool with 97% confidence. A second freeze attempt without a new review was refused.

Rerun these checks after a Claude Code upgrade. The restricted mode and the hook interface are the vendor's and can change.

## Known limits

**What the builder already knew.** SPITE controls what a build session can reach while it works. It cannot control what the model behind that session learned before. A language model may have read the target's public site, documentation or open-source clients in training. The mitigations are the clean repo's neutral codename, a spec that never names the target, a different model on each side when the operator sets one, role instructions not to draw on any known product, and the blind test, which refuses a spec that a reader can trace to the target. The blind test asks one reader once, and its answer varies from run to run: a pass means that reader did not recognise the product, not that the builders cannot. None of this is proof. The provenance file says so in plain words, and you should too.

**The sandbox is the operator's container runtime.** The test run is as isolated as that container is. It mounts the clean repo read-only and has no network, so build-side code cannot read the dirty repo or send anything out, and cannot alter the repo it is judged in. It does not defend against a flaw in the container runtime itself, and with `--unsandboxed` there is no protection at all: the code runs with the operator's file access and could read the dirty repo and print it into the test report, which the builder reads in a fix round.

**Interactive sessions are weaker than runner sessions.** `spite init` installs the guard and deny rules in the clean repo's `.claude/` so that opening Claude Code there by hand is also confined. But an interactive session loads the user's own instructions, memory and plugins, which the runner's sessions do not. Use interactive sessions for looking, and the runner for anything that goes on the record.

**The scan is a floor.** It finds listed marks, source addresses, identical files, runs of eight or more shared words, and identifiers (routes, field names, multi-word flags, config keys) or short quoted wording that are in the survey material but never crossed the gate. It ignores bare numbers and, in the copied-run count, the short identifiers the frozen spec itself hands over in code formatting, because data samples repeat those in a fixed order. One-word option names are listed for information and do not block. It does not detect paraphrase, translated text or similarity of design. A clean scan means none of those things was found. It does not mean the work is non-infringing.

**Clearing a finding tells the build side something.** To get a flagged passage reworded, the runner tells the builder, tester or release writer which of its own passages or names to change, and gives no reason. That string is already in the clean repo, so no dirty material crosses, but the role can infer that the dirty side has something like it. The alternative, an operator accepting the finding, leaves the match in place. Both are recorded.

**The orchestrating session is dirty.** The session that drives the runner sees both sides' reports. It must not write into the clean repo or relay text to clean sessions. The uncommitted-change check catches a write it leaves in the working tree. It does not catch one that is also committed by hand, and nothing technical stops the session from passing text some other way; the skill's instructions do.

**The record chain proves integrity, not truth.** Each record carries the hash of the one before it on its side, and the two sides pin each other at the freeze, at each question and answer, and at release. Changing, dropping or inserting a published record breaks the chain, and `spite verify` shows it. The last record of a chain can be cut off without trace until a later record follows it; in a release that later record is the release record. Nothing here stops the operator from producing a false but consistent set of records. Two optional settings narrow that. With signed commits, every step's commit carries the operator's SSH signature, so the record is tied to a key. With pushing after each step, a private host holds a dated copy of every record as it was made, which makes rewriting history afterwards visible to anyone the operator later shows that repo to. Neither is a notary: a host's push dates are the host's word, and a signature says who, not whether it is true.

**It is not legal advice.** A clean-room process is evidence of independent creation. Whether a given release infringes anything, breaches terms of use or runs into patents is a legal question the process does not answer.

## Hardening options

- Run the clean side under a different OS user that has no read access to the dirty repo, or on a different machine.
- Give the two repos different roots (`spite init --clean-root`), so they are not siblings.
- Turn on signed commits and pushing after each step. For a release that may be contested, add a timestamp from an independent service over the freeze and release commits; that is not built in.
- Use a different model, or a different vendor's model, for the clean side than for the dirty side.
- Have a person read the reviewed spec before every freeze and the full diff before every publish. The runner stops at both points for that reason.
