---
name: spite
description: Run SPITE (Survey, Partition, Isolate, Test, Emit), a clean-room pipeline that takes one software product from public research to an independent open-source release with a published provenance trail. Use this whenever the user wants to clean-room, clone, reimplement or build an open-source alternative to an existing product or SaaS; asks how to do clean-room reverse engineering with agents; mentions SPITE, a dirty and clean repo, a frozen spec, a spec gate or a provenance scan; or wants to check whether a repo is contaminated by the product it was modelled on. Also use it to run any single step (survey, spec, review, freeze, build, conformance tests, release), to sift a list of candidate products, to update a released clone, to try the pipeline on the built-in demo target, or when the user asks what SPITE is or how to use it.
argument-hint: "[help | demo | scout <list> | <what you want to do>]"
---

# SPITE: Survey, Partition, Isolate, Test, Emit

SPITE builds an independent implementation of an existing product without copying it. One group reads public material about the product and writes down what it does. A different group, which never sees the product or that material, builds from the written spec alone. The value of the result rests on being able to show that the wall between the two held, so every step is run through a runner that enforces the wall and writes a record.

There are two repos per target and one gate between them:

- **Dirty repo** `spite-<target>-dirty`: the survey, the source list, the draft and reviewed spec, logged questions and answers.
- **Clean repo** `spite-<codename>`: the code, the conformance tests, the licence and the provenance bundle.
- **The gate**: only the frozen, hashed spec crosses, plus reviewed written answers to the builders' questions. The runner carries them. Nothing else moves, in either direction.

## How to open

The user may never have seen this before, and nothing on their screen explains it. Before you run anything, make sure they know where they are. How you open depends on what they asked:

- **They asked for help, asked what SPITE is, or invoked the skill with no task.** Read `HELP.md` (beside this file) and give them its substance in your own words, briefly: what SPITE does in two sentences, the things they can ask for, and what the first time looks like. Then ask what they want to do, and suggest the demo if they have not run it. Do not paste the whole file; offer the rest (`spite help` prints it).
- **First time on this machine.** Run `spite doctor` before anything else. It checks for Python, a recent enough Claude Code, git with a name and email, and a container runtime, and says how to fix what is missing. Tell the user what it found in plain words.
- **They asked for the demo.** Say in a few lines what is about to happen: a made-up product, the stages it will go through, roughly 20 minutes and a few dollars of model usage, that Docker must be running for the test step, and that you will stop once to show them the spec before it is frozen. Then start.
- **They named a product or a step.** Run `spite status <target>` first. If a workspace exists, tell them where it stands and what the next step is before doing it. If none exists, tell them in a few lines what the process will ask of them (the list under "Before anything runs against a real product"), then begin with the first question, not with all of them at once.
- **They have a list and no target.** Explain the scout in two sentences, including that it costs per candidate, and agree a limit.

End your opening, and each point where you stop for a decision, with one short line saying that `/spite help` explains the whole thing. Once per stop is enough. When the skill was installed as a plugin its command is `/spite:spite`; use whichever form the user typed.

There is also an on-screen reminder that needs no session with you: `spite hint on` adds two hooks to the current folder's settings, so that "Type /spite help for help" is shown to the user when a session starts there and every tenth prompt (`--every N` to change it, `--user` for every folder, `spite hint off` to remove it). The user sees the line; you do not. Offer it once, when they first set up a workspace. Turn it on only if they say yes, and never with `--user` unless they ask for that, since it changes their own settings file.

As you go, say what each step is for in a sentence when you start it, and what it found when it ends. The runner's output is terse and written for you; the user should hear it in plain words. When a step needs a decision from them, say what the decision is, what you would choose and why, and wait.

## Where you stand

You, the session reading this, are on the dirty side. You will see the target's name, the survey and the lint reports. That means:

- Do not write, edit or create files in the clean repo yourself, and do not "just fix" clean-side code. Clean-side work happens only in the sessions the runner launches.
- Do not pass text into a clean-side session. The runner writes their task messages. If the builders need information, it goes through `spite qa`.
- Scan and lint details quote dirty material. Read them, act on them from the dirty side, and never copy them into the clean repo.

If you break this, the provenance record is no longer true, and the record is the product.

## Before anything runs against a real product

Ask the user which target to work on. Do not pick one for them and do not start a survey of a real product without their say-so. Then settle these with the user:

1. **The target gate.** All four must hold: the core value is in code (not a licence, a data set, a network or a model); the core can be built in under four weeks; no strong open-source equivalent exists; you can name the paying users the release would take. Record their answers with `spite gate`. If one fails, say so and stop.
2. **Prior exposure.** Ask whether the user has seen anything non-public from this target: interviews, take-home tasks, NDAs, employment, demo accounts. Such a person should not steer the survey, and the material must not be used. Their general professional knowledge is fine. Record the answer in the gate with `--prior-exposure "..."`.
3. **Terms of use, before any survey.** Many products' terms bar automated access to their site, and a survey that reads the site directly is automated access. So the terms come first: run `spite terms <target>`, which reads only the terms pages and notes what they say, or have the user read them in a browser. Then tell the user what the terms say about automated access, reverse engineering and competing products, and whether they seem to bind any visitor or only account holders. The user decides, and you record it with `spite terms <target> --decision live|saved|stop --note "..."`: `live` lets the survey read the public site, `saved` limits it to copies a person saved from a browser, `stop` ends work on the target. The survey refuses to run until a decision is recorded. Do not make this decision for the user, and do not treat "public" as meaning "permitted". When asked, recommend `saved`: it avoids the automated-access question, the saved copies stay on disk as provenance, and the survey reads the pages' real text instead of a summary. Account-based observation is a separate switch, off unless the user turns it on in the dirty repo's `spite.json`.
4. **Names that must not cross.** Collect the target's product names, domain, slogans and feature brand names. They go in as `--mark` values and the gate blocks them.
5. **What happens to the research.** Explain the dirty repo to the user in plain words, because it is easy to mistake for scrap: it is the record of what was read, the drafts and the questions and answers. It is in use until release (the leak scan compares the product against it), and afterwards it is the evidence behind the release, since the published records carry hashes of files that exist only there, and it is where a later update starts. Then give them the choice, and do not choose for them:
   - **Keep it on this machine only.** Nothing to set up. If the machine is lost, so is the evidence.
   - **Back it up to a private repo of its own.** The cleanest separation. Record it with `spite remote <target> --dirty <url>`.
   - **Back it up to a branch of the product's private dev repo.** One repo to look after. Record it with `spite remote <target> --dirty-on-branch`: snapshots go to a `provenance` branch, inside a `provenance/` folder, and never into the product's branch or the folder the builders work in. The cost: that repo can never simply be made public, because the research branch would go with it. A public release then needs a fresh repo that receives only the product branch.
   - **Discard it after release.** Allowed, but say what it costs: the published hashes can no longer be checked against anything, and an update means surveying again from nothing.

   With either backup choice, `spite backup <target>` does the push, when the user asks for it. Whatever they choose, the research never goes anywhere public: the runner checks that the destination is private each time, and a push guard inside the dirty repo refuses any push to a repo that is not.

## Commands

The runner is `${CLAUDE_SKILL_DIR}/scripts/spite.py` (Python 3, standard library only; `spite` and `spite.cmd` wrappers sit beside it). Run it from the folder that should hold the two repos, or pass `--root`.

| Step | Command | What it does |
|---|---|---|
| Set up | `spite init <target> --name "<Name>" --url <site> --mark <mark>` | Creates both repos, installs the role files and the clean-side guard. The clean repo gets a neutral codename (`--codename` to choose one) |
| Gate | `spite gate <target> --core-in-code "..." --weeks N --no-strong-oss "..." --paying-users "..."` | Records the four-part target gate |
| Terms | `spite terms <target>`, then `spite terms <target> --decision live\|saved\|stop --note "..."` | Notes the target's terms of use, then records the user's decision on how the survey may read the site |
| Survey | `spite survey <target> [--from-dir <folder>]` | Dirty side: observation log and dated source list, from the live site or from saved copies |
| Partition | `spite partition <target>` | Dirty side, no web: draft functional spec in new words |
| Review | `spite review <target> [--note "..."]` | Dirty side: strips expressive material, then lints the result |
| Freeze | `spite freeze <target>` | Hashes the reviewed spec and carries it across the gate |
| Build | `spite build <target> [--fix]` | Clean side, fresh session: implementation from the frozen spec |
| Q&A | `spite qa <target>` | Carries builders' questions to the reviewer and reviewed answers back |
| Test | `spite test <target> [--fix-tests]` | Clean side: conformance tests from the spec, the test run, the provenance scan |
| Emit | `spite emit <target> --name <NewName> --license AGPL-3.0\|Apache-2.0` | Licence, README, provenance bundle; local only |
| Settings | `spite config <target> [--sandbox docker] [--test-cmd "..."] [--dirty-model X --clean-model Y]` | Show or change how tests are run and which model each side uses |
| Check | `spite status <target>`, `spite scan <target>` | Show the record chain; rerun the provenance scan |
| Verify | `spite verify <folder>` | Check a published `provenance/` folder. Needs nothing else; anyone can run it |
| Scout | `spite scout <name> --list <file> [--limit N]` | Sift a list of candidates against the target gate, from search results only. Starts no survey |
| Update | `spite update <target>` | Start an update of a released product; the usual steps then run as an update |
| Destinations | `spite remote <target> [--clean <url>] [--dirty <url> \| --dirty-on-branch]` | Show or set where the two repos are checked in. Never pushes |
| Back up | `spite backup <target>` | Push the research to the private place the user chose. The only command that pushes, and only the dirty side |

Options that go before the step name: `--model <name>` picks the model for the sessions, `--budget <usd>` caps each session's spend, `--timeout <seconds>` stops a stuck session. Sessions take minutes; run long steps in the background and tell the user what is running.

Settings to offer right after `init`:

- **A different model for each side** (`--dirty-model`, `--clean-model`). The builders should not share a mind with the people who read the target. Recommend it; the user chooses the models.
- **A sandbox for the test run.** `init` picks a container when Docker is present. Without one, `spite test` refuses to run, because code written on the build side would otherwise execute with the user's own file access and could read the dirty repo.

- **Signed commits** (`spite config <target> --sign-with <ssh public key>`). Every step is a commit, so signing the commits signs the record. The key must work without a prompt; the runner checks with a test commit.
- **Push after each step** (`spite remote <target> --auto-push on`, once destinations are set). Both sides are pushed to their private repos after every step, which gives an outside, dated copy of every record. The product branch is pushed only while its destination is private; a push anywhere public is publishing and stays the user's act.

`spite init --clean-root <folder>` puts the clean repo somewhere other than beside the dirty one. Suggest it when the user wants the two physically apart.

## Running the steps

Run them in order. Each prints what to run next and writes a record. A step that fails says why.

**Survey.** It runs only after the terms decision. `--from-dir <folder>` surveys copies of public pages a person saved, instead of the live web; under a `saved` decision that is the only mode. A survey can be run again to fill gaps: it keeps the earlier log and extends it. If an earlier survey should not be used at all (it ran in a mode the user has since ruled out, or a source turned out to be tainted), run `spite survey <target> --set-aside --note "..."`: its output moves where no role can read it, its record stays in the chain, and the next survey starts clean. Never delete survey material by hand. Read the log's Gaps section afterwards. The page-reading tool often returns a summary, not the page's text, so where exact field names and numbers matter (an API reference, a file format), ask the user to save the vendor's published schema or reference pages into a folder and run a second pass over them with `--from-dir`.

**Review.** The reviewer strips names and expressive material, then the runner lints the result. It fails on any listed mark, any source address, any run of eight or more consecutive words shared with the survey material, any interop literal the user has not approved, and any decision left open in the text. When it fails, rerun `spite review`; the reviewer is handed the lint report and fixes it. Use `--note "..."` to pass a decision of the user's, which is recorded. `--lint-only` rechecks the reviewed spec without a new review session, for when it was edited by hand on the dirty side.

Interop literals are the user's call, not yours and not the reviewer's. If the lint reports an unapproved literal (a format tag or header name that contains the target's name), tell the user what it is and why interoperability needs it. If they approve, add it to `interop_literals` in the dirty `spite.json` and rerun the review; if not, rerun and the reviewer removes it.

After the reviewer, a **second reader** goes through the spec against the survey with one aim: to find names, wording, sample values and source-shaped structure the reviewer left in. Its findings land in the lint report as failures and the next review round fixes them. Expect three or four rounds on a first spec; that is the check working. If it will not converge, tell the user what is still being flagged. They can end it with `--skip-audit --note "..."`, which is recorded.

**Freeze.** This is a checkpoint for a person. Show the user where `spec/reviewed-spec.md` is and wait for them to say it may be frozen. Once frozen, the spec is what the builders get and what gets published.

Freeze first runs a **blind test**: a reader with no web access, in an empty folder, using the build side's model, is given the spec alone and asked which product it describes. If it names the target, the builders could too, so the freeze is refused and the reasons go back to the reviewer. Run `spite review` again; do not rerun freeze hoping for a different answer, which the runner refuses anyway. Sometimes the identifying parts are what interoperability needs (a published API's field names). Then it is the user's decision to freeze anyway with `--accept-identifiable --note "..."`. Explain what the reader recognised and let them decide.

**Build, then test.** `spite build` refuses to start unless the clean repo's spec matches the frozen hash. The builder cannot run code; `spite test` runs the tests and writes `reports/test-report.txt`. When tests fail, the code or the test may be the one that is wrong, and neither side may decide that alone:

1. Run `spite build <target> --fix`. The builder reads the report and either fixes the product or, if it believes a test contradicts the spec, files a question instead.
2. If it filed a question (the runner says so), run `spite qa <target>`. The reviewer answers from the spec.
3. If the answer says the test is wrong, run `spite test <target> --fix-tests`, which has the tester recheck the failing tests against the spec and the answer. Otherwise run `spite build --fix` again so the builder acts on the answer.
4. Run `spite test <target>`.

A few rounds are normal. If the spec itself turns out to be ambiguous or wrong, fix it on the dirty side with a new review and `spite freeze --refreeze`, never by touching the clean repo.

**The ship gate** is the passing `test` record. It needs three things: every `(core)` item in the spec's checklist has a test that cites its id, the tests pass, and the provenance scan finds nothing. `spite test` prints the tail of the test output and lists core items with no test; `spite test --write-tests` has the tester add them. The full run is in `reports/test-report.txt`.

**Scan findings.** The scan looks for the target's names, source addresses, identical files, runs of eight copied words, and identifiers or short quoted wording that exist in the survey material but never crossed the gate. Read the detail file the runner names, which stays on the dirty side. Each finding is one of:

- **A leak or a coincidence that can simply be reworded.** Run `spite build <target> --fix` (or `spite test --fix-tests` for files under `tests/`, or `spite emit` again for the README). The runner tells that role which of its own passages or names to change, and nothing else. Plain one-line descriptions often collide with the survey's wording; a fix round clears them.
- **A coincidence the user is content to keep**, such as an ordinary option name. Only the user can say so: `spite scan <target> --accept <kind> --match "<text from the finding>" --note "why"`. One-word option names like `--date` are already listed for information only.
- **A fault in the scan.** Stop and tell the user.

Do not wave findings through, and do not loop: if two fix rounds leave the same finding, stop and tell the user.

**Emit.** The user chooses the new name and the licence. AGPL-3.0 keeps hosted versions open; Apache-2.0 favours adoption and embedding. The name must have no reference to the target, and the runner rejects one that overlaps a mark. It also reports, without blocking, whether a package of that name already exists in the common registries; pass that on. If the product declares third-party dependencies, emit stops until a person has checked their licences against the chosen one (`--deps-reviewed "who and what they found"`); the tool lists them and does not judge them. Emit prepares the release locally and stops. The bundle carries its own checker: `python provenance/verify.py provenance` confirms the record is complete and unaltered.

**Never touch the clean repo yourself.** Every clean-side command first checks that the clean repo has no changes the runner did not make, and stops if it finds any. If that happens, say what the files were; do not tidy them away quietly.

**Publishing is the user's act.** Never push the product, tag, create a remote repo or announce anything unless the user tells you to. If the research is kept on a branch of the product's dev repo, remind them that this repo must stay private and that the public release goes to a fresh repo with the product branch only. Tell the user the release is ready, where it is, and that the clean repo's git history contains every attempt, so they can decide whether to publish the history or a fresh export.

**Where things are checked in.** Three kinds of repo exist and they never share a home:

- The skill's own repo holds the skill and nothing else. Never commit a target's repos, survey material, briefs, target lists or notes into it.
- Each target's dirty repo and clean repo are their own git repos, created with no destination at all.

Before anything other than the skill is pushed anywhere, ask the user which repo it should go to, separately for each repo. Do not suggest the skill's repo, do not reuse another target's repo, and do not create a remote repo yourself. The dirty repo holds survey material and goes only to a private repo, if it goes anywhere. Record the answers with `spite remote <target> --dirty <url> --clean <url>`, which sets the destinations and pushes nothing. It refuses the skill's own repo, refuses one destination for both sides, and refuses a dirty destination it finds to be public. Where it cannot check (a host other than GitHub, or no `gh` command), it needs `--dirty-is-private`, which records that the user said so; ask them, do not assume. `spite remote <target>` shows what is set and confirms the dirty repo's push guard is in place. The same goes for any other file you think is worth keeping under version control: ask for its destination first.

**What a release says about how it was made.** The published records and `PROVENANCE.md` state what each role could reach and nothing about how the work was carried out. They name no tools, models or vendors, the runner's commits carry the operator's own git identity with no credit lines, and the tooling folders are kept out of git. Keep it that way: do not add generator notes, badges or credit lines to anything in either repo.

## Choosing a target with the scout

When the user has a list of candidates and no target yet, `spite scout <name> --list <file>` sifts the list against the four-part gate and writes a ranked table (`spite-scout-<name>/report.md`). It works from search results only: the scout has no tool for opening a page, so no candidate's site is touched and nobody's terms are in question. It runs in batches and can be continued; `--limit` caps how many it looks at in one run. A long list costs real money, so say how many candidates there are and agree a limit before starting.

Treat the table as a first sift, and say so. It is built from snippets, it is often wrong about open-source equivalents' licences and maturity, and it rates most candidates `no`, which is the correct behaviour. The scout names no target. The user picks, and the gate for their pick is still recorded with `spite gate` in their own words.

## Updating a released product

`spite update <target>` opens an update cycle on a product that already has a passing release. After that the usual commands run in order and each knows it is an update:

- **survey** brings the log up to date from newly saved pages and writes what was added, changed and removed;
- **partition** rewrites the spec keeping every requirement id that still holds, numbers new requirements after the highest existing id, and writes a delta by id;
- **review** and **freeze** treat the delta like the spec: it is checked, frozen and carried across with it;
- **build** changes only what the delta calls for, and **test** has the tester add, correct and remove tests to match;
- **emit** releases under the same name, and its record points at the release before it.

Earlier frozen specs are kept by their hash and travel in the bundle, so the records of earlier releases can still be checked. Ask the user for the same name and licence as before unless they say otherwise.

## Trying it out

`spite demo <slug>` sets up a made-up product so the whole pipeline can be exercised without touching a real one. It prints the remaining commands. Use it the first time, and after changing the role files. `assets/demo-target-v2/` is the same made-up product one release later, for trying `spite update`: after the demo's emit, run `spite update <slug>` and survey that folder.

Two self-tests need no sessions and cost nothing: `python -I scripts/selftest_guard.py scripts/spite_guard.py` and `python -I scripts/selftest_runner.py`. Run both after changing the runner or the guard.

## What SPITE does not give you

Be straight with the user about these; details are in `references/isolation.md`.

- It controls what material a build session could reach. It cannot certify what a builder already knew, and a language model may have read the target's public pages in training. The clean repo's neutral codename keeps the target's name off the clean side, which helps and does not solve it.
- The scan finds names, addresses, identical files, copied runs of text, and survey-side identifiers and short wording. It does not find paraphrase, and it cannot judge similarity of design.
- The blind test asks one reader once. A pass means that reader did not recognise the product, not that nobody could.
- The test run executes code the build sessions wrote. It runs in a container with no network and a read-only copy of the clean repo by default. `--unsandboxed` removes that protection, and the release's provenance file then says so.
- The record chain shows the published records are complete and unaltered. It cannot show they are true. Signed commits tie the record to the operator's key, and pushes to a private host give outside dates; neither stops an operator from writing a false record in the first place.
- It is a record of process, not legal advice. A release aimed at a real company deserves a lawyer's read first.

## Reference files

- `references/process.md`: the SPITE process in full, the target gate, and how to tell function from expression. Read it when a judgement call comes up in survey, partition or review.
- `references/isolation.md`: how the wall is enforced, what was verified, the known limits and how to harden it. Read it before promising the user anything about isolation.
- `references/records.md`: the layout of both repos and the record format. Read it when inspecting or publishing the record chain.
- `agents/`: the six role definitions. `spite init` installs them in each repo's `.claude/agents/`, and the runner feeds them to the sessions it launches.
