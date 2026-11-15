# Repo layout and gate records

## Layout

```
<root>/
  spite-<target>-dirty/          dirty repo (stays private)
    spite.json                   target name, marks, approved interop literals, settings
    survey/
      observations.md            raw observation log
      sources.csv                id,url,title,accessed,kind,access
      terms.md                   note on the target's terms of use (live runs)
      raw/                       saved public pages, when surveying from a folder
    spec/
      draft-spec.md              the spec writer's draft
      reviewed-spec.md           after the reviewer
      reviewer-notes.md          what was removed and why
      lint-report.txt            result of the crossing checks
      frozen-spec.md             the frozen spec
      frozen-spec.sha256         its hash
      frozen-history/<hash>.md   every spec ever frozen, filed under its own hash
      baseline-spec.md, reviewed-delta.md   during an update: the released spec, and what changed
    qa/                          question-NNN.md and answer-NNN.md pairs
    set-aside/<time>/            survey output taken out of use; no role can read it
    records/                     gate records for dirty-side steps
  spite-<codename>/              clean repo (becomes the release)
    spec/frozen-spec.md          the only material that entered from outside
    spec/frozen-spec.sha256
    <the implementation>
    INTERFACE.md                 how to start and call the product
    tests/                       black-box conformance tests
    reports/test-report.txt      output of the last conformance run
    qa/questions/, qa/answers/   builders' questions and reviewed answers
    records/                     gate records for clean-side steps
    LICENSE, README.md, docs/    written at emit
    PROVENANCE.md                written at emit
    provenance/                  sources.csv, frozen spec and hash, all records, the Q&A log, verify.py,
                                 and after an update the delta and the earlier frozen specs
    spite.json                   test command, setup command, timeout
  .spite/<target>/               the runner's private folder: session logs, policies,
                                 tool-call audit logs, scan details. Never published.
```

`.claude/` in each repo holds the role files, and in the clean repo the guard and its settings. Both `.claude/` and `.spite/` are kept out of git through `.git/info/exclude`, not `.gitignore`, so the published repo does not list the tooling.

## Record format

Every step writes `records/<NN>-<step>-<UTC time>.json`. `NN` is a code for the kind of step, not a sequence number: steps repeat and interleave, so the order of events is the `date` field, which `spite status` sorts by. Reruns add a new file; nothing is overwritten, so failed attempts stay in the chain.

```json
{
  "spite_record": 1,
  "step": "build",
  "side": "clean",
  "date": "2026-11-15T17:10:59+00:00",
  "inputs":  [{"path": "spec/frozen-spec.md", "sha256": "..."}],
  "outputs": [{"path": "INTERFACE.md", "sha256": "..."}],
  "checks":  {"spec_sha256": "...", "mode": "build", "problems": []},
  "result":  "pass",
  "access": {
    "role": "builder",
    "isolation": "separate working environment; no shared history with the other side",
    "web_access": false,
    "command_execution": false,
    "could_read": ["."],
    "could_write": ["."]
  }
}
```

- `seq` and `prev_sha256` chain the records of one side: each record carries the SHA-256 of the record file before it. Records written where the two sides meet (freeze, spec received, questions and answers, release) also carry `other_side_head_sha256`, the latest record of the other side at that moment.
- `inputs` and `outputs` list each file with its SHA-256 at the time, so one record's outputs can be matched to the next record's inputs.
- `access` says what the role could reach. Records are written to be published, so they state capabilities and nothing about how the work was carried out: no tools, models, vendors or call counts.
- The detail stays private. For each step the runner keeps the role's policy, a log of every allowed and refused call, and the raw output under `.spite/<target>/sessions/`. A refused call there is not a failure: it shows the wall being tested and holding.

| No. | Step | Side | Notable checks |
|---|---|---|---|
| 00 | gate | dirty | the four criteria as the operator stated them |
| 01 | terms | dirty | pages read for the terms note; the operator's decision (live, saved or stop) and their note |
| 02 | survey | dirty | number of sources, pages fetched but not logged, the terms decision it ran under |
| 02 | survey-set-aside | dirty | which survey output was taken out of use, where it went, and why |
| 03 | partition | dirty | draft written, nothing outside `spec/` changed |
| 04 | review | dirty | lint failures and warnings, what the second reader found, any operator decision |
| 05 | blind-test | dirty | written only when a blind reader named the target from the spec |
| 05 | freeze | dirty | the spec's SHA-256, approved interop literals, the blind test's result, what crossed |
| 06 | config | clean | a change to how the tests are run |
| 06 | spec-received | clean | the SHA-256 the clean repo received |
| 07 | build | clean | spec hash, build or fix round |
| 08 | test | clean | test command, where it ran and whether that was sandboxed, requirement coverage, exit code, scan counts, ship gate |
| 09 | qa | both | the question, lint result, what crossed |
| 10 | scan | clean | provenance scan counts |
| 10 | scan-accept | dirty | a scan finding the operator judged not to be a leak, and why |
| 11 | emit | clean | new name, licence, scan counts, test environment, dependencies and their licence review, name lookups, whether commits were signed, and for an update the previous release's record and spec, `published: false` |
| 12 | update-start | dirty | the update's number, and the hashes of the release and frozen spec it starts from |
| 00 | scout | scout folder | how many candidates were listed and assessed, and the method (search results only) |

## Following the chain

1. `provenance/sources.csv` lists what was read and when.
2. The `survey` record hashes the log and the source list.
3. The `partition` and `review` records take those as inputs and hash the drafts.
4. The `freeze` record states the frozen spec's hash. The `spec-received` record on the clean side states the same hash.
5. Every clean-side record lists `spec/frozen-spec.md` as an input with that hash.
6. The last passing `test` record is the ship gate. The `emit` record names the release.

`spite status <target>` prints the chain in time order.

## Checking a published bundle

`spite verify <folder>` checks a `provenance/` folder and needs nothing else: not the dirty repo, not the clean repo, not this skill's other files. Every release also carries the checker as `provenance/verify.py`, so a reader can run `python provenance/verify.py provenance` in the published repo. It confirms that the frozen spec matches its stated hash, that each side's records form an unbroken chain, that the links between the sides resolve, that every build-side record names that frozen spec, and that a passing test precedes the release. It reports what it cannot tell you as well: that the record is true.
