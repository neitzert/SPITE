# SPITE: how to use it

SPITE builds an open-source replacement for an existing product without copying it. One side reads what the product publishes and writes down what it does. The other side, which never sees the product, builds from that description alone. Every step leaves a record, so you can show afterwards that the wall between the two held.

The name stands for the five stages: **S**urvey, **P**artition, **I**solate, **T**est, **E**mit.

## What you can ask for

Type `/spite` followed by what you want, in your own words. If you installed SPITE as a plugin, the command is `/spite:spite` instead; everything else is the same. For example:

| You want to | Say something like |
|---|---|
| See it work without touching a real product | `/spite run the demo` |
| Sort a list of candidate products | `/spite scout this list: candidates.txt` |
| Start on a product | `/spite start on Acme Notes, acme.example` |
| Carry on where you left off | `/spite where are we with acme?` |
| Bring a released clone up to date | `/spite update acme` |
| Check a release's records | `/spite verify the provenance folder in this repo` |
| Read this again | `/spite help` |

If this is your first time, run `spite doctor` to check that your machine has what is needed, then run the demo. It uses a made-up product, takes about 20 minutes, and costs a few dollars in model usage.

## What happens, in order

1. **Gate.** You say why this product is worth cloning: its value is in its code, it can be built in under four weeks, nothing open-source already does the job, and you can name who pays for it today.
2. **Terms.** The product's terms of use are read before anything else. You decide how its public pages may be read: directly, only from copies you save in your browser, or not at all.
3. **Survey.** Public pages only. Every page is logged with its address and the date.
4. **Partition.** The notes become a specification of what the product does, in new words. A reviewer strips out names, wording and anything else that belongs to the original. A second reader hunts for what the reviewer missed.
5. **Freeze.** You read the specification. A blind reader is asked whether it can tell which product it describes. Then it is frozen, and it is the only thing the builders ever get.
6. **Isolate.** The product is built from the frozen specification, in sessions that have no web access, cannot run commands and cannot see the research.
7. **Test.** Tests are written from the specification, not from the code, and run in a sealed container. The build is also scanned for anything that came from the research.
8. **Emit.** You pick a new name and a licence. The release is prepared on your machine with its records. Publishing it is yours to do.

## What you will be asked

Before any real product is touched:

- **Which product.** It is never chosen for you.
- **The four gate questions** from step 1.
- **Whether you have seen anything of it that is not public**: an interview, a take-home task, an NDA, a demo account.
- **How its pages may be read**, once you know what its terms say.
- **Which names must never reach the builders**: the product, its domain, its slogans.
- **What to do with the research afterwards**: keep it on this machine, back it up to a private repo, or discard it after release.

Along the way you are asked to read the specification before it is frozen, and to choose the name and licence at the end.

## What it will not do without you

- Start on a product you did not name.
- Freeze a specification you have not been shown.
- Publish, or push the product anywhere that is not private.
- Decide that something flagged by a check is fine. It tells you what was flagged and what it thinks, and you decide.

## What it costs

Each step runs one or more model sessions. On the made-up demo, a first release took about 17 sessions and under three dollars, and an update about half that. A real product has more pages and more code, so expect more. You can cap each session's spend, and you can choose the model for each side.

## What it does not give you

- **Proof of what the builders already knew.** It controls what they could read while building. A model may have seen the product's public pages before.
- **A legal opinion.** It produces a record of process. Before you release a clone of a real company's product, have a lawyer read it.
- **A finished business.** It reproduces what the code does, not the data, the customers or the services around it.

## Where things are

For a product called `acme`, in the folder you run it from:

- `spite-acme-dirty/`: the research, the drafts and their records. Private, always.
- `spite-<codename>/`: the product. The folder gets a neutral name so the builders never see the original's.
- `.spite/`: session logs and scan details. Never published.

## A reminder on screen

`spite hint on` makes Claude Code show "Type /spite help for help" when a session starts in this folder and every tenth prompt after that. `spite hint on --every 5` changes how often, `spite hint on --user` shows it in every folder, and `spite hint off` removes it. Only you see the line.

## Sharing or uploading the skill

`spite package` writes the whole skill as one file, `spite.skill`, which is what an upload form or another person needs. A single `.md` file is not the skill: the scripts and role files have to travel with it.

SPITE drives Claude Code on your own machine. It starts sessions with the `claude` command and uses git and a container runtime. Uploaded to the Claude apps it can explain itself, but the pipeline only runs where those are available.

## Commands, if you prefer to drive it yourself

Every step is one command; `spite --help` lists them and `spite <command> --help` explains each. `spite status <product>` shows what has been done and each step prints what to run next.
