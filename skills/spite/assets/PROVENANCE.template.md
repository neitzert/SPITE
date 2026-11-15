# Provenance of {{NAME}}

{{NAME}} is an independent implementation written from a functional specification. This file says how that was done, what was controlled, what was not, and where the records are. It was prepared on {{DATE}}.

## How it was made

1. **Survey.** Public material about an existing product was read and logged. Every source is listed with its address and the date it was read in `provenance/sources.csv` ({{SOURCES}} sources). No material behind a login, no leaked or decompiled code and nothing held in confidence was used.
2. **Specification.** A functional specification was written from that log in new words. A reviewer then removed expressive material: interface text, documentation prose, visual design, names and marks. An automated check rejected any draft that named the existing product, cited its addresses, or repeated eight or more consecutive words from the survey material.
3. **Freeze.** The reviewed specification was frozen. Its SHA-256 is `{{SPEC_SHA256}}` and the file is `provenance/frozen-spec.md`.
4. **Isolation.** The code and the tests were written in a separate working environment confined to this repository: no web access, no command execution, no history shared with the survey side, and no read access to anything outside the repository. The frozen specification was the only material that entered. Written questions from the build side were answered in writing by the reviewer under the same checks ({{QA}} answers, in `provenance/qa/` when there are any).
5. **Test.** Conformance tests were written from the specification, without access to the implementation. The release required every core requirement in the specification's checklist to have a test, those tests to pass, and a scan of this repository to find no survey-side material. The tests were run in: {{TEST_ENVIRONMENT}}.

## What this establishes and what it does not

- It records and controls **which material was available** while the code was written.
- It does **not** certify what general background knowledge a builder had before the work began. No process can.
- The scan detects names, addresses, identical files and copied runs of text. It does not detect paraphrase or similarity of structure.
- This is a record of process, not a legal opinion.

## Records

Each step wrote a record of its inputs, their hashes, its outputs and its result. The records for the release itself are added after this table.

| Date (UTC) | Side | Step | Result | Record |
|---|---|---|---|---|
{{RECORDS_TABLE}}

## How to check

Compute the SHA-256 of `provenance/frozen-spec.md` and compare it with the value above and with `provenance/frozen-spec.sha256`. Each record lists the SHA-256 of the files it read and wrote, and the SHA-256 of the record before it on its side, so the records form two chains that also pin each other where the sides met. Changing, removing or inserting a record breaks a chain. The check can be run on this folder alone, with `python provenance/verify.py provenance`; it shows the record is complete and unaltered, not that it is true.

## Licence

{{NAME}} is released under {{LICENSE}}. See `LICENSE`.
