# Fleet CI report

A GitHub Action that shows what an agent bot changed in your repo. It publishes a live dashboard (SVG, light and dark) you can embed in a README, and writes a report to each run's summary page.

Every chart shows one mark per UTC day, under four headings in the order a reader asks: what came out (merged PRs by origin, issues), whether it was good (first-pass rate, change failure), what it cost, and how close the limits are (CI runner hours against the pool, job queue wait, runner time per merged PR, and the share of run minutes saved by early cancellation and of runs that failed). Name your bot's branch prefixes and its PRs get their own color, and its dollar spend is added up from the day it started; a line marks the day it started, through every chart. The cards show the last finished UTC day against the day before it: a fleet that changes week to week is described by its latest day, not a period average. It also publishes its source data and explains which measures are exact or sampled.

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

For runner hours you can trust, add the occupancy sampler. It takes one reading every 15 minutes of the jobs holding
a runner, across every repository of the owner, because they all share one hosted-runner pool. Run it from a host, not
a scheduled workflow: a workflow needs a runner from the pool it is measuring, so its readings skip the saturated
hours, and GitHub drops many scheduled runs under load (on gitgalaxy, 7 of 88 ran in a day). On a Linux host with `gh`
logged in:

```sh
install -m 755 scripts/occupancy-sampler.sh ~/.local/bin/squid-occupancy-sampler
cp templates/systemd/squid-occupancy.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now squid-occupancy.timer
```

It keeps its readings in `~/.local/state/squid-occupancy` and pushes `occupancy.jsonl` to the `dashboard` branch at
most hourly, merged with what is there. Set `SQUID_OCC_REPO` / `SQUID_OCC_DASHBOARD` for another repo (see the script).
About 15-60 API calls a reading on the host's `gh` token. A day the host was off for more than a quarter of it falls back
to the sampled estimate. Without a host, `occupancy-sample: "true"` in a workflow still takes one reading (it leaves
its own job out).

For refreshes at set times, start them from a host too. GitHub delays or drops scheduled runs on a busy account (on
2026-10-25 none of squid-bot's three daily slots refreshed on schedule), while a `workflow_dispatch` starts at once.
`templates/systemd/squid-dashboard.timer` dispatches `dashboard.yml` at 05:47, 14:47 and 19:52 US Eastern (edit
`OnCalendar` and the repo in the `.service` for yours); keep the workflow's own schedule as the backup:

```sh
cp templates/systemd/squid-dashboard.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now squid-dashboard.timer
```

The first run backfills 30 days of exact counts, plus the sampled detail for the 3 most recent days. Older days get their sampled detail 4 at a time on later runs (`detail-backfill`), to stay within the API budget.

### Inputs

| Input | Default | What it does |
|---|---|---|
| `repo` | the running repo | Repository to measure (`owner/repo`). Can be another public repo. |
| `token` | `github.token` | Reads the measured repo and pushes to `branch` |
| `branch` | `dashboard` | Where `dashboard.svg`, `limits.svg`, `quality.svg` and `metrics.json` are published. Empty = don't publish. |
| `days` | `30` | Days on the dashboard |
| `title` | `<repo>: CI activity` | Heading |
| `agent-branch-prefixes` | `claude/,codex/,copilot/,cursor/,devin/` | Head-branch prefixes that mark a PR as agent-made |
| `bot-branch-prefixes` | none | Head-branch prefixes of the bot the dashboard follows (e.g. `churn/`). Its PRs are classed **bot**, ahead of the agent prefixes |
| `bot-name` | `bot` | Name shown for the bot |
| `bot-start` | its first merged PR | `YYYY-MM-DD` the bot went live. The charts stretch (up to 90 days) to keep 14 days before it in view; those 14 days are the "before" baseline |
| `events` | none | Dates to mark, as `YYYY-MM-DD: label; YYYY-MM-DD: label` |
| `agent-markers` | built-in list | Regex on PR bodies that marks a PR as agent-made (Claude Code, Copilot, Devin, Cursor, Codex and Gemini CLI trailers by default) |
| `bot-workflows` | none | Workflow names that are your orchestrator's own bookkeeping, reported as a share of runs |
| `sample-fraction` | `0.05` | Share of each day's completed runs measured (stratified by workflow, across the whole day) |
| `sample-min` | `30` | Fewest runs measured a day |
| `sample-budget` | `400` | New run lookups per invocation; the rest of a day's sample comes on later runs |
| `runner-pool` | off | Concurrent runners (e.g. `20`): a dotted line on the CI hours chart marks pool x 24 h, and a day above it is reported as an estimate fault |
| `occupancy-sample` | `false` | `true`: take one occupancy reading and stop (run from its own ~15-min schedule) |
| `occupancy-scope` | `org` | `org`: every repo of the owner (they share the pool); `repo`: this one |
| `sample-runs` | — | Deprecated; read as `sample-min` |
| `detail-backfill` | `4` | Older days to fill with sampled detail per run |
| `max-wait-p90` | off | Warn when the 7-day average p90 queue wait (min) exceeds this |
| `max-runner-hours-per-pr` | off | Warn when 7-day runner hours per merged PR exceed this |
| `cost-max` | fit the data | Top of the runner-hours-per-merged-PR axis |
| `red-main-label` | none | Issue label a main-watch workflow opens when a check fails on main; drives the change-failure panel |
| `fail-on-alert` | `false` | Fail the step on a warning |

Output: `dashboard-url`, the raw URL of the published SVG.

## Method

**Exact** (counted by the GitHub API):
- **Merged PRs per day:** each PR is classed **bot** if its head branch has a bot prefix, **agent** if its head branch has an agent prefix or its body matches an agent marker. It's **automation** if a bot account opened it (dependabot, renovate, release bots), and **other** otherwise.
- **Actions runs per day**, by conclusion. Days with more than 2,500 runs are counted in 6-hour windows, since the API caps a single count at 2,500.
- **Bot workflow runs**, per configured workflow.
- **CI changes:** PR merges to the default branch that touched `.github/workflows`. They show as markers on the queue-wait chart.

**Sampled** (labelled "sampled" or "est." wherever shown): `sample-fraction` of each day's completed, non-skipped runs (at least `sample-min`), stratified by workflow and drawn across the whole day from the full run listing. Each run is measured once and kept, so a day's sample grows as it is refreshed. For each run, every job's queue wait (created → picked up by a runner) and runner time (start → end) are read. Jobs that never really ran are excluded. From that:
- queue wait median and p90
- runner minutes per day: per workflow, measured runner minutes per wall minute × the workflow's total wall time (every run's wall time is in the listing), with a 95% interval
- runner time split by workflow, and by origin: agent PR, other PR, automation PR, or default branch and schedules

### The charts

- **Delivery:** merged PRs per day, split by recorded origin (bot, other agents, other, automation). Attribution depends on configured branch prefixes or PR-body markers.
- **Change failure** (with `red-main-label`): each day, the issues with that label opened (one per check that went red on main, opened by a main-watch workflow) ÷ merged PRs, from the bot's start; the tooltip has the median hours to green. An infrastructure failure on main counts too, and one bad merge that breaks two checks counts twice: it is how often main broke per merge, not a count of bad PRs.
- **Spend** and **Cost per PR** (when the orchestrator publishes `factory.json` with `usd`): the bot's dollar cost added up since it started, and each day's dollars per merged bot PR with the overall average.
- **CI hours:** each day's runner hours. Where org-scope occupancy readings cover the day, the bar is every repo's hours, measured, in color, because the repos share one pool; the tooltip gives this repo's share. Otherwise it is this repo's own figure (its occupancy readings, else the stratified sample's estimate) in gray, and the legend names both. Days from before the sampler started can't be org-wide: the run sample only reads this repo. With `runner-pool` set, a dotted line marks what the pool can do in a day (pool x 24 h) and the axis keeps it in view, so you can see how close the pool came to full. Otherwise the axis fits three times the median day; a day above it (a sampling overshoot, or a real storm) is drawn at the top and marked with its value.
- **Speed:** each day's sampled p90 job queue wait.
- **CI load:** each day's estimated runner hours per merged PR. Set `cost-max` to cap the axis; days above it are drawn at the top and marked with their value.
- **Efficiency:** two percentages per day. **Runs failed:** failed ÷ (passed + failed) workflow runs, exact. **Run minutes saved (est.):** a run cancelled early (usually because a newer push replaced it) would have taken about as long as that day's median successful run of the same workflow; what it saved is that minus what it ran. The chart shows saved ÷ (spent + saved), in run wall-clock minutes. Runs of the configured `bot-workflows` are left out of both sides, and cancelled runs of a workflow with no successful run that day aren't priced. It is an estimate, labelled as one.

`limits.svg` is a separate sheet, published beside `dashboard.svg` when `factory.json` has the data:

- **Limit each day** and **Limit each hour** (when `factory.json` days carry `limits`): the limit in force, for each minute the first thing that, if removed, would start another bot. In order: paused on tokens, every slot busy (code generation at max), a pace hold (tokens), the box, CI, no ready work (including when only items held for the owner are left), else unknown. The bars are hours per host, so a day stays 24 h with several hosts. The cells show each hour's main limit. This only sees launches: with every slot full it says code generation, even if merges are stuck behind CI.
- **Bots allowed vs working** (when `factory.json` days carry `max_workers` and `worker_hours`): the most bots allowed that day across hosts, against the average working, split into new work and fix loops. The gap is unused capacity; the tooltip gives the idle and paused hours.

**PR quality by size** (`quality.svg`, when `factory.json` days carry a `quality` field): daily, over the 7 days ending on each day and summed across every host in `factory.json`, the share of merged bot PRs that needed a fix (a review send-back or a CI fix run) by hand-written file count (1-2, 3-4, 5, 6+), and fix runs per 100 merged PRs by cause. Plain lines; a size line is drawn where a window has 5 PRs or more. The weekly report keeps a weekly table. It is published beside `dashboard.svg`, as is `goals.svg` when `factory.json` has `goals`.

Today is drawn faded, as incomplete. The cards use finished days only.

The current GitHub API feed does not contain historical local-host PSI, agent review effort, or post-merge rework. The dashboard leaves those out rather than treating CI activity as a substitute for them.

**Days are UTC.** The last 3 days are re-fetched on every run because they can still change; finished days are kept in `metrics.json`.

### Known limits
- **Classification is only as good as the trace agents leave.** An agent that uses ordinary branch names and no trailer counts as "other". Set your fleet's own prefixes and markers.
- **Runner time:** run cost is heavy-tailed (on gitgalaxy, 2026-10-23: median run 0.5 runner-min, top 5% of runs = 58% of the minutes). A fixed small sample of runs is noise: the old 24-run sample put that day anywhere from 124 to 1,088 h against a 480 h ceiling. The stratified ratio estimate at 5% is about ±15% (95%). With the occupancy sampler running, runner hours come from the readings instead (time-weighted running jobs, used when they cover 75% of the day). That figure can't exceed the pool and needs no run sampling.
- **The runner pool is the owner's:** GitHub's concurrency limit for hosted runners is per account or org, so other repos' jobs take runners from this repo. The org-scope occupancy counts all of them (`occupancy_org`) next to this repo's share, and draws the CI hours chart. The cards and CI load stay this repo's, since they divide by its merged PRs.
- **Runner time is job wall time on GitHub's clock.** It isn't billing: public repos on standard runners are free, and larger runners bill at a multiplier.
- **GitHub-hosted Ubuntu runner only:** it uses the preinstalled `python3` and `gh`.
