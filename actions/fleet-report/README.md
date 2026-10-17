# Fleet CI report

A GitHub Action that shows what an agent bot changed in your repo. It publishes a live dashboard (SVG, light and dark) you can embed in a README, and writes a report to each run's summary page.

Every chart shows one mark per UTC day: merged PRs by origin, job queue wait, runner time per merged PR, and CI runs that failed or were cancelled. Name your bot's branch prefixes and its PRs get their own color; a shaded band marks the days it has run, through every chart, and the cards compare each measure's average with the bot against the 14 days before it. Without a bot, they compare the last 7 days with the 7 before. It also publishes its source data and explains which measures are exact or sampled.

![Example: GitGalaxy's agent fleet](https://raw.githubusercontent.com/squid-protocol/squid-bot/dashboard/dashboard.svg)

## Usage

```yaml
# .github/workflows/fleet-report.yml
name: Fleet CI report
on:
  schedule: [{cron: "17 */6 * * *"}]
  workflow_dispatch:
permissions:
  contents: write   # to publish to the `dashboard` branch
jobs:
  report:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: squid-protocol/squid-bot/actions/fleet-report@main
```

Then embed it: `![CI](https://raw.githubusercontent.com/<owner>/<repo>/dashboard/dashboard.svg)`.

The first run backfills 30 days of exact counts, plus the sampled detail for the 3 most recent days. Older days get their sampled detail 4 at a time on later runs (`detail-backfill`), to stay within the API budget.

### Inputs

| Input | Default | What it does |
|---|---|---|
| `repo` | the running repo | Repository to measure (`owner/repo`). Can be another public repo. |
| `token` | `github.token` | Reads the measured repo and pushes to `branch` |
| `branch` | `dashboard` | Where `dashboard.svg` and `metrics.json` are published. Empty = don't publish. |
| `days` | `30` | Days on the dashboard |
| `title` | `<repo>: CI activity` | Heading |
| `agent-branch-prefixes` | `claude/,codex/,copilot/,cursor/,devin/` | Head-branch prefixes that mark a PR as agent-made |
| `bot-branch-prefixes` | none | Head-branch prefixes of the bot the dashboard follows (e.g. `churn/`). Its PRs are classed **bot**, ahead of the agent prefixes |
| `bot-name` | `bot` | Name shown for the bot |
| `bot-start` | its first merged PR | `YYYY-MM-DD` the bot went live. The charts stretch (up to 90 days) to keep 14 days before it in view; those 14 days are the "before" baseline |
| `events` | none | Dates to mark, as `YYYY-MM-DD: label; YYYY-MM-DD: label` |
| `agent-markers` | built-in list | Regex on PR bodies that marks a PR as agent-made (Claude Code, Copilot, Devin, Cursor, Codex and Gemini CLI trailers by default) |
| `bot-workflows` | none | Workflow names that are your orchestrator's own bookkeeping, reported as a share of runs |
| `sample-runs` | `24` | Runs sampled per day for queue wait and runner time |
| `detail-backfill` | `4` | Older days to fill with sampled detail per run |
| `max-wait-p90` | off | Warn when the 7-day average p90 queue wait (min) exceeds this |
| `max-runner-hours-per-pr` | off | Warn when 7-day runner hours per merged PR exceed this |
| `fail-on-alert` | `false` | Fail the step on a warning |

Output: `dashboard-url`, the raw URL of the published SVG.

## Method

**Exact** (counted by the GitHub API):
- **Merged PRs per day:** each PR is classed **bot** if its head branch has a bot prefix, **agent** if its head branch has an agent prefix or its body matches an agent marker. It's **automation** if a bot account opened it (dependabot, renovate, release bots), and **other** otherwise.
- **Actions runs per day**, by conclusion. Days with more than 2,500 runs are counted in 6-hour windows, since the API caps a single count at 2,500.
- **Bot workflow runs**, per configured workflow.
- **CI changes:** PR merges to the default branch that touched `.github/workflows`. They show as markers on the queue-wait chart.

**Sampled** (labelled "sampled" or "est." wherever shown): about `sample-runs` completed runs a day, spread over four 6-hour windows. For each run, every job's queue wait (created → picked up by a runner) and runner time (start → end) are read. Jobs that never really ran are excluded. From that:
- queue wait median and p90
- runner minutes per day (mean per run × the day's non-skipped runs)
- runner time split by workflow, and by origin: agent PR, other PR, automation PR, or default branch and schedules

### The four charts

- **Delivery:** merged PRs per day, split by recorded origin (bot, other agents, other, automation). Attribution depends on configured branch prefixes or PR-body markers.
- **Speed:** each day's sampled p90 job queue wait.
- **Cost:** each day's estimated runner hours per merged PR.
- **Waste:** each day's failed and cancelled workflow runs. Hover a day for its pass rate (success / success + failure), a run-level signal: retries count as separate runs, so it is not first-pass PR success.

Today is drawn faded, as incomplete. The "with bot" averages and cards use finished days only.

The current GitHub API feed does not contain historical local-host PSI, agent review effort, or post-merge rework. The dashboard leaves those out rather than treating CI activity as a substitute for them.

**Days are UTC.** The last 3 days are re-fetched on every run because they can still change; finished days are kept in `metrics.json`.

### Known limits
- **Classification is only as good as the trace agents leave.** An agent that uses ordinary branch names and no trailer counts as "other". Set your fleet's own prefixes and markers.
- **Per-day sampled figures are noisy:** a few long runs dominate a 24-run sample. Read them as weekly aggregates, which is what the tiles and report do. Raise `sample-runs` for tighter estimates.
- **Runner time is job wall time on GitHub's clock.** It isn't billing: public repos on standard runners are free, and larger runners bill at a multiplier.
- **GitHub-hosted Ubuntu runner only:** it uses the preinstalled `python3` and `gh`.
