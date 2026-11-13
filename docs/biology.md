*This is the picture the names come from. For which parts of the design really follow cell regulation and where the comparison stops, see [Where the design came from](../README.md#-where-the-design-came-from-cell-regulation) in the README.*

# The gene-editing picture, in full

Left unmanaged, coding agents behave like a plague of grasshoppers: five started at once will take every core, run the box out of memory, back the CI queue up for hours, and time their own tests out. Keeping them inside the host's carrying capacity is half the job. The other half is that a coding fleet is not a swarm competing for grass: many agents are making **heritable changes to one shared genome**, `origin/main`, which every future branch inherits.

That is gene editing, and the way labs do it safely is a good model for doing it with agents. In an *ex vivo* therapy (the way CAR-T cells are made), cells are taken out of the patient, edited in a dish, screened, and only verified cells are returned. squid-bot does the same with code: each issue is a known defect, each agent edits an isolated copy of the genome, CI screens the edited copy, and only a verified edit is integrated into `main`.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                       THE GENOME (origin/main)                               │
│   Known defects to correct and traits to add: the issue backlog              │
│      #4893 loss-of-function bug   ·   #5166 knock-in: new feature            │
└──────────────────────────────────────┬───────────────────────────────────────┘
                                       │
            1. Initiation: promoter-bot switches editing on or off
               • box-status: editor toxicity / host capacity (GREEN/YELLOW/RED)
               • usage limits, connectivity, owner holds: when and how many
               • comms-bot: the owner's sign-off, asked and answered by phone
                                       │
┌──────────────────────────────────────▼───────────────────────────────────────┐
│          THE EDITORS (Claude Code / Gemini / Cursor), one per culture        │
│                                                                              │
│   Each edits an isolated worktree (a cell culture, not the patient) and      │
│   produces an edited clone: a branch + draft PR, sharing cores via           │
│   heavy-run.sh                                                               │
└──────────────────────────────────────┬───────────────────────────────────────┘
                                       │
            2. Screening: is this clone correct, and did it break anything?
               • draft PR: cheap genotyping first, full CI only after
               • ci_digest: genotype a failed clone (real defect vs noise)
                                       │
┌──────────────────────────────────────▼───────────────────────────────────────┐
│            INTEGRATION: integrase-bot (Verified Squash Merge)                │
│                                                                              │
│   Only a screened, green clone is integrated into origin/main. Memory of     │
│   past failures is kept for the next editors (handoffs, lessons-inbox)       │
└──────────────────────────────────────────────────────────────────────────────┘
```

### The Mapping

| Gene editing | squid-bot | What it does |
|---|---|---|
| **The genome** | **`origin/main`** | The canonical sequence that every future branch inherits. Changes here are heritable. |
| **A known defect, or a trait to add** | **An issue** | A bug is a loss-of-function mutation to correct; a feature is a knock-in of a new trait. |
| **The promoter that drives the editor** (in name only: a promoter answers to one signal, promoter-bot to about ten holds that must all be clear) | **promoter-bot** | Decides which edits start, when, and how many at once; switches editing off on host stress, usage limits or lost connectivity. |
| **Editor toxicity** (too much Cas9 kills the cell) | **`box-status`** | Senses host pressure (`/proc/pressure/cpu`, `memory`, `io`) and holds new work before the host collapses. |
| **The editor** (Cas9 plus its guide RNA) | **A coding agent** with its brief | The brief is the guide: where to cut and what the corrected sequence should be. |
| **Editing in a dish, not in the patient** | **A git worktree** | Every edit happens in an isolated copy; `main` is never edited directly. |
| **Sharing the incubator** | **`heavy-run.sh`** | Gives each heavy job a fair share of the cores when it starts. |
| **The repair template; HDR vs NHEJ** | **The PR's diff** | A precise, template-guided fix (HDR) versus a quick patch that leaves side effects (NHEJ "indels"). |
| **Screening clones; off-target sequencing** | **CI: draft checks, then the full suite** | Does the edit fix the defect, and did it break anything elsewhere (off-target effects = regressions)? |
| **Genotyping a failed clone** | **`ci_digest`** | Classifies a failure (`real` / `infra` / `flake` / `main` / `dirty`) and hands the editor the exact local repro. |
| **Integrase** (writes a verified sequence into the genome) | **integrase-bot** (formerly the shepherd) | Merges a green PR into `main`; holds, reruns and flags the rest. Also: it does Continuous Integration. |
| **The physician's sign-off** (the clinician who ordered the therapy decides what the lab can't) | **comms-bot** | Takes the fleet's questions to the owner's phone in plain language, brings the decisions back, and sends a daily lab report. Uses no model tokens. |
| **Discarding a failed clone** | **Closing a broken PR** | An edit that cannot be verified never reaches the genome. |
| **Two edits at the same locus** | **Merge conflicts** | Caught as `dirty` and sent back for a rebuild against the current genome. |
| **The CRISPR array** (bacteria store spacers from past infections to recognise them again) | **Handoffs, `lessons-inbox.md`, the integrase ledger & `flake-history`** | Memory of past failures carried to the next generation of editors. |
