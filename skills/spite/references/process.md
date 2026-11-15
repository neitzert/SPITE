# The SPITE process

Survey, Partition, Isolate, Test, Emit. The steps exist to keep one promise: the people who wrote the code never saw the product or anything copied from it, and there is a record that shows it.

## 0. Target gate

A target qualifies when all four hold:

1. Its core value is in code, not in a licence, a data set, a network or a model.
2. The core can be built in under four weeks.
3. No strong open-source equivalent exists.
4. You can name the paying users the release would take.

The gate is the operator's own judgement, recorded as given. A target that fails one of these is either not worth building or not buildable this way. A product whose value is its data, its certified staff or its customer network yields a clone that works and that nobody needs.

## 1. Survey (dirty side)

### Terms first

Before any page of the target is surveyed, its terms of use are read and the operator decides how the survey may proceed. "Public" does not mean "permitted": terms often bar robots, scrapers and other automated means, bar reverse engineering except where the law allows it, and sometimes bar use by competitors. Whether such terms bind a visitor who never accepted them is a legal question this process does not answer, so it is put to the operator, with the clauses in front of them, and the decision is recorded:

- **live**: the survey may read the public site directly.
- **saved**: the survey reads only copies that a person saved from a browser.
- **stop**: the target is dropped.

The terms step itself reads at most four pages, and only terms pages. An operator who prefers that nothing automated touch the site at all can read the terms in a browser and record the decision without running that step.

### The survey

Gather only public material: marketing pages, public documentation, API references, pricing, published integrations, and behaviour observed through normal use where the terms allow it. Log every source with its address and the date it was read.

Never use leaked code, decompiled binaries, private repositories, material received in confidence (interviews, NDAs, sales calls under NDA, employment), or any account whose terms bar this use. If one source is tainted, the release it fed is tainted, so when in doubt leave it out and note the gap.

Output: a raw observation log (`survey/observations.md`), the source list (`survey/sources.csv`) and, for live runs, a note on the target's terms (`survey/terms.md`).

## 2. Partition (spec gate)

The dirty side turns the log into a functional spec: features, data model, interfaces and behaviours, written in its own words. A reviewer then strips expressive material. Only the reviewed spec crosses the gate. It is frozen and hashed first.

### Function crosses, expression stays

| Crosses (function) | Stays behind (expression) |
|---|---|
| What a feature does, its inputs, outputs and rules | Interface text: labels, messages, onboarding and email copy |
| The data model: entities, fields, relationships | Documentation prose and its sentence structure |
| Operation shapes of a published API: methods, parameters, status codes | Prompt text and sample content |
| Field names and identifiers of a published file format | Visual design, layout, colour, icons, tone of voice |
| Limits that are part of behaviour | Product names, feature brand names, slogans, trade dress |
| Error conditions | The wording of error messages |

Two working rules:

- State what happens, never what it says. "A second check-in on the same day is rejected" is function. The message the product shows is expression.
- Keep exactly what interoperability needs, and keep it in the Interfaces and Interchange sections only. A format tag or header that contains the target's name may be functionally necessary. That is a decision for a person, recorded as an approved interop literal, never a quiet exception.

### The lint

Before anything crosses, the runner rejects text that names the target or a listed mark, contains a source address, or shares a run of eight or more consecutive words with the survey material. Bare numbers are ignored in that count. The lint is a floor. It does not replace the reviewer's judgement or the person's read before the freeze.

### Two more readers

A reviewer who has read the vendor's pages stops noticing the vendor's order and phrasing. So a second reader goes over the reviewed spec against the survey with one job: find the names, wording, sample values and source-shaped structure still in it. Its findings are failures and go back to the reviewer.

Then, before the freeze, a blind reader who has seen nothing but the spec is asked what product it describes. If it can name the target, the spec fails: the builders would recognise it too, and a builder who recognises the product is no longer working from the spec alone. When the identifying parts are exactly what interoperability requires, freezing anyway is the operator's decision, and it is recorded.

## 3. Isolate (clean side)

The build runs in a separate repo, in separate sessions that never saw the dirty material or the target, with the frozen spec as their only input. Context, tools and history are not shared across the gate.

Questions from the clean side go back through the reviewer as written questions and written answers. Answers pass the same lint before they cross, and both sides log them.

## 4. Test

Conformance tests are written from the spec, not from the target and not from the implementation. Where interoperability matters (API shape, file formats, import from the incumbent), test against the published interface only.

The spec's conformance checklist is the list of requirements, each with an id and the core ones marked. Tests cite the ids, and coverage is counted: a core requirement with no test blocks the release, however many other tests pass.

Code written on the build side is executed in one place only, the conformance run, and there it runs in a sandbox that holds the clean repo alone and has no network.

Ship gate: every core requirement has a test, the tests pass, and the provenance scan finds no dirty material in the clean repo.

## 5. Emit

Release under AGPL-3.0 when the aim is to keep hosted versions open, or Apache-2.0 when adoption and embedding matter more. Publish the frozen spec, the source list and the gate records with the code. Use a new name and branding with no reference to the target. Include an importer for the incumbent's published export format, because switching cost is what keeps users where they are.

Publishing itself is always a person's decision and a person's act.

## Before the process: choosing among candidates

A list of candidates can be sifted against the target gate before any one is chosen. The sift uses search results only and opens no candidate's own pages, so it raises no terms question and starts no survey. It is a first pass for a person to check: it ranks, it does not decide.

## After the process: updates

Products change, and a clone that stops at one release falls behind. An update repeats the process on the difference. The survey is brought up to date from newly saved pages. The spec is rewritten with its requirement ids kept stable, so a requirement is the same requirement from one release to the next, and a delta names what was added, changed and removed by id. The delta is reviewed, frozen and carried across the gate with the new spec, and it is all the builders are told about what changed. The new release's record points at the one before it, and every spec ever frozen is kept under its own hash so that earlier records stay checkable.

## Naming

- Working folders are `spite-<target>-dirty` and `spite-<codename>`. The clean repo always gets a neutral codename, picked by the runner unless you choose one, so the target's name is not even in its path.
- The public name is chosen at emit. It must not contain, resemble or allude to the target's marks.
- Inside the clean repo, the target is never named. The one place a release identifies what was studied is the published source list in `provenance/`, where honesty requires it.
