#!/usr/bin/env python3
"""Fleet CI report: what an agent fleet costs and congests in GitHub Actions, per unit of output.

Counted exactly (GitHub API):
  merged PRs per day      GraphQL search, each PR classed agent / automation / other (see classify_pr)
  Actions runs per day     actions/runs?created=DAY, total and per conclusion
  bot workflow runs        per configured bot workflow (an orchestrator's own bookkeeping), if any
  CI changes merged        PR merges to the default branch that touched .github/workflows (chart markers)
  open PRs right now       search API
Sampled (about --sample-runs runs a day, spread over the day; labelled "sampled" / "est." wherever shown):
  job queue wait           job created -> picked up by a runner, median and p90
  runner minutes           mean job minutes per run x the day's runs, split by workflow and by origin
                           (agent PR / other PR / automation PR / default branch and schedules)

History accumulates in <out>/metrics.json: a finished day is fetched once, the last REFRESH_DAYS days are
re-fetched on every run, and older days get the sampled detail a few per run (--detail-backfill).
Writes <out>/dashboard.svg (light/dark) and, with --summary, a Markdown report (the Action points it at
$GITHUB_STEP_SUMMARY). Needs `gh`, authenticated or with GH_TOKEN set.

Usage:
    python fleet_report.py --repo owner/repo [--out dashboard] [--summary report.md]
    python fleet_report.py --render-only --out dashboard     # redraw from metrics.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

REFRESH_DAYS = 3  # today, yesterday and the day before can still change
OUTCOMES = ("success", "failure", "cancelled", "skipped")
DETAIL_BACKFILL = 4  # older days missing the sampled detail, filled a few per run (API budget)
# Runner time, 2026-10-24 rework. A fixed 24 runs a day, from page 1 of four 6-hour windows, was mostly noise: run cost
# is heavy-tailed (gitgalaxy 2026-10-23: median 0.5 runner-min, the top 5% of runs carry 58% of the minutes, CV 3.5), so
# 24 runs put the day anywhere in 124-1,088 h (the pool's ceiling was 480), and page 1 only covers the last minutes of
# each window. Now: a FRACTION of the day's runs, stratified by workflow across the whole day, priced with a ratio
# estimator on each run's wall time (known for every run from the listing), with a 95% interval; and, when an
# occupancy sampler runs, runner hours from time-sampled running jobs, which cannot exceed the pool.
EST_METHOD = "ratio-v2"
OCC_FILE = "occupancy.jsonl"  # one reading per line, appended by --occupancy-sample (the occupancy workflow)
OCC_MAX_GAP_MIN = 45  # readings further apart than this leave the gap uncovered
OCC_MIN_COVERAGE = 0.75  # a day's occupancy figure is used when its readings cover this share of the day
OCC_KEEP_DAYS = 35
SAMPLE_KEEP_DAYS = 14  # per-run sample detail kept this long (later days keep only their estimates)
DEFAULT_AGENT_PREFIXES = "claude/,codex/,copilot/,cursor/,devin/"
DEFAULT_AGENT_MARKERS = (r"Generated with \[?Claude Code|Co-Authored-By: Claude|Co-authored-by: Copilot|"
                         r"agy-authored|Devin AI|Cursor Agent|OpenAI Codex|Gemini CLI")
CLASSES = ("bot", "agent", "other", "automation")
SHOW_BEFORE = 14  # days before the bot started kept on the charts and used as its "before" baseline
MAX_DAYS = 90


def _csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def _events(s: str) -> list[tuple[str, str]]:
    """'2026-10-10: shepherd on; 2026-10-12: paused' -> [(day, label), ...]"""
    out = []
    for item in s.split(";"):
        day, _, label = item.partition(":")
        if day.strip() and label.strip():
            out.append((day.strip(), label.strip()))
    return out


@dataclass
class Config:
    repo: str
    title: str = ""
    bot_workflows: list[str] = field(default_factory=list)
    agent_prefixes: list[str] = field(default_factory=lambda: _csv(DEFAULT_AGENT_PREFIXES))
    agent_markers: str = DEFAULT_AGENT_MARKERS
    sample_fraction: float = 0.05  # share of the day's completed runs measured (so precision holds as volume grows)
    sample_min: int = 30  # ...but never fewer runs than this
    sample_budget: int = 400  # new run lookups per invocation, across all days (API budget); the rest come next run
    runner_pool: int | None = None  # concurrent runners available; a day's estimate above pool x 24 h is flagged
    bot_prefixes: list[str] = field(default_factory=list)
    bot_name: str = "bot"
    bot_start: str = ""
    events: list[tuple[str, str]] = field(default_factory=list)
    cost_max: float | None = None  # top of the cost-per-PR axis; days above it are drawn at the top and marked

    @property
    def heading(self) -> str:
        return self.title or f"{self.repo}: CI activity"


# ---------------------------------------------------------------- GitHub API

def _gh(cmd: list[str], tries: int = 3) -> str:
    """Runs a gh call, retrying transient failures (a 5xx or a dropped connection) with a short backoff."""
    for attempt in range(tries):
        out = subprocess.run(cmd, capture_output=True, text=True)
        if out.returncode == 0:
            return out.stdout
        if attempt < tries - 1:
            time.sleep(2 * 3 ** attempt)
    raise RuntimeError(f"{' '.join(cmd[:3])} {cmd[-1][:120]} failed: {out.stderr.strip()[:200]}")


def gh_json(path: str):
    return json.loads(_gh(["gh", "api", "-H", "Accept: application/vnd.github+json", path]))


def gh_count(path: str) -> int:
    return int(gh_json(path)["total_count"])


def gh_graphql(query: str, **variables: str) -> dict:
    cmd = ["gh", "api", "graphql"]
    for k, v in variables.items():
        cmd += ["-f", f"{k}={v}"]
    return json.loads(_gh(cmd + ["-f", f"query={query}"]))["data"]


def _iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


# ---------------------------------------------------------------- classification

def classify_pr(pr: dict, cfg: Config) -> str:
    """bot: one of the bot's own branch prefixes (the orchestrator this dashboard follows); agent: an agent branch
    prefix or an agent marker in the body; automation: any other bot account (dependabot, renovate, release bots);
    other: everything else (people, or agents that leave no trace)."""
    branch = pr.get("headRefName") or ""
    if any(branch.startswith(p) for p in cfg.bot_prefixes):
        return "bot"
    if any(branch.startswith(p) for p in cfg.agent_prefixes):
        return "agent"
    if cfg.agent_markers and re.search(cfg.agent_markers, pr.get("bodyText") or "", re.I):
        return "agent"
    author = pr.get("author") or {}
    if author.get("__typename") == "Bot" or str(author.get("login", "")).endswith("[bot]"):
        return "automation"
    return "other"


SEARCH_PRS = """query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { number headRefName bodyText author { login __typename } } }
  }
}"""


def merged_prs(repo: str, day: str) -> list[dict]:
    prs, after = [], None
    while True:
        vars_ = {"q": f"repo:{repo} is:pr is:merged merged:{day}"}
        if after:
            vars_["after"] = after
        s = gh_graphql(SEARCH_PRS, **vars_)["search"]
        prs += [n for n in s["nodes"] if n]
        if not s["pageInfo"]["hasNextPage"]:
            return prs
        after = s["pageInfo"]["endCursor"]


def merged_counts(repo: str, day: str, cfg: Config) -> dict:
    c = Counter(classify_pr(p, cfg) for p in merged_prs(repo, day))
    return {"merged": sum(c.values()), "merged_by_class": {k: c.get(k, 0) for k in CLASSES}}


class RunOrigin:
    """Which kind of PR a workflow run worked for: one PR lookup per PR number, cached for the whole collect."""

    def __init__(self, repo: str, cfg: Config):
        self.repo, self.cfg, self.cache = repo, cfg, {}
        self.owner = repo.split("/")[0]
        self.default_branch = gh_json(f"repos/{repo}")["default_branch"]

    def _classify(self, p: dict) -> str:
        return classify_pr({"headRefName": p["head"]["ref"], "bodyText": p.get("body") or "",
                            "author": {"login": p["user"]["login"],
                                       "__typename": "Bot" if p["user"]["type"] == "Bot" else "User"}}, self.cfg)

    def __call__(self, run: dict) -> str:
        branch = run.get("head_branch") or ""
        if branch == self.default_branch or run.get("event") in ("schedule", "dynamic") or branch.startswith("gh-pages"):
            return "main"
        prs = run.get("pull_requests") or []
        # GitHub often leaves pull_requests empty even on PR runs: then find the PR by its head branch
        key = f"#{prs[0]['number']}" if prs else f"branch:{branch}"
        if key not in self.cache:
            if prs:
                found = [gh_json(f"repos/{self.repo}/pulls/{prs[0]['number']}")]
            else:
                found = gh_json(f"repos/{self.repo}/pulls?head={self.owner}:{branch}&state=all&per_page=1")
            self.cache[key] = self._classify(found[0]) if found else classify_pr({"headRefName": branch}, self.cfg)
        return self.cache[key]


# ---------------------------------------------------------------- fetching

def bot_workflow_ids(repo: str, cfg: Config) -> dict[str, int]:
    if not cfg.bot_workflows:
        return {}
    wfs = gh_json(f"repos/{repo}/actions/workflows?per_page=100")["workflows"]
    return {w["name"]: w["id"] for w in wfs if w["name"] in cfg.bot_workflows}


def day_runs(repo: str, day: str) -> list[dict]:
    """Every workflow run created on the day."""
    out = []
    for h in range(0, 24, 2):  # a listing stops at 1,000 runs: 2-hour windows stay under it on a busy day
        span = f"{day}T{h:02d}:00:00Z..{day}T{h + 1:02d}:59:59Z"
        for page in range(1, 11):
            runs = gh_json(f"repos/{repo}/actions/runs?created={span}&per_page=100&page={page}")["workflow_runs"]
            out += runs
            if len(runs) < 100:
                break
    return out


def rerun_counts(repo: str, day: str, runs: list[dict] | None = None) -> dict:
    """Exact: the day's runs that were rerun (run_attempt > 1) and how their latest attempt ended, per workflow.
    A rerun that failed again usually retried a real failure; one that passed was a flake or an outage."""
    by_wf: dict[str, list[int]] = {}
    attempts = 0
    for r in day_runs(repo, day) if runs is None else runs:
        if (r.get("run_attempt") or 1) > 1 and r.get("status") == "completed":
            attempts += r["run_attempt"] - 1
            c = by_wf.setdefault(r["name"], [0, 0, 0])
            c[{"success": 0, "failure": 1}.get(r.get("conclusion"), 2)] += 1
    return {"runs": sum(sum(c) for c in by_wf.values()), "attempts": attempts,
            "passed": sum(c[0] for c in by_wf.values()), "failed_again": sum(c[1] for c in by_wf.values()),
            "by_workflow": by_wf}


def minutes_saved(runs: list[dict], skip: set[str]) -> dict:
    """Estimated run minutes that early cancellation saved, in run wall time (start to finish). A cancelled run would
    have taken about as long as the day's median successful run of the same workflow; what it saved is that minus what
    it ran. Workflows in `skip` (an orchestrator's bookkeeping) are left out of both sides. Cancelled runs of a workflow
    with no successful run that day can't be priced and are counted as `unpriced`."""
    def wall(r: dict) -> float:
        return max(0.0, (_iso(r["updated_at"]) - _iso(r["run_started_at"])).total_seconds() / 60)
    done = [r for r in runs if r.get("status") == "completed" and r.get("conclusion") != "skipped"
            and r.get("run_started_at") and r.get("updated_at") and r.get("name") not in skip]
    full: dict[str, list[float]] = {}
    for r in done:
        if r.get("conclusion") == "success":
            full.setdefault(r["name"], []).append(wall(r))
    saved, unpriced, cancelled = 0.0, 0, 0
    for r in done:
        if r.get("conclusion") == "cancelled":
            cancelled += 1
            if r["name"] in full:
                saved += max(0.0, _pct(full[r["name"]], 0.5) - wall(r))
            else:
                unpriced += 1
    return {"wall_min": round(sum(wall(r) for r in done)), "saved_min_est": round(saved),
            "cancelled": cancelled, "unpriced": unpriced}


def run_wall(r: dict) -> float | None:
    """A run's wall minutes, start to last update (from the listing, no extra call)."""
    if not (r.get("run_started_at") and r.get("updated_at")):
        return None
    return max(0.0, (_iso(r["updated_at"]) - _iso(r["run_started_at"])).total_seconds() / 60)


def sample_population(runs: list[dict]) -> list[dict]:
    """The runs runner time is estimated over: completed, not skipped, with a wall time."""
    return [{"id": r["id"], "name": r.get("name") or "?", "wall": w} for r in runs
            if r.get("status") == "completed" and r.get("conclusion") != "skipped" and (w := run_wall(r)) is not None]


def sample_plan(pop: list[dict], have, cfg: Config, seed: str) -> list[dict]:
    """The runs still to measure: target max(sample_min, sample_fraction x N), allocated to workflows in proportion to
    their run counts with at least one per workflow, drawn by a per-day seeded shuffle (a re-run keeps its picks), and
    ordered round-robin across workflows so a partial budget still covers every workflow."""
    n_target = min(len(pop), max(cfg.sample_min, math.ceil(cfg.sample_fraction * len(pop))))
    by: dict[str, list[dict]] = {}
    for r in pop:
        by.setdefault(r["name"], []).append(r)
    # largest remainder, so the shares add up to the target; then at least one per workflow
    quota = {wf: n_target * len(rs) / max(1, len(pop)) for wf, rs in by.items()}
    alloc = {wf: math.floor(q) for wf, q in quota.items()}
    for wf in sorted(quota, key=lambda w: (alloc[w] - quota[w], w))[: n_target - sum(alloc.values())]:
        alloc[wf] += 1
    queues = []
    for wf in sorted(by):
        rs = sorted(by[wf], key=lambda r: r["id"])
        random.Random(f"{seed}:{wf}").shuffle(rs)
        n_w = min(len(rs), max(1, alloc[wf]))
        queues.append([r for r in rs[:n_w] if str(r["id"]) not in have])
    out = []
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out


def ratio_estimate(pop: list[dict], measured: dict) -> dict:
    """Runner minutes for the whole population from the measured runs ({id: {"m": minutes, ...}}): per workflow, the
    measured runner-min per wall-min times the workflow's total wall time (a separate ratio where >= 3 runs are
    measured, one combined ratio over the rest, including workflows with none measured). Returns the total, the 95%
    half-width, per-workflow totals and a weight per measured run (so any split of the runs sums to the total)."""
    by: dict[str, list[dict]] = {}
    for r in pop:
        by.setdefault(r["name"], []).append(r)
    sep, small = {}, []
    for wf, rs in by.items():
        got = [(r, float(measured[str(r["id"])]["m"])) for r in rs if str(r["id"]) in measured]
        (sep.__setitem__(wf, got) if len(got) >= 3 else small.append(wf))
    total, var, by_wf, weights = 0.0, 0.0, {}, {}

    def group(wfs: list[str], got: list[tuple[dict, float]]) -> None:
        nonlocal total, var
        n_pop = sum(len(by[w]) for w in wfs)
        x_pop = sum(r["wall"] for w in wfs for r in by[w])
        n, xs, ys = len(got), sum(r["wall"] for r, _ in got), sum(y for _, y in got)
        if not n:
            return
        if xs > 0:
            ratio, wt = ys / xs, x_pop / xs
            resid = [y - ratio * r["wall"] for r, y in got]
        else:  # no wall time to scale by: the mean per run
            ratio, wt = None, n_pop / n
            resid = [y - ys / n for _, y in got]
        for w in wfs:
            by_wf[w] = (ratio * sum(r["wall"] for r in by[w])) if ratio is not None else ys / n * len(by[w])
        total += ys * wt
        for r, _ in got:
            weights[str(r["id"])] = wt
        if n > 1:
            mean_r = sum(resid) / n
            s2 = sum((d - mean_r) ** 2 for d in resid) / (n - 1)
            var += n_pop ** 2 * (1 - n / n_pop) * s2 / n

    for wf, got in sep.items():
        group([wf], got)
    group(small, [(r, float(measured[str(r["id"])]["m"])) for w in small for r in by[w] if str(r["id"]) in measured])
    return {"total": total, "half_width": 1.96 * math.sqrt(var), "by_workflow": by_wf, "weights": weights}


def measure_run(repo: str, run_id: int) -> dict:
    """One run's runner minutes and its jobs' queue waits (one API call)."""
    minutes, waits = 0.0, []
    for j in gh_json(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")["jobs"]:
        if j.get("started_at") and j.get("completed_at") and j.get("conclusion") != "skipped":
            m = (_iso(j["completed_at"]) - _iso(j["started_at"])).total_seconds() / 60
            if m > 0.05:  # a job that never really ran held no runner
                minutes += m
                waits.append(round(max(0.0, (_iso(j["started_at"]) - _iso(j["created_at"])).total_seconds() / 60), 2))
    return {"m": round(minutes, 2), "w": waits}


def fetch_detail(repo: str, day: str, bots: dict[str, int], origin, cfg: Config, prev: dict | None = None,
                 budget: list[int] | None = None) -> dict:
    """Bot-run count and CI-change PRs (exact); job queue wait and runner minutes (a stratified sample of
    sample_fraction of the day's runs, kept in `sample` and topped up while the day is refreshed, so each run is
    measured once; `budget` is the lookups left this invocation); minutes saved by early cancellation (estimated from
    every run of the day)."""
    everything = day_runs(repo, day)
    by_id = {x["id"]: x for x in everything}
    bot_runs = sum(gh_count(f"repos/{repo}/actions/workflows/{i}/runs?created={day}&per_page=1") for i in bots.values())
    changes: dict[int, str] = {}
    for c in gh_json(f"repos/{repo}/commits?path=.github/workflows&since={day}T00:00:00Z&until={day}T23:59:59Z&per_page=100"):
        title = c["commit"]["message"].splitlines()[0]
        m = re.search(r"\(#(\d+)\)\s*$", title)
        if m:  # squash merges of PRs; direct bot commits carry no PR number
            changes[int(m.group(1))] = title
    pop = sample_population(everything)
    prev = prev or {}
    measured = dict(prev.get("sample") or {}) if prev.get("est_method") == EST_METHOD else {}
    budget = budget if budget is not None else [cfg.sample_budget]
    for r in sample_plan(pop, measured, cfg, seed=day):
        if budget[0] <= 0:
            break
        try:
            measured[str(r["id"])] = {**measure_run(repo, r["id"]), "o": origin(by_id[r["id"]])}
        except RuntimeError as e:  # one unreadable run shrinks the sample, it doesn't stop the report
            print(f"::notice::skipped run {r['id']} in the sample: {e}", file=sys.stderr)
        budget[0] -= 1
    alive = {str(r["id"]) for r in pop}
    measured = {k: v for k, v in measured.items() if k in alive}
    est = ratio_estimate(pop, measured)
    waits = [w for v in measured.values() for w in v.get("w", [])]
    by_origin: Counter[str] = Counter()
    for k, v in measured.items():
        by_origin[v.get("o", "?")] += float(v["m"]) * est["weights"].get(k, 0.0)
    target = min(len(pop), max(cfg.sample_min, math.ceil(cfg.sample_fraction * len(pop))))
    return {
        "bot_runs": bot_runs,
        "ci_changes": [{"pr": n, "title": t} for n, t in sorted(changes.items())],
        "wait_median": round(_pct(waits, 0.5), 2),
        "wait_p90": round(_pct(waits, 0.9), 2),
        "est_method": EST_METHOD,
        "runner_min_est": round(est["total"]) if measured else None,
        "runner_min_ci": round(est["half_width"]) if measured else None,
        "population": len(pop),
        "sampled_runs": len(measured),
        "sample_target": target,
        "sampled_min_by_workflow": {k: round(v, 1) for k, v in est["by_workflow"].items()},
        "sampled_min_by_origin": {k: round(v, 1) for k, v in by_origin.items()},
        "sample": measured,
        "reruns": rerun_counts(repo, day, everything),
        "cancel_savings": minutes_saved(everything, set(bots)),
    }


def count_runs(repo: str, day: str, query: str = "") -> int:
    # total_count tops out at 2,500 per query, a busy day passes that: count it in 6-hour windows
    return sum(
        gh_count(f"repos/{repo}/actions/runs?created={day}T{h:02d}:00:00Z..{day}T{h + 5:02d}:59:59Z{query}&per_page=1")
        for h in (0, 6, 12, 18)
    )


def fetch_day(repo: str, day: str, cfg: Config) -> dict:
    runs = {"total": count_runs(repo, day)}
    for outcome in OUTCOMES:
        runs[outcome] = count_runs(repo, day, f"&status={outcome}")
    return {**merged_counts(repo, day, cfg), "runs": runs}


def days_to_fetch(have: dict, today: date, days: int) -> list[str]:
    window = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    fresh = {(today - timedelta(days=i)).isoformat() for i in range(REFRESH_DAYS)}
    return [d for d in window if d not in have or d in fresh]


def collect(cfg: Config, days: int, data: dict, today: date, backfill: int = DETAIL_BACKFILL) -> dict:
    repo = cfg.repo
    if data.get("repo") != repo:
        data = {"repo": repo, "days": {}}
    days = shown_days(data, cfg, days, today)
    fresh = days_to_fetch(data["days"], today, days)
    for d in fresh:
        data["days"][d] = fetch_day(repo, d, cfg)
    window = [(today - timedelta(days=i)).isoformat() for i in range(days)]
    for d in window:  # history from before PR classing (or before the bot class): one GraphQL query a day fills it in
        classes = data["days"].get(d, {}).get("merged_by_class")
        if d in data["days"] and (classes is None or (cfg.bot_prefixes and "bot" not in classes)):
            data["days"][d].update(merged_counts(repo, d, cfg))
    bots = bot_workflow_ids(repo, cfg)
    origin = RunOrigin(repo, cfg)
    def incomplete(det: dict) -> bool:  # missing, from the old 24-run estimator, or short of its sample target
        return (not {"sampled_min_by_origin", "reruns", "cancel_savings"} <= det.keys()
                or det.get("est_method") != EST_METHOD or det.get("sampled_runs", 0) < det.get("sample_target", 0))
    missing = [d for d in window if d not in fresh and d in data["days"] and incomplete(data["days"][d].get("detail", {}))]
    budget = [cfg.sample_budget]
    for d in fresh + missing[:backfill]:
        try:
            data["days"][d]["detail"] = fetch_detail(repo, d, bots, origin, cfg, data["days"][d].get("detail"), budget)
        except RuntimeError as e:  # keep the day's previous detail (if any); the next run tries again
            print(f"::warning::sampled detail for {d} not refreshed: {e}", file=sys.stderr)
    keep = (today - timedelta(days=SAMPLE_KEEP_DAYS)).isoformat()
    for d, row in data["days"].items():  # old days keep their estimates, not the per-run detail
        if d < keep:
            row.get("detail", {}).pop("sample", None)
    try:
        data["scorecard"] = collect_scorecard(cfg, data, today, days)
    except RuntimeError as e:  # keep the previous issue counts; the next run tries again
        print(f"::warning::issue counts not refreshed: {e}", file=sys.stderr)
    data["open_prs"] = gh_count(f"search/issues?q=repo:{repo}+is:pr+is:open&per_page=1")
    data["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    return data


# ---------------------------------------------------------------- occupancy (time-sampled runner use)

def org_repos(owner: str) -> list[str]:
    """The owner's repositories the token can see (archived and disabled ones left out): an org, else a user."""
    out: list[str] = []
    for base in (f"orgs/{owner}/repos", f"users/{owner}/repos"):
        try:
            for page in range(1, 11):
                got = gh_json(f"{base}?per_page=100&page={page}&type=all")
                out += [r["full_name"] for r in got if not r.get("archived") and not r.get("disabled")]
                if len(got) < 100:
                    break
            return out
        except RuntimeError:
            out = []
    return out


def occupancy_reading(repo: str, scope: str = "org") -> dict:
    """Jobs holding a runner right now, and jobs waiting for one. Owner policy 2026-10-24: the hosted-runner pool is
    shared by every repository of the owner, so scope "org" counts them all (two calls per repo: its in-progress runs
    and its queued count; one per in-progress run for its jobs); "repo" counts `repo` only. by_repo keeps each
    repository's share."""
    repos = org_repos(repo.split("/")[0]) if scope == "org" else [repo]
    if repo not in repos:
        repos.append(repo)
    by_repo: dict[str, dict] = {}
    unread = 0
    for name in repos:
        try:  # by status, not the newest N: on a busy repo the newest runs are all freshly queued (2026-10-24 reading)
            live = []
            for page in range(1, 4):
                got = gh_json(f"repos/{name}/actions/runs?status=in_progress&per_page=100&page={page}")["workflow_runs"]
                live += got
                if len(got) < 100:
                    break
            queued_runs = int(gh_json(f"repos/{name}/actions/runs?status=queued&per_page=1").get("total_count", 0))
        except RuntimeError:
            unread += 1
            continue
        running = queued = 0
        for r in live:
            try:
                jobs = gh_json(f"repos/{name}/actions/runs/{r['id']}/jobs?per_page=100&filter=latest")["jobs"]
            except RuntimeError:
                continue
            running += sum(j.get("status") == "in_progress" for j in jobs)
            queued += sum(j.get("status") in ("queued", "waiting", "pending") for j in jobs)
        if running or queued or queued_runs:
            by_repo[name] = {"running": running, "queued": queued, "queued_runs": queued_runs}
    return {"t": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "scope": scope,
            "running": sum(v["running"] for v in by_repo.values()), "queued": sum(v["queued"] for v in by_repo.values()),
            "queued_runs": sum(v["queued_runs"] for v in by_repo.values()), "repos_read": len(repos) - unread,
            **({"repos_unread": unread} if unread else {}), "by_repo": by_repo}


def read_occupancy(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def occupancy_day(readings: list[dict], day: str, now: datetime | None = None, repo: str | None = None) -> dict | None:
    """Runner minutes for one UTC day from occupancy readings: the running-jobs count integrated over time (trapezoid
    between readings at most OCC_MAX_GAP_MIN apart; each reading also holds half a gap on its open sides), the share of
    the day (so far, for today) those readings cover, and the minutes scaled up from the covered part. None without
    readings. Bounded by construction: a reading can't show more jobs than there are runners."""
    t0 = _iso(f"{day}T00:00:00Z")
    t1 = min(t0 + timedelta(days=1), now or datetime.now(timezone.utc))
    def value(r: dict) -> float | None:  # the whole pool, or one repository's share (a repo-scope reading is that repo)
        if repo is None or "by_repo" not in r:
            return r.get("running")
        return float((r["by_repo"].get(repo) or {}).get("running", 0))
    pts = sorted((_iso(r["t"]), float(v)) for r in readings
                 if r.get("t") and t0 <= _iso(r["t"]) < t1 and (v := value(r)) is not None)
    if not pts or t1 <= t0:
        return None
    covered = integral = 0.0
    half = OCC_MAX_GAP_MIN / 2
    for (ta, a), (tb, b) in itertools.pairwise(pts):
        gap = (tb - ta).total_seconds() / 60
        if gap <= OCC_MAX_GAP_MIN:
            covered += gap
            integral += gap * (a + b) / 2
        else:  # a long gap: each side keeps half a max gap
            covered += 2 * half
            integral += half * (a + b)
    for t, v, edge in ((pts[0][0], pts[0][1], t0), (pts[-1][0], pts[-1][1], t1)):
        m = min(half, abs((t - edge).total_seconds()) / 60)
        covered += m
        integral += m * v
    span = (t1 - t0).total_seconds() / 60
    coverage = min(1.0, covered / span)
    return {"runner_min": round(integral / covered * span) if covered else None, "coverage": round(coverage, 3),
            "readings": len(pts), "running_mean": round(integral / covered, 1) if covered else None}


def runner_minutes(detail: dict) -> float | None:
    """The day's best runner-minute figure: time-sampled occupancy when its readings cover enough of the day, else the
    stratified sample's estimate."""
    occ = detail.get("occupancy") or {}
    if occ.get("runner_min") is not None and occ.get("coverage", 0) >= OCC_MIN_COVERAGE:
        return occ["runner_min"]
    return detail.get("runner_min_est")


def runner_source(detail: dict) -> str:
    occ = detail.get("occupancy") or {}
    if occ.get("runner_min") is not None and occ.get("coverage", 0) >= OCC_MIN_COVERAGE:
        return f"occupancy ({occ['coverage']:.0%} of the day)"
    if detail.get("runner_min_est") is None:
        return "no data"
    if detail.get("est_method") != EST_METHOD:
        return f"old fixed sample of {detail.get('sampled_runs', '?')} runs, no interval (re-sampled on a later run)"
    ci = detail.get("runner_min_ci")
    pct = f" ±{ci / detail['runner_min_est']:.0%}" if ci and detail["runner_min_est"] else ""
    return f"sample of {detail.get('sampled_runs', 0)}/{detail.get('population', '?')} runs{pct}"


def over_ceiling(detail: dict, pool: int | None, minutes_in_day: float = 1440) -> bool:
    """Above what `pool` runners can do in a day: this repo's figure, or the whole owner's when it was measured."""
    m = runner_minutes(detail)
    org = (detail.get("occupancy_org") or {}).get("runner_min")
    return bool(pool) and any(x is not None and x > pool * minutes_in_day for x in (m, org))


# ---------------------------------------------------------------- aggregation

def bot_start(data: dict, cfg: Config) -> str | None:
    """The day the bot went live: configured, else the first day with a bot-made merge."""
    if cfg.bot_start:
        return cfg.bot_start
    live = sorted(d for d, r in data.get("days", {}).items() if (r.get("merged_by_class") or {}).get("bot"))
    return live[0] if live else None


def shown_days(data: dict, cfg: Config, days: int, today: date) -> int:
    """Days on the charts: the configured window, stretched so the bot's start and SHOW_BEFORE days before it stay in view."""
    start = bot_start(data, cfg)
    if not start or start > today.isoformat():
        return days
    return min(MAX_DAYS, max(days, (today - date.fromisoformat(start)).days + SHOW_BEFORE + 1))


def compare(data: dict, cfg: Config, days: int, today: date) -> dict:
    """The two periods the cards and the report compare: with the bot vs the SHOW_BEFORE days before it, or (no bot)
    the last 7 finished days vs the 7 before."""
    start = bot_start(data, cfg)
    if start and start <= today.isoformat():
        rows = sorted(data["days"].items())
        first = (date.fromisoformat(start) - timedelta(days=SHOW_BEFORE)).isoformat()
        before = [(d, r) for d, r in rows if first <= d < start]
        with_bot = [(d, r) for d, r in rows if start <= d < today.isoformat()]
        if before or with_bot:
            return {"start": start, "cur": week_stats([r for _, r in with_bot]), "prev": week_stats([r for _, r in before]),
                    "cur_days": [d for d, _ in with_bot], "prev_days": [d for d, _ in before],
                    "cur_label": f"with {cfg.bot_name}", "prev_label": f"before {cfg.bot_name}"}
    done = [(d, r) for d, r in window_rows(data, days, today) if d != today.isoformat()]
    return {"start": None, "cur": week_stats([r for _, r in done[-7:]]), "prev": week_stats([r for _, r in done[-14:-7]]),
            "cur_days": [d for d, _ in done[-7:]], "prev_days": [d for d, _ in done[-14:-7]],
            "cur_label": "last 7 days", "prev_label": "previous 7 days"}


def last_two_days(data: dict, today: date) -> tuple[str | None, str | None]:
    """The last finished UTC day with data and the one before it: the cards' figures (the fleet changes too fast for
    a period average to describe today)."""
    done = sorted(d for d in data.get("days", {}) if d < today.isoformat())
    return (done[-1] if done else None), (done[-2] if len(done) > 1 else None)


def window_rows(data: dict, days: int, today: date) -> list[tuple[str, dict]]:
    window = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    return [(d, data["days"][d]) for d in window if d in data["days"]]


def finished(rows: list[tuple[str, dict]], today: date) -> list[dict]:
    return [r for d, r in rows if d != today.isoformat()]


def week_stats(rows: list[dict]) -> dict:
    """Aggregates over finished days; None where a figure has no data."""
    n = len(rows)
    if not n:
        return {}
    merged = sum(r["merged"] for r in rows)
    classed = [r for r in rows if "merged_by_class" in r]
    agent = sum(r["merged_by_class"].get("agent", 0) + r["merged_by_class"].get("bot", 0) for r in classed)
    bot = sum(r["merged_by_class"].get("bot", 0) for r in classed)
    runs = sum(r["runs"]["total"] for r in rows)
    ok = sum(r["runs"]["success"] for r in rows)
    bad = sum(r["runs"]["failure"] for r in rows)
    det = [r["detail"] for r in rows if "detail" in r]
    est = [(runner_minutes(r["detail"]), r["merged"]) for r in rows if runner_minutes(r.get("detail", {})) is not None]
    return {
        "days": n,
        "merged_per_day": merged / n,
        "agent_share": agent / max(1, sum(r["merged"] for r in classed)) if classed else None,
        "bot_merged": bot,
        "bot_merge_share": bot / max(1, sum(r["merged"] for r in classed)) if classed else None,
        "runs_per_day": runs / n,
        "pass_rate": ok / (ok + bad) if ok + bad else None,
        "fail_rate": bad / (ok + bad) if ok + bad else None,
        **saved_stats([x["cancel_savings"] for x in det if "cancel_savings" in x]),
        "wait_median": sum(x["wait_median"] for x in det) / len(det) if det else None,
        "wait_p90": sum(x["wait_p90"] for x in det) / len(det) if det else None,
        "runner_h_per_pr": sum(m for m, _ in est) / max(1, sum(k for _, k in est)) / 60 if est else None,
        "bot_share": sum(x["bot_runs"] for x in det) / max(1, sum(r["runs"]["total"] for r in rows if "detail" in r)) if det else None,
        "ci_changes": [c for x in det for c in x["ci_changes"]],
        **rerun_stats(det),
    }


def saved_share(c: dict) -> float | None:
    """Share of the minutes the runs would have taken that early cancellation saved (estimated)."""
    total = c["wall_min"] + c["saved_min_est"]
    return c["saved_min_est"] / total if total else None


def saved_stats(cs: list[dict]) -> dict:
    if not cs:
        return {}
    merged = {"wall_min": sum(c["wall_min"] for c in cs), "saved_min_est": sum(c["saved_min_est"] for c in cs)}
    return {"saved_share": saved_share(merged), "saved_min_per_day": merged["saved_min_est"] / len(cs)}


def rerun_stats(det: list[dict]) -> dict:
    """Reruns per day and the share whose latest attempt passed, over the days that counted them."""
    rr = [x["reruns"] for x in det if "reruns" in x]
    if not rr:
        return {"reruns_per_day": None, "rerun_pass_rate": None}
    passed, failed = sum(x["passed"] for x in rr), sum(x["failed_again"] for x in rr)
    return {"reruns_per_day": sum(x["runs"] for x in rr) / len(rr),
            "rerun_pass_rate": passed / (passed + failed) if passed + failed else None}


def rerun_split(rows: list[dict]) -> list[tuple[str, int, int, int]]:
    """(workflow, reruns, passed, failed again) over the rows, most rerun first."""
    c: dict[str, list[int]] = {}
    for r in rows:
        for k, v in r.get("detail", {}).get("reruns", {}).get("by_workflow", {}).items():
            s = c.setdefault(k, [0, 0, 0])
            for i in range(3):
                s[i] += v[i]
    return sorted(((k, sum(v), v[0], v[1]) for k, v in c.items()), key=lambda t: (-t[1], t[0]))


def factory_rows(data: dict, today: date, n: int = 7) -> list[tuple[str, dict]]:
    """The last n finished days of the agent factory's own numbers (factory.json, published beside metrics.json by an
    orchestrator such as churn): worker-hours by kind, slot use, fix loops, pauses, idle ticks."""
    days = (data.get("factory") or {}).get("days") or {}
    return [(d, days[d]) for d in sorted(days)
            if d < today.isoformat() and sum((days[d].get("worker_hours") or {}).values())][-n:]  # no runs: no data yet


def factory_stats(rows: list[tuple[str, dict]]) -> dict:
    """Averages per day over the rows (worker-hour shares weighted by hours)."""
    if not rows:
        return {}
    wh = Counter()
    for _, r in rows:
        wh.update(r.get("worker_hours") or {})
    worked = sum(wh.values())
    loops, merged = sum(r.get("loop_runs") or 0 for _, r in rows), sum(r.get("churn_merged") or 0 for _, r in rows)
    avg = lambda k: sum(r.get(k) or 0 for _, r in rows) / len(rows)
    slots = [r["slot_use"] for _, r in rows if r.get("slot_use") is not None]
    return {
        "days": len(rows), "new_work_share": wh["new_work"] / worked if worked else None,
        "fix_h": (wh["pr_fix"]) / len(rows), "sendback_h": (wh["send_back"] + wh["precheck_retry"]) / len(rows),
        "slot_use": sum(slots) / len(slots) if slots else None, "max_workers": rows[-1][1].get("max_workers"),
        "loops_per_pr": loops / merged if merged else None, "paused_h": avg("paused_hours"), "ci_busy_h": avg("ci_busy_hours"),
        "idle_h": avg("idle_ticks") * 10 / 60,  # one gate tick every ~10 min
        "precheck_sent_back": sum((r.get("precheck") or {}).get("sent_back", 0) for _, r in rows),
        "precheck_failed_again": sum((r.get("precheck") or {}).get("failed_again", 0) for _, r in rows),
    }


def runner_split(rows: list[dict], key: str) -> list[tuple[str, float]]:
    """Share of sampled runner minutes by workflow or by origin, largest first."""
    c: Counter[str] = Counter()
    for r in rows:
        for k, v in r.get("detail", {}).get(key, {}).items():
            c[k] += v
    tot = sum(c.values())
    return [(k, v / tot) for k, v in c.most_common()] if tot else []


# ---------------------------------------------------------------- Markdown summary and alerts

def _fmt(v, kind: str) -> str:
    if v is None:
        return "–"
    return {"pct": f"{100 * v:.1f}%", "min": f"{v:.1f} min", "h": f"{v:.1f} h", "n": f"{v:,.1f}"}[kind]


def _md(s: str) -> str:
    return s.replace("|", "\\|")


def summary_md(data: dict, cfg: Config, days: int, today: date, alerts: list[str]) -> str:
    days = shown_days(data, cfg, days, today)
    done = finished(window_rows(data, days, today), today)
    cmp = compare(data, cfg, days, today)
    cur, prev = cmp["cur"], cmp["prev"]
    span = lambda ds: f"{ds[0]} to {ds[-1]}, {len(ds)} days" if ds else "no days"  # noqa: E731
    period = (f"{cfg.bot_name} live since {cmp['start']}: {cmp['cur_label']} ({span(cmp['cur_days'])}) vs "
              f"{cmp['prev_label']} ({span(cmp['prev_days'])})." if cmp["start"] else "Last 7 finished days vs the 7 before.")
    out = [f"## {cfg.heading}", "",
           f"Updated {data['updated'].replace('T', ' ').replace('Z', ' UTC')}. {period} "
           f"Days are UTC; {sampling_note(cfg)}.", ""]
    if alerts:
        out += ["> [!WARNING]", *[f"> {a}" for a in alerts], ""]
    out += [f"| Measure | {cmp['cur_label'].capitalize()} | {cmp['prev_label'].capitalize()} |", "|---|---|---|"]
    lines = [("Delivery · merged PRs / day", "merged_per_day", "n"),
             *([(f"Delivery · {cfg.bot_name}-made share", "bot_merge_share", "pct")] if cmp["start"] else []),
             ("Delivery · agent-classified share", "agent_share", "pct"),
             ("Proof · CI workflow-run pass rate", "pass_rate", "pct"),
             ("Proof · failed runs", "fail_rate", "pct"),
             ("Efficiency · run minutes saved by early cancellation (est.)", "saved_share", "pct"),
             ("Reruns / day", "reruns_per_day", "n"),
             ("Reruns whose latest attempt passed", "rerun_pass_rate", "pct"),
             ("Capacity · queue wait median (sampled)", "wait_median", "min"),
             ("Capacity · queue wait p90 (sampled)", "wait_p90", "min"),
             ("Capacity · runner hours per merged PR (est.)", "runner_h_per_pr", "h")]
    if cfg.bot_workflows:
        lines.append(("Context · configured bot workflow share", "bot_share", "pct"))
    for label, k, kind in lines:
        out.append(f"| {label} | {_fmt(cur.get(k), kind)} | {_fmt(prev.get(k), kind)} |")
    out.append(f"| Context · CI changes merged | {len(cur.get('ci_changes', []))} | {len(prev.get('ci_changes', []))} |")
    if cfg.bot_workflows:
        out += ["", f"Configured bot workflow share of runs (last 7 days): {_fmt(cur.get('bot_share'), 'pct')}."]
    wf = runner_split(done[-7:], "sampled_min_by_workflow")
    if wf:
        out += ["", "### Runner time by workflow (sampled, last 7 days)", "", "| Workflow | Share |", "|---|---|"]
        out += [f"| {_md(k)} | {100 * v:.1f}% |" for k, v in wf[:12]]
    rr = rerun_split(done[-7:])
    if rr:
        out += ["", "### Reruns by workflow (last 7 days)", "",
                "A rerun that failed again usually retried a real failure: runner time spent learning nothing.", "",
                "| Workflow | Reruns | Passed | Failed again |", "|---|---|---|---|"]
        out += [f"| {_md(k)} | {n} | {p} | {f} |" for k, n, p, f in rr[:12]]
    fr = factory_rows(data, today)
    if fr:
        out += ["", "### Agent factory (from the orchestrator's own logs)", "",
                "Where the agents' time went. New work makes PRs; PR fixes and send-backs are loops. Idle = a free worker "
                "slot with nothing launchable.", "",
                "| Day | Worker-h | New work | Fix loops / merged PR | Slots used | Paused h | CI-busy h | Idle h |",
                "|---|---|---|---|---|---|---|---|"]
        for d, r in fr:
            w = r.get("worker_hours") or {}
            out.append(f"| {d} | {sum(w.values()):.1f} | {_fmt(r.get('new_work_share'), 'pct')} | "
                       f"{_fmt(r.get('loops_per_merged_pr'), 'n')} | {_fmt(r.get('slot_use'), 'pct')} | "
                       f"{r.get('paused_hours', 0):.1f} | {r.get('ci_busy_hours', 0):.1f} | {(r.get('idle_ticks') or 0) / 6:.1f} |")
    org = runner_split(done[-7:], "sampled_min_by_origin")
    if org:
        names = {"bot": f"{cfg.bot_name} PRs", "agent": "Agent PRs", "other": "Other PRs", "automation": "Automation PRs",
                 "main": "Default branch & schedules"}
        out += ["", "### Runner time by origin (sampled, last 7 days)", "", "| Origin | Share |", "|---|---|"]
        out += [f"| {names.get(k, k)} | {100 * v:.1f}% |" for k, v in org]
    if cur.get("ci_changes"):
        out += ["", "### CI changes merged (last 7 days)", ""]
        out += [f"- #{c['pr']}: {c['title']}" for c in cur["ci_changes"][-25:]]
        if len(cur["ci_changes"]) > 25:
            out.append(f"- …and {len(cur['ci_changes']) - 25} earlier in the week")
    out += ["", "<sub>Exact: merged PRs, run counts and outcomes, bot runs, CI changes, reruns. Sampled: queue wait and runner "
            "time. Agent PRs (bot PRs included): an agent branch prefix or an agent marker in the PR body.</sub>", ""]
    return "\n".join(out)


def sampling_note(cfg: Config) -> str:
    return (f"queue wait is sampled from {cfg.sample_fraction:.0%} of each day's runs (at least {cfg.sample_min}); runner "
            "time is runner occupancy where its readings cover the day, else that sample's estimate (± 95%)")


def check_alerts(data: dict, days: int, today: date, max_wait_p90: float | None, max_h_per_pr: float | None,
                 pool: int | None = None) -> list[str]:
    dated = [(d, r) for d, r in window_rows(data, days, today) if d != today.isoformat()][-7:]
    cur = week_stats([r for _, r in dated])
    out = []
    for day, r in dated:  # an estimate the runners could not have produced is an estimator fault, not a busy day
        det = r.get("detail", {})
        if over_ceiling(det, pool):
            out.append(f"{day}: runner time {runner_minutes(det) / 60:.0f} h is over the pool's {pool * 24} h "
                       f"({runner_source(det)}): treat it as an estimate fault.")
    if max_wait_p90 is not None and cur.get("wait_p90") is not None and cur["wait_p90"] > max_wait_p90:
        out.append(f"Job queue wait p90 averaged {cur['wait_p90']:.1f} min over the last 7 days (limit {max_wait_p90:g}).")
    if max_h_per_pr is not None and cur.get("runner_h_per_pr") is not None and cur["runner_h_per_pr"] > max_h_per_pr:
        out.append(f"Runner time per merged PR was {cur['runner_h_per_pr']:.1f} h over the last 7 days (limit {max_h_per_pr:g}).")
    return out


# ---------------------------------------------------------------- SVG rendering

STYLE = """
  .root { --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781; --grid:#e1e0d9;
          --axis:#c3c2b7; --border:rgba(11,11,11,0.10); --tile:#f4f3f0; --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a;
          --neutral:#c3c2b7; --failed:#d03b3b; --good:#0ca30c; }
  @media (prefers-color-scheme: dark) {
    .root { --surface:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --muted:#898781; --grid:#2c2c2a;
            --axis:#383835; --border:rgba(255,255,255,0.10); --tile:#232322; --s1:#3987e5; --s2:#d95926; --s3:#199e70;
            --neutral:#5c5c58; }
  }
  text { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; fill: var(--ink); }
  .bg { fill: var(--surface); stroke: var(--border); }
  .tile { fill: var(--tile); }
  .h1 { font-size: 16px; font-weight: 650; }
  .lead { font-size: 12.5px; fill: var(--ink2); }
  .h2 { font-size: 12.5px; font-weight: 600; }
  .meta, .tick, .lbl { font-size: 10.5px; fill: var(--muted); }
  .tick { font-variant-numeric: tabular-nums; }
  .k { font-size: 10.5px; font-weight: 600; letter-spacing: 0.04em; fill: var(--ink2); }
  .v { font-size: 22px; font-weight: 650; font-variant-numeric: tabular-nums; }
  .sub { font-size: 10.5px; fill: var(--ink2); }
  .grid { stroke: var(--grid); stroke-width: 1; }
  .base { stroke: var(--axis); stroke-width: 1; }
  .start { stroke: var(--s1); stroke-width: 1.5; }
  .startl { font-size: 11px; font-weight: 650; fill: var(--ink); }
  .mark { stroke: var(--ink2); stroke-width: 1.5; } .markd { fill: var(--muted); }
  .evl { font-size: 10.5px; fill: var(--ink2); }
  .seg { stroke: var(--surface); stroke-width: 1; }
  .hours { fill: var(--ink2); opacity: 0.55; }
  .bot { fill: var(--s1); } .agent { fill: var(--s2); } .other { fill: var(--s3); } .automation, .unknown { fill: var(--neutral); }
  .failed { fill: var(--failed); } .saved { fill: var(--good); }
  .line.saved { fill: none; stroke: var(--good); } .dot.saved { fill: var(--good); }
  .line.failedl { fill: none; stroke: var(--failed); } .dot.failedl { fill: var(--failed); }
  .partial { opacity: 0.5; }
  .line.cum { stroke: var(--s1); stroke-width: 2.5; } .dot.cum { fill: var(--s1); } .area { fill: var(--s1); opacity: 0.12; }
  .endl { font-size: 13px; font-weight: 650; fill: var(--s1); font-variant-numeric: tabular-nums; }
  .avg { stroke: var(--ink2); stroke-width: 1.5; stroke-dasharray: 5 4; } .avgl { font-size: 10.5px; font-weight: 600; fill: var(--ink2);
    paint-order: stroke; stroke: var(--surface); stroke-width: 4px; stroke-linejoin: round; }
  .clip { font-size: 9.5px; fill: var(--ink2); } .vl { font-size: 9.5px; fill: var(--ink2); font-variant-numeric: tabular-nums; }
  .line { fill: none; stroke: var(--ink2); stroke-width: 2; } .dot { fill: var(--ink2); stroke: var(--surface); stroke-width: 2; }
  .people { fill: #8b5cf6; } .agents { fill: var(--s1); } .bots { fill: var(--neutral); } .closed { fill: var(--good); }
  .dayband { fill: var(--tile); } .band { fill: var(--s1); opacity: 0.13; }
  .daily { fill: var(--surface); stroke: var(--ink2); stroke-width: 1.5; }
  .noissue { font-size: 10.5px; font-weight: 650; fill: var(--failed); }
"""


def _nice_axis(v: float) -> tuple[float, float]:
    """(top, step) of the tightest axis with a round step (1, 2, 2.5 or 5 x 10^k) and at most 5 intervals: 26 -> (30, 10)."""
    v = max(v, 1e-9)
    for k in range(-2, 7):
        for m in (1, 2, 2.5, 5):
            step = m * 10 ** k
            if math.ceil(v / step - 1e-9) <= 5:
                return math.ceil(v / step - 1e-9) * step, step
    return v, v / 4


def _bar_path(x: float, y: float, w: float, h: float, r: float) -> str:
    """Bar with rounded top corners only (the data end), square at the baseline."""
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z")


def _short(name: str, n: int = 22) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


def _fmt_day(d: str) -> str:
    return date.fromisoformat(d).strftime("%b %-d")


def _delta(cur: float | None, prev: float | None) -> str:
    if cur is None or prev is None or not prev:
        return ""
    return f" · {100 * (cur - prev) / prev:+.0f}%"


def _origin_parts(row: dict) -> dict[str, float]:
    counts = row.get("merged_by_class")
    if counts is None:
        return {"bot": 0, "agent": 0, "other": 0, "automation": 0, "unknown": row["merged"]}
    parts = {kind: counts.get(kind, 0) for kind in CLASSES}
    parts["unknown"] = max(0, row["merged"] - sum(parts.values()))
    return parts


def render(data: dict, days: int, today: date, cfg: Config | None = None) -> str:
    """The dashboard: one UTC day per mark. When a bot is configured (or found), a line marks the day it started
    through every panel. The cards show the last finished day against the day before it."""
    cfg = cfg or Config(repo=data["repo"])
    rows = window_rows(data, shown_days(data, cfg, days, today), today)
    if not rows:
        raise SystemExit("no data to render")
    today_s = today.isoformat()
    width, pad = 920, 24
    left, right = pad + 42, width - pad
    slot = (right - left) / len(rows)
    bar_w = max(2.0, slot - 2)
    x_left = lambda i: left + i * slot
    x_mid = lambda i: left + i * slot + slot / 2
    index = {day: i for i, (day, _) in enumerate(rows)}
    start = bot_start(data, cfg)
    start = start if start and start <= today_s else None
    last, before = last_two_days(data, today)
    cur = week_stats([data["days"][last]]) if last else {}
    prev = week_stats([data["days"][before]]) if before else {}
    start_i = next((i for i, (day, _) in enumerate(rows) if day >= start), None) if start else None
    out: list[str] = []

    # Heading and the one-sentence story
    out.append(f'<text class="h1" x="{pad}" y="34">{escape(cfg.heading)}</text>')
    out.append(f'<text class="meta" x="{width - pad}" y="34" text-anchor="end">last {len(rows)} days · '
               f'updated {escape(data["updated"].replace("T", " ").replace("Z", " UTC"))}</text>')
    today_row = data["days"].get(today_s)
    today_note = f' Today so far: {today_row["merged"]} merged.' if today_row else ""
    if start:
        since = [r for d, r in data["days"].items() if d >= start]
        merged_since = sum(r["merged"] for r in since)
        bot_since = sum((r.get("merged_by_class") or {}).get("bot", 0) for r in since)
        share = f" ({100 * bot_since / merged_since:.0f}%)" if merged_since else ""
        lead = (f"{cfg.bot_name} has run since {_fmt_day(start)}: {bot_since:,} of the {merged_since:,} PRs merged since "
                f"then were its own{share}.{today_note}")
    else:
        lead = f"Each mark is one UTC day; the cards show the last finished day against the day before.{today_note}"
    out.append(f'<text class="lead" x="{pad}" y="54">{escape(lead)}</text>')

    # Cards: the last finished day vs the day before it
    factory = factory_stats(factory_rows(data, today))
    factory_detail = []
    if factory:
        factory_detail = [
            f"Agent time on new work ({factory['days']}d): {_fmt(factory.get('new_work_share'), 'pct')}",
            f"Fixes {factory['fix_h']:.1f}h/day; send-backs {factory['sendback_h']:.1f}h/day",
            f"Worker slots used: {_fmt(factory.get('slot_use'), 'pct')} of {factory.get('max_workers') or '?'}",
            f"Fix loops per merged PR: {_fmt(factory.get('loops_per_pr'), 'n')}",
            f"Idle with a free slot: {factory['idle_h']:.1f}h/day",
        ]
    was = f"{_fmt_day(before)}:" if before else "day before:"
    if start:
        share_card = (f"{cfg.bot_name.upper()}-MADE SHARE", _fmt(cur.get("bot_merge_share"), "pct"),
                      f'{cur.get("bot_merged", 0):,} PRs · all agents {_fmt(cur.get("agent_share"), "pct")}')
    else:
        share_card = ("AGENT-MADE SHARE", _fmt(cur.get("agent_share"), "pct"),
                      f'{was} {_fmt(prev.get("agent_share"), "pct")}')
    count = lambda st: f"{st['merged_per_day']:,.0f}" if st else "–"
    cards = [
        ("MERGED PRs", count(cur),
         f'{was} {count(prev)}{_delta(cur.get("merged_per_day"), prev.get("merged_per_day"))}', []),
        (*share_card, factory_detail),
        ("RUNNER HOURS / MERGED PR", _fmt(cur.get("runner_h_per_pr"), "h"),
         f'{was} {_fmt(prev.get("runner_h_per_pr"), "h")}{_delta(cur.get("runner_h_per_pr"), prev.get("runner_h_per_pr"))}', []),
        ("QUEUE WAIT P90", _fmt(cur.get("wait_p90"), "min"),
         f'{was} {_fmt(prev.get("wait_p90"), "min")}{_delta(cur.get("wait_p90"), prev.get("wait_p90"))}', []),
    ]
    card_y, card_h, card_gap = 68, 74, 12
    card_w = (width - 2 * pad - 3 * card_gap) / 4
    for i, (label, value, sub, detail) in enumerate(cards):
        x = pad + i * (card_w + card_gap)
        out.append("<g>" + (f"<title>{escape('. '.join(detail))}</title>" if detail else ""))
        if i == 1:
            out.append("<desc>Merged PRs per day, by origin. Agent share of merged PRs</desc>")
        out.append(f'<rect class="tile" x="{x:.1f}" y="{card_y}" width="{card_w:.1f}" height="{card_h}" rx="8"/>')
        out.append(f'<text class="k" x="{x + 14:.1f}" y="{card_y + 20}">{escape(label)}</text>')
        out.append(f'<text class="v" x="{x + 14:.1f}" y="{card_y + 46}">{escape(value)}</text>')
        out.append(f'<text class="sub" x="{x + 14:.1f}" y="{card_y + 63}">{escape(sub)}</text>')
        out.append("</g>")
    out.append(f'<text class="meta" x="{pad}" y="{card_y + card_h + 18}">Cards: '
               + escape(f"{_fmt_day(last)}, the last finished UTC day" + (f", vs {_fmt_day(before)}" if before else "")
                        if last else "no finished day yet") + ".</text>")

    # Shared furniture for every panel
    events = [(d, label) for d, label in cfg.events if d in index]

    def frame(top: int, chart_h: int, axis: tuple[float, float], title: str, formatter, title_detail: str = "") -> None:
        maximum, step = axis
        intervals = round(maximum / step)
        tip = f"<title>{escape(title_detail)}</title>" if title_detail else ""
        out.append(f'<g>{tip}<text class="h2" x="{pad}" y="{top - 30}">{escape(title)}</text></g>')
        for tick in range(intervals + 1):
            value = step * tick
            y = top + chart_h - chart_h * tick / intervals
            cls = "base" if tick == 0 else "grid"
            out.append(f'<line class="{cls}" x1="{left}" x2="{right}" y1="{y:.1f}" y2="{y:.1f}"/>')
            out.append(f'<text class="tick" x="{left - 8}" y="{y + 3.5:.1f}" text-anchor="end">{formatter(value)}</text>')
        for idx, (day, _) in enumerate(rows):
            if (len(rows) - 1 - idx) % 7 == 0:
                out.append(f'<text class="tick" x="{x_mid(idx):.1f}" y="{top + chart_h + 16}" '
                           f'text-anchor="middle">{_fmt_day(day)}</text>')
        for day, label in events:
            x = x_left(index[day])
            out.append(f'<g><title>{_fmt_day(day)}: {escape(label)}</title><line class="mark" x1="{x:.1f}" x2="{x:.1f}" '
                       f'y1="{top - 4}" y2="{top + chart_h}"/></g>')
        if start_i is not None:
            x = x_left(start_i)
            out.append(f'<line class="start" x1="{x:.1f}" x2="{x:.1f}" y1="{top - 4}" y2="{top + chart_h}"/>')

    def annotate(top: int) -> None:
        """Labels for the bot's start and the events, in the lane above the first chart (two rows, no overlaps)."""
        labels = [(x_left(index[d]), f"{_fmt_day(d)} · {label}", d < (start or "9999")) for d, label in events]
        if start_i is not None:
            labels.append((x_left(start_i), f"{cfg.bot_name} live · {_fmt_day(start)}", False))
        taken: list[list[tuple[float, float]]] = [[], []]
        for x, text, before in sorted(labels, key=lambda item: item[2]):
            w = 6.2 * len(text)
            x0, x1 = (x - 5 - w, x - 5) if before else (x + 5, x + 5 + w)
            if x1 > right:
                x0, x1 = x - 5 - w, x - 5
                before = True
            row = next((r for r in (0, 1) if all(x1 < a or x0 > b for a, b in taken[r])), 1)
            taken[row].append((x0, x1))
            cls = "startl" if text.startswith(f"{cfg.bot_name} live") else "evl"
            anchor = "end" if before else "start"
            out.append(f'<text class="{cls}" x="{(x - 5 if before else x + 5):.1f}" y="{top - 8 - 13 * row}" '
                       f'text-anchor="{anchor}">{escape(text)}</text>')

    def line_series(top: int, chart_h: int, values: list[float | None], maximum: float, formatter=None, detail: str = "",
                    tips: list[str] | None = None, cls: str = "") -> None:
        y_at = lambda value: top + chart_h - chart_h * value / maximum
        segment: list[str] = []
        dots = []
        for i, ((day, _), value) in enumerate(zip(rows, values)):
            if value is None:
                if len(segment) > 1:
                    out.append(f'<polyline class="line {cls}" points="{" ".join(segment)}"/>')
                segment = []
                continue
            segment.append(f"{x_mid(i):.1f},{y_at(value):.1f}")
            partial = day == today_s
            text = tips[i] if tips else f"{formatter(value)} {detail}"
            dots.append(f'<circle class="dot {cls}{" partial" if partial else ""}" cx="{x_mid(i):.1f}" cy="{y_at(value):.1f}" '
                        f'r="4"><title>{_fmt_day(day)}: {escape(text)}'
                        f'{" (today so far)" if partial else ""}</title></circle>')
        if len(segment) > 1:
            out.append(f'<polyline class="line {cls}" points="{" ".join(segment)}"/>')
        out.extend(dots)

    def legend(y: int, items: list[tuple[str, str]]) -> None:
        x = right
        for label, css in reversed(items):
            x -= 6.2 * len(label) + 22
            out.append(f'<rect class="{css}" x="{x:.1f}" y="{y - 8}" width="9" height="9" rx="2"/>')
            out.append(f'<text class="lbl" x="{x + 13:.1f}" y="{y}">{escape(label)}</text>')

    # 1 · Delivery: merged PRs each day, by origin
    y = card_y + card_h + 18 + 62
    top, chart_h = y, 195
    delivery = [_origin_parts(row) for _, row in rows]
    delivery_axis = _nice_axis(max(1.0, max(sum(parts.values()) for parts in delivery)))
    delivery_max = delivery_axis[0]
    frame(top, chart_h, delivery_axis, "1 · Delivery — merged PRs per day, by origin", lambda v: f"{v:g}")
    origins = [("bot", cfg.bot_name), ("agent", "other agents"), ("other", "other"), ("automation", "automation"),
               ("unknown", "unclassified")]
    if not start and not any(parts["bot"] for parts in delivery):
        origins = origins[1:]
    for i, ((day, _), parts) in enumerate(zip(rows, delivery)):
        x, base = x_left(i) + (slot - bar_w) / 2, top + chart_h
        shown = [(kind, parts[kind]) for kind, _ in origins if parts[kind] > 0]
        elements = []
        for n, (kind, value) in enumerate(shown):
            h = chart_h * value / delivery_max
            if n == len(shown) - 1:
                elements.append(f'<path class="{kind} seg" d="{_bar_path(x, base - h, bar_w, h, 4)}"/>')
            else:
                elements.append(f'<rect class="{kind} seg" x="{x:.1f}" y="{base - h:.1f}" width="{bar_w:.1f}" height="{h:.1f}"/>')
            base -= h
        total = sum(parts.values())
        tip = escape(f'{_fmt_day(day)} · {total:.0f} merged PRs{" (today so far)" if day == today_s else ""} · '
                     + ", ".join(f"{label} {parts[kind]:.0f}" for kind, label in origins))
        fade = ' class="partial"' if day == today_s else ""
        out.append(f'<g{fade}><title>{tip}</title>{"".join(elements)}</g>')
    annotate(top)
    legend(top - 30, [(label, kind) for kind, label in origins if kind != "unknown" or any(p["unknown"] for p in delivery)])
    y = top + chart_h + 70
    panel = 2

    def running_total(top: int, per_day: dict[str, float], title: str, axis_fmt, end_fmt, tip_fmt, note: str = "") -> int:
        """A running total of per_day from the bot's start, as a filled line ending in its total; days past the last one
        with data are left off. Returns the y of the next panel."""
        last = max(per_day, default="")
        values, tips, total = [], [], 0.0
        for day, _ in rows:
            if day < start or day > last:
                values.append(None)
                tips.append("")
                continue
            total += per_day.get(day, 0)
            values.append(total)
            tips.append(tip_fmt(day, per_day.get(day, 0), total))
        chart_h = 150
        axis = _nice_axis(max(1.0, total))
        frame(top, chart_h, axis, title, axis_fmt, note)
        pts = [(x_mid(i), top + chart_h - chart_h * v / axis[0]) for i, v in enumerate(values) if v is not None]
        if len(pts) > 1:
            edge = " ".join(f"L{x:.1f},{py:.1f}" for x, py in pts)
            out.append(f'<path class="area" d="M{pts[0][0]:.1f},{top + chart_h} {edge} L{pts[-1][0]:.1f},{top + chart_h} Z"/>')
        line_series(top, chart_h, values, axis[0], tips=tips, cls="cum")
        if pts:
            out.append(f'<text class="endl" x="{pts[-1][0] - 8:.1f}" y="{pts[-1][1] - 9:.1f}" text-anchor="end">'
                       f'{escape(end_fmt(total))}</text>')
        if note:
            out.append(f'<text class="avgl" x="{right}" y="{top - 30}" text-anchor="end">{escape(note)}</text>')
        return top + chart_h + 70

    # 2 · CI hours: runner hours each day (est. from the sampled runs); the tip splits them by origin
    details = [row.get("detail", {}) for _, row in rows]
    hours = [runner_minutes(detail) / 60 if runner_minutes(detail) is not None else None for detail in details]
    known = sorted(v for v in hours if v)
    if known:
        top, chart_h = y, 150
        # a few days' estimates overshoot (a small sample of long runs x thousands of runs): fit the axis to 3x the
        # median day and mark the days above it, so they don't flatten the rest
        hours_axis = _nice_axis(min(known[-1], 3 * known[len(known) // 2]))
        hours_max = hours_axis[0]
        frame(top, chart_h, hours_axis, f"{panel} · CI hours — runner hours per day (est., sampled)", lambda v: f"{v:g}")
        panel += 1
        for i, ((day, row), v) in enumerate(zip(rows, hours)):
            if v is None:
                continue
            h = chart_h * min(v, hours_max) / hours_max
            split = runner_split([row], "sampled_min_by_origin")
            tip = (f"{_fmt_day(day)} · {v:,.0f} runner hours (est.){' (today so far)' if day == today_s else ''} · "
                   f"{row['runs']['total']:,} runs" + (" · " + ", ".join(f"{k} {100 * f:.0f}%" for k, f in split) if split else ""))
            fade = ' class="partial"' if day == today_s else ""
            out.append(f'<g{fade}><title>{escape(tip)}</title><path class="hours" '
                       f'd="{_bar_path(x_left(i) + (slot - bar_w) / 2, top + chart_h - h, bar_w, h, 4)}"/></g>')
            if v > hours_max:
                out.append(f'<text class="clip" x="{x_mid(i):.1f}" y="{top - 8}" text-anchor="middle">▲ {v:,.0f} h</text>')
        y = top + chart_h + 70

    # Queue wait and CI cost per merged PR (sampled)
    wait_values = [detail.get("wait_p90") for detail in details]
    runner_values = [runner_minutes(detail) / row["merged"] / 60 if runner_minutes(detail) is not None
                     and row["merged"] else None for (_, row), detail in zip(rows, details)]
    if any(v is not None for v in wait_values + runner_values):
        top, chart_h = y, 120
        wait_axis = _nice_axis(max((v for v in wait_values if v is not None), default=1.0))
        wait_max = wait_axis[0]
        frame(top, chart_h, wait_axis, f"{panel} · Speed — job queue wait (p90) per day, sampled (minutes)", lambda v: f"{v:g}")
        panel += 1
        for i, (day, row) in enumerate(rows):
            changes = row.get("detail", {}).get("ci_changes")
            if changes:
                prs = ", ".join(f'#{change["pr"]}' for change in changes[:6])
                out.append(f'<g><title>{_fmt_day(day)}: CI change(s) merged {escape(prs)}</title>'
                           f'<path class="markd" d="M{x_mid(i):.1f},{top - 9} l3,3 l-3,3 l-3,-3 Z"/></g>')
        line_series(top, chart_h, wait_values, wait_max, lambda v: f"{v:.1f}",
                    f"minutes · {cfg.sample_fraction:.0%} of each day's runs sampled")
        legend(top - 30, [("CI change merged", "markd")])
        y = top + chart_h + 70
        top = y
        runner_axis = (_nice_axis(cfg.cost_max) if cfg.cost_max
                       else _nice_axis(max((v for v in runner_values if v is not None), default=1.0)))
        runner_max = runner_axis[0]
        frame(top, chart_h, runner_axis, f"{panel} · CI load — runner hours per merged PR (est., sampled)", lambda v: f"{v:g}")
        panel += 1
        cost_tips = [f"{v:.2f} runner hours / merged PR" + (f" (above the {runner_max:g} h axis)" if v > runner_max else "")
                     if v is not None else "" for v in runner_values]
        line_series(top, chart_h, [None if v is None else min(v, runner_max) for v in runner_values], runner_max,
                    tips=cost_tips)
        for i, v in enumerate(runner_values):
            if v is not None and v > runner_max:
                out.append(f'<text class="clip" x="{x_mid(i):.1f}" y="{top - 8}" text-anchor="middle">▲ {v:.0f} h</text>')
        y = top + chart_h + 70
    else:
        out.append(f'<text class="h2" x="{pad}" y="{y - 30}">{panel} · Speed and cost</text>')
        panel += 1
        out.append(f'<text class="meta" x="{pad}" y="{y - 12}">No sampled CI capacity history yet.</text>')
        y += 20

    # The bot's own spend in dollars (factory.json: what its agent sessions cost, all hosts) and per merged bot PR
    fdays = {d: r for d, r in ((data.get("factory") or {}).get("days") or {}).items()
             if start and d >= start and r.get("usd") is not None}
    if start_i is not None and fdays:
        usd = {d: float(r["usd"]) for d, r in fdays.items()}
        prs = {d: r.get("churn_merged") or 0 for d, r in fdays.items()}
        spent, made = sum(usd.values()), sum(prs.values())
        y = running_total(
            y, usd, f"{panel} · Spend — {cfg.bot_name} cost in dollars, cumulative since {_fmt_day(start)}",
            lambda v: f"${v:,.0f}", lambda t: f"${t:,.0f} total",
            lambda d, n, t: f"${n:,.2f} this day ({prs[d]} merged PRs) · ${t:,.2f} since {_fmt_day(start)}"
                            + (" (today so far)" if d == today_s else ""),
            f"{made:,} merged {cfg.bot_name} PRs for ${spent:,.0f}")
        panel += 1
        per_pr = [usd[d] / prs[d] if prs.get(d) else None for d, _ in rows]
        top, chart_h = y, 120
        pr_axis = _nice_axis(max((v for v in per_pr if v is not None), default=1.0))
        frame(top, chart_h, pr_axis, f"{panel} · Cost per PR — {cfg.bot_name} dollars per merged PR, each day",
              lambda v: f"${v:g}")
        panel += 1
        line_series(top, chart_h, per_pr, pr_axis[0],
                    tips=[f"${v:.2f} per merged PR (${usd[d]:,.2f} for {prs[d]})" if v is not None else ""
                          for (d, _), v in zip(rows, per_pr)], cls="cum")
        if made:
            avg = spent / made
            spots = [index[d] for d in sorted(fdays) if d in index]
            ya = top + chart_h - chart_h * avg / pr_axis[0]
            out.append(f'<line class="avg" x1="{x_left(spots[0]):.1f}" x2="{x_left(spots[-1]) + slot:.1f}" '
                       f'y1="{ya:.1f}" y2="{ya:.1f}"/>')
            out.append(f'<text class="avgl" x="{x_left(spots[-1]) + slot:.1f}" y="{ya - 6:.1f}" text-anchor="end">'
                       f'{escape(f"avg ${avg:.2f} per PR since {_fmt_day(start)}")}</text>')
        y = top + chart_h + 70

    # 4 · Efficiency: share of run minutes early cancellation saved (est.) and share of runs that failed
    proof_detail = ["CI workflow-run outcomes; not a per-PR quality score"]
    reruns = rerun_split(finished(rows, today)[-7:])
    if reruns:
        total, passed, failed = sum(item[1] for item in reruns), sum(item[2] for item in reruns), sum(item[3] for item in reruns)
        proof_detail.append(f"Reruns that passed: {_fmt(passed / (passed + failed) if passed + failed else None, 'pct')} "
                            f"({passed} passed, {failed} failed again; {total} total)")
        proof_detail.extend(f"{name}: {count} reruns, {ok} passed, {again} failed again" for name, count, ok, again in reruns[:12])
    saved_values, saved_tips, failed_values, failed_tips = [], [], [], []
    for _, row in rows:
        c = row.get("detail", {}).get("cancel_savings")
        share = saved_share(c) if c else None
        saved_values.append(None if share is None else 100 * share)
        if share is None:
            saved_tips.append("")
        else:
            unpriced = f', {c["unpriced"]} not priced' if c["unpriced"] else ""
            saved_tips.append(f'{share * 100:.1f}% of run minutes saved by early cancellation (est. {c["saved_min_est"]:,} '
                              f'of {c["wall_min"] + c["saved_min_est"]:,} min; {c["cancelled"]} runs cancelled{unpriced})')
        r = row["runs"]
        judged = r["success"] + r["failure"]
        failed_values.append(100 * r["failure"] / judged if judged else None)
        failed_tips.append(f'{100 * r["failure"] / judged:.1f}% of runs failed ({r["failure"]:,} of {judged:,} CI runs that '
                           f'passed or failed)' if judged else "")
    top, chart_h = y, 120
    pct_axis = _nice_axis(max([1.0] + [v for v in saved_values + failed_values if v is not None]))
    frame(top, chart_h, pct_axis, f"{panel} · Efficiency — % of run minutes saved by early cancellation (est.) and % of runs failed",
          lambda v: f"{v:g}%", ". ".join(proof_detail))
    line_series(top, chart_h, saved_values, pct_axis[0], tips=saved_tips, cls="saved")
    line_series(top, chart_h, failed_values, pct_axis[0], tips=failed_tips, cls="failedl")
    legend(top - 30, [("minutes saved (est.)", "saved"), ("runs failed", "failed")])
    y = top + chart_h + 44

    # Issues: opened (arrows up, stacked by who opened them) and closed as completed (arrows down), one pair a day
    panel += 1  # after Efficiency
    y += 26  # Efficiency left room for the footer only
    sc_days = (data.get("scorecard") or {}).get("days") or {}
    fac_days = (data.get("factory") or {}).get("days") or {}
    if any(d in sc_days for d, _ in rows):
        top, half_h = y, 85
        up_max = max([1] + [sc_days[d].get("opened", 0) for d, _ in rows if d in sc_days])
        down_max = max([1] + [sc_days[d].get("resolved", 0) for d, _ in rows if d in sc_days])
        scale = half_h / max(up_max, down_max)  # one scale both ways: an arrow's length is its count
        base = top + half_h
        out.append(f'<text class="h2" x="{pad}" y="{top - 30}">{panel} · Issues — opened ▲ (by who) and closed ▼ each day</text>')
        legend(top - 30, [("opened by people", "people"), ("by agents", "agents"), ("by bots", "bots"),
                          ("closed as completed", "closed")])
        step = _nice_axis(max(up_max, down_max))[1]
        v = step
        while v <= max(up_max, down_max):  # grid both ways from the baseline: up = opened, down = closed
            for yy in (base - v * scale, base + v * scale):
                out.append(f'<line class="grid" x1="{left}" x2="{right}" y1="{yy:.1f}" y2="{yy:.1f}"/>'
                           f'<text class="tick" x="{left - 8}" y="{yy + 3.5:.1f}" text-anchor="end">{v:g}</text>')
            v += step
        aw = max(3.0, slot * 0.3)
        for i, (day, _) in enumerate(rows):
            if i % 2 == 0:  # alternate day bands: each day's pair of arrows reads as one
                out.append(f'<rect class="dayband" x="{x_left(i):.1f}" y="{top - 4}" width="{slot:.1f}" '
                           f'height="{2 * half_h + 8}"/>')
            r = sc_days.get(day)
            if not r:
                continue
            fade = ' class="partial"' if day == today_s else ""
            by = r.get("opened_by_origin") or {}
            people, agents, bots = by.get("owner", 0) + by.get("other", 0), by.get("agent", 0), by.get("automation", 0)
            xu, xd = x_left(i) + slot * 0.13, x_left(i) + slot * 0.57
            parts, yy = [], base
            stack = [(people, "people"), (agents, "agents"), (bots, "bots")]
            shown = [(v, c) for v, c in stack if v]
            for n, (v, c) in enumerate(shown):
                h = v * scale
                head = min(aw * 0.9, h) if n == len(shown) - 1 else 0  # the arrowhead tops the last segment
                parts.append(f'<rect class="{c}" x="{xu:.1f}" y="{yy - h + head:.1f}" width="{aw:.1f}" height="{h - head:.1f}"/>')
                if head:
                    parts.append(f'<path class="{c}" d="M{xu - aw * 0.35:.1f},{yy - h + head:.1f} L{xu + aw / 2:.1f},{yy - h:.1f} '
                                 f'L{xu + aw * 1.35:.1f},{yy - h + head:.1f} Z"/>')
                yy -= h
            closed = r.get("resolved", 0)
            if closed:
                h = closed * scale
                head = min(aw * 0.9, h)
                parts.append(f'<rect class="closed" x="{xd:.1f}" y="{base:.1f}" width="{aw:.1f}" height="{h - head:.1f}"/>'
                             f'<path class="closed" d="M{xd - aw * 0.35:.1f},{base + h - head:.1f} L{xd + aw / 2:.1f},'
                             f'{base + h:.1f} L{xd + aw * 1.35:.1f},{base + h - head:.1f} Z"/>')
            tip = (f"{_fmt_day(day)}: opened {people + agents + bots} (people {people}, agents {agents}, bots {bots}); "
                   f"closed {closed} as completed, {r.get('not_planned', 0)} as not planned"
                   + (" (today so far)" if day == today_s else ""))
            out.append(f'<g{fade}><title>{escape(tip)}</title>{"".join(parts)}</g>')
        for idx, (day, _) in enumerate(rows):
            if (len(rows) - 1 - idx) % 7 == 0:
                out.append(f'<text class="tick" x="{x_mid(idx):.1f}" y="{base + half_h + 18}" text-anchor="middle">'
                           f'{_fmt_day(day)}</text>')
        out.append(f'<line class="base" x1="{left}" x2="{right}" y1="{base:.1f}" y2="{base:.1f}"/>')
        y = base + half_h + 70
        panel += 1

    def daily_and_rolling(top: int, chart_h: int, daily: list[float | None], rolling: list[float | None], maximum: float,
                          daily_tips: list[str], rolling_tips: list[str], cls: str) -> None:
        """Each day's value as a hollow dot, and the 7-day rolling value as the line (days with few events are noisy)."""
        y_at = lambda v: top + chart_h - chart_h * min(v, maximum) / maximum  # noqa: E731
        for i, v in enumerate(daily):
            if v is not None:
                out.append(f'<circle class="daily" cx="{x_mid(i):.1f}" cy="{y_at(v):.1f}" r="2.6"><title>'
                           f'{_fmt_day(rows[i][0])}: {escape(daily_tips[i])}</title></circle>')
        line_series(top, chart_h, rolling, maximum, tips=rolling_tips, cls=cls)

    def rolling(num: list[float | None], den: list[float | None], n: int = 7) -> list[float | None]:
        out_v = []
        for i in range(len(num)):  # days count only where both sides are known
            pairs = [(a, b) for a, b in zip(num[max(0, i - n + 1): i + 1], den[max(0, i - n + 1): i + 1])
                     if a is not None and b is not None]
            b_sum = sum(b for _, b in pairs)
            out_v.append(sum(a for a, _ in pairs) / b_sum if b_sum and num[i] is not None else None)
        return out_v

    # First-pass rate: exact (every merged bot PR is known), with a 95% band for the day's sample size
    fp = [(fac_days.get(d) or {}).get("first_pass") for d, _ in rows]
    if any(x and x.get("merged") for x in fp):
        top, chart_h = y, 120
        vals, lo_b, hi_b, tips = [], [], [], []
        for x in fp:
            n, k = (x or {}).get("merged", 0), (x or {}).get("first_pass", 0)
            if not n:
                vals.append(None), lo_b.append(None), hi_b.append(None), tips.append("")
                continue
            z, ph = 1.96, k / n  # Wilson interval
            c = (ph + z * z / (2 * n)) / (1 + z * z / n)
            w = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / (1 + z * z / n)
            vals.append(100 * ph), lo_b.append(100 * max(0, c - w)), hi_b.append(100 * min(1, c + w))
            tips.append(f"{100 * ph:.0f}% first pass ({k} of {n} {cfg.bot_name} PRs merged with no send-back or fix; "
                        f"95% band {100 * max(0, c - w):.0f}-{100 * min(1, c + w):.0f}%)")
        frame(top, chart_h, (100.0, 25.0), f"{panel} · First-pass rate — {cfg.bot_name} PRs merged with no send-back "
              "or fix, each day (band: 95% for the day's count)", lambda v: f"{v:g}%")
        pts = [(i, a, b) for i, (a, b) in enumerate(zip(lo_b, hi_b)) if a is not None]
        if len(pts) > 1:
            y_at = lambda v: top + chart_h - chart_h * v / 100  # noqa: E731
            poly = [f"{x_mid(i):.1f},{y_at(b):.1f}" for i, _, b in pts] + [f"{x_mid(i):.1f},{y_at(a):.1f}" for i, a, _ in reversed(pts)]
            out.append(f'<polygon class="band" points="{" ".join(poly)}"/>')
        line_series(top, chart_h, vals, 100.0, tips=tips, cls="cum")
        y = top + chart_h + 70
        panel += 1

    # Change failure: merged PRs that were reverts or regression fixes, over merged PRs, each day (owner 2026-10-24: a
    # plain day-by-day line, no averaging, from the bot's start)
    if any(d in sc_days for d, _ in rows):
        live = lambda d: d in sc_days and (not start or d >= start)  # noqa: E731
        fails = [sc_days[d].get("reverts", 0) + sc_days[d].get("regression_fixes", 0) if live(d) else None for d, _ in rows]
        merged = [row["merged"] if live(d) else None for d, row in rows]
        daily = [100 * f / m if f is not None and m else None for f, m in zip(fails, merged)]
        top, chart_h = y, 110
        axis = _nice_axis(max([1.0] + [v for v in daily if v is not None]))
        frame(top, chart_h, axis, f"{panel} · Change failure — % of merged PRs that were reverts or regression fixes, "
              "each day", lambda v: f"{v:g}%")
        line_series(top, chart_h, daily, axis[0], cls="failedl",
                    tips=[f"{v:.2f}% ({f} of {m} merged PRs were reverts or regression fixes)" if v is not None else ""
                          for f, m, v in zip(fails, merged, daily)])
        y = top + chart_h + 70
        panel += 1

    # Cost per resolved issue: the bot's dollars over the issues it did the work on (merged bot PRs), each day as a dot
    # and the average of the days so far (up to 7) as the line, from the first day with a bot-resolved issue: day 1 is
    # one day's figure, day 2 the average of two, and so on (owner 2026-10-24: no zero-filled days, no false trend).
    usd = [(fac_days.get(d) or {}).get("usd") if not start or d >= start else None for d, _ in rows]
    by_bot = [sc_days[d].get("resolved_by_bot") if d in sc_days else None for d, _ in rows]
    first = next((i for i, (u, r) in enumerate(zip(usd, by_bot)) if u is not None and r), None)
    if first is not None:
        usd = [u if i >= first else None for i, u in enumerate(usd)]
        by_bot = [r if i >= first and usd[i] is not None else None for i, r in enumerate(by_bot)]
        daily = [u / r if u is not None and r else None for u, r in zip(usd, by_bot)]
        roll = rolling(usd, by_bot)
        top, chart_h = y, 110
        axis = _nice_axis(max([1.0] + [v for v in roll if v is not None]) * 1.4)
        frame(top, chart_h, axis, f"{panel} · Cost per resolved issue — {cfg.bot_name} dollars per issue it resolved "
              "(dots: each day; line: average of the days so far, up to 7)", lambda v: f"${v:g}")
        daily_and_rolling(top, chart_h, daily, roll, axis[0],
                          [f"${u:,.0f} / {r} issues resolved with {cfg.bot_name} work = ${v:.2f}" if v is not None else ""
                           for u, r, v in zip(usd, by_bot, daily)],
                          [f"${v:.2f} per issue, average of the last {min(7, i - first + 1)} day(s)" if v is not None else ""
                           for i, v in enumerate(roll)], "cum")
        y = top + chart_h + 70
        panel += 1

    # GitHub API: the shared token's busiest hour each day, against its limit (owner 2026-10-24: the quota ran out and
    # the merge bot, which shares it, stopped merging). From the orchestrator's own quota readings (factory.json "api").
    api = [(fac_days.get(d) or {}).get("api") or {} for d, _ in rows]
    if any(x.get("peak_hour") for x in api):
        top, chart_h = y, 110
        limit = 5000
        axis = _nice_axis(max([limit] + [x.get("peak_hour") or 0 for x in api]))
        frame(top, chart_h, axis, f"{panel} · GitHub API — calls in the busiest hour each day on the shared token "
              f"(limit {limit:,}/h; red: readings that found it exhausted)", lambda v: f"{v:,.0f}")
        yl = top + chart_h - chart_h * limit / axis[0]
        out.append(f'<line class="avg" x1="{left}" x2="{right}" y1="{yl:.1f}" y2="{yl:.1f}"/>'
                   f'<text class="avgl" x="{right}" y="{yl - 5:.1f}" text-anchor="end">limit {limit:,}/h</text>')
        for i, ((day, _), x) in enumerate(zip(rows, api)):
            v = x.get("peak_hour")
            if not v:
                continue
            h = chart_h * v / axis[0]
            bx = x_left(i) + (slot - bar_w) / 2
            cls = "failed" if x.get("readings_at_zero") else "hours"
            callers = ", ".join(f"{k} {n}" for k, n in list((x.get("churn_by_caller") or {}).items())[:4])
            tip = (f"{_fmt_day(day)}: busiest hour {v:,} calls; {x.get('readings_at_zero', 0)} readings at 0; churn made "
                   f"{x.get('churn_calls', 0):,} calls that day ({callers})")
            out.append(f'<g><title>{escape(tip)}</title><rect class="{cls}" x="{bx:.1f}" y="{top + chart_h - h:.1f}" '
                       f'width="{bar_w:.1f}" height="{h:.1f}" rx="2"/>'
                       + (f'<text class="vl" x="{bx + bar_w / 2:.1f}" y="{top + chart_h - h - 4:.1f}" text-anchor="middle">'
                          f'{x["readings_at_zero"]}×0</text>' if x.get("readings_at_zero") else "") + "</g>")
        y = top + chart_h + 70
        panel += 1

    # GitHub writes: each account's busiest hour of content-creating writes, against the ~500/h secondary-limit guidance
    writes = [(sc_days.get(d) or {}).get("writes") or {} for d, _ in rows]
    if any(writes):
        totals: Counter = Counter()
        for w in writes:
            for a, v in w.items():
                totals[a] += v.get("total", 0)
        actors = [a for a, _ in totals.most_common(3)]
        top, chart_h = y, 110
        axis = _nice_axis(max([WRITE_GUIDE_H] + [w.get(a, {}).get("busiest_hour", 0) for w in writes for a in actors]))
        frame(top, chart_h, axis, f"{panel} · GitHub writes — each account's busiest hour of comments, label/state "
              f"changes, merges and new PRs/issues (guidance ~{WRITE_GUIDE_H}/h)", lambda v: f"{v:g}")
        yl = top + chart_h - chart_h * WRITE_GUIDE_H / axis[0]
        out.append(f'<line class="avg" x1="{left}" x2="{right}" y1="{yl:.1f}" y2="{yl:.1f}"/>'
                   f'<text class="avgl" x="{right}" y="{yl - 5:.1f}" text-anchor="end">~{WRITE_GUIDE_H}/h</text>')
        for a, cls in zip(actors, ("cum", "failedl", "saved")):
            line_series(top, chart_h, [w[a]["busiest_hour"] if a in w else None for w in writes], axis[0], cls=cls,
                        tips=[f"{a}: busiest hour {w[a]['busiest_hour']} writes, {w[a]['total']} that day" if a in w
                              else "" for w in writes])
        legend(top - 30, [(a, c) for a, c in zip(actors, ("bot", "failed", "saved"))])
        y = top + chart_h + 70
        panel += 1

    prefixes = ", ".join(cfg.bot_prefixes)
    out.append(f'<text class="meta" x="{pad}" y="{y}">Source: GitHub API. Days are UTC; faded marks are today so far.</text>')
    y += 15
    out.append(f'<text class="meta" x="{pad}" y="{y}">{escape(sampling_note(cfg)).capitalize()}.</text>')
    out.append(f'<text class="meta" x="{pad}" y="{y + 15}">'
               + escape((f"{cfg.bot_name} = PRs from {prefixes} branches. " if prefixes else "")
                        + "Other agents = agent branch prefixes or PR-body markers. Failed % is per workflow run, not per PR; "
                          "minutes saved is estimated.") + '</text>')
    height = y + 34
    body = "\n  ".join(out)
    merged_total = sum(row["merged"] for _, row in rows)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'role="img" aria-label="{escape(cfg.heading)}: delivery, speed, cost and CI waste per day'
            f'{f", before and with {escape(cfg.bot_name)}" if start else ""}; '
            f'{merged_total:,} merged PRs in the selected period">\n'
            f'<style>{STYLE}</style>\n<g class="root">\n  <rect class="bg" x="0.5" y="0.5" width="{width - 1}" '
            f'height="{height - 1}" rx="10"/>\n  {body}\n</g>\n</svg>\n')


# ---------------------------------------------------------------- factory scorecard
# Owner 2026-10-24: merged PRs are motion, not results, and most people don't know what a PR count means. The
# dashboard adds what people ask of a code factory: issues opened (by whom) and closed, first-pass rate, change
# failure rate and cost per resolved issue; a separate goals sheet (goals.svg) shows the progress of whatever the
# factory is pointed at. GitHub-side figures are exact search counts; first-pass, spend and goals come from the
# orchestrator's factory.json (churn).
SCORE_DAYS = 21
ISSUE_SEARCH = """query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 50, after: $after) {
    issueCount pageInfo { hasNextPage endCursor }
    nodes { ... on Issue { number createdAt closedAt author { login __typename } bodyText
      timelineItems(itemTypes: [CROSS_REFERENCED_EVENT, CLOSED_EVENT], last: 60) { nodes { __typename
        ... on CrossReferencedEvent { source { ... on PullRequest { merged mergedAt headRefName } } }
        ... on ClosedEvent { closer { ... on PullRequest { headRefName } } } } } } }
  }
}"""
COUNT_SEARCH = """query($q: String!) { search(query: $q, type: ISSUE, first: 1) { issueCount } }"""
AGENT_ISSUE_MARKERS = r"Sibling gap|Found by (?:churn|an agent)|churn run|agy-authored"


def search_count(q: str) -> int:
    return int(gh_graphql(COUNT_SEARCH, q=q)["search"]["issueCount"])


def search_issues(q: str) -> list[dict]:
    out, after = [], None
    for _ in range(10):
        vars_ = {"q": q, **({"after": after} if after else {})}
        sr = gh_graphql(ISSUE_SEARCH, **vars_)["search"]
        out += [n for n in sr["nodes"] if n]
        if not sr["pageInfo"]["hasNextPage"]:
            break
        after = sr["pageInfo"]["endCursor"]
    return out


def issue_origin(node: dict, cfg: Config) -> str:
    """automation (a bot account), agent (an agent marker in the body: agents file the sibling gaps they find), or
    owner (everyone else: the people who decide what the factory works on)."""
    author = node.get("author") or {}
    login = str(author.get("login", ""))
    if author.get("__typename") == "Bot" or login.endswith("[bot]") or login == "github-actions":
        return "automation"
    body = node.get("bodyText") or ""
    if (cfg.agent_markers and re.search(cfg.agent_markers, body, re.I)) or re.search(AGENT_ISSUE_MARKERS, body, re.I):
        return "agent"
    return "owner"


def bot_worked(node: dict, cfg: Config) -> bool:
    """The issue had the bot's work merged: a merged PR from a bot branch referenced it, or a bot PR closed it. Owner
    2026-10-24: cost per resolved issue divides the bot's spend by THESE, not by every issue people resolved too (on
    the bot's first part-day, $12 over 18 mostly human-resolved issues read as $0.66 and drew a false trend)."""
    if not cfg.bot_prefixes:
        return False
    bot = lambda b: any(str(b or "").startswith(p) for p in cfg.bot_prefixes)  # noqa: E731
    closed = _iso(node["closedAt"]) if node.get("closedAt") else None
    for e in ((node.get("timelineItems") or {}).get("nodes") or []):
        src = e.get("source") or {}
        if bot((e.get("closer") or {}).get("headRefName")):
            return True
        # a bot PR merged by the time the issue closed (an hour's slack for the close that follows a merge); a later
        # slice that only mentions the closed issue ("Part of #n") did not resolve it
        if src.get("merged") and bot(src.get("headRefName")) and src.get("mergedAt") and \
                (closed is None or _iso(src["mergedAt"]) <= closed + timedelta(hours=1)):
            return True
    return False


def fetch_scorecard_day(repo: str, day: str, cfg: Config) -> dict:
    """One day's issue flow and change failures (about 6 GraphQL searches)."""
    base = f"repo:{repo} is:issue"
    resolved = search_issues(f"{base} closed:{day} reason:completed")
    hours = sorted((_iso(n["closedAt"]) - _iso(n["createdAt"])).total_seconds() / 3600 for n in resolved
                   if n.get("closedAt") and n.get("createdAt"))
    opened = Counter(issue_origin(n, cfg) for n in search_issues(f"{base} created:{day}"))
    return {
        "resolved": len(resolved),
        "resolved_by_bot": sum(bot_worked(n, cfg) for n in resolved),
        "resolved_by_origin": dict(Counter(issue_origin(n, cfg) for n in resolved)),
        "resolve_h_median": round(_pct(hours, 0.5), 1) if hours else None,
        "not_planned": search_count(f'{base} closed:{day} reason:"not planned"'),
        "closed": search_count(f"{base} closed:{day}"),
        "opened": sum(opened.values()), "opened_by_origin": dict(opened),
        "reverts": search_count(f"repo:{repo} is:pr is:merged merged:{day} Revert in:title"),
        "regression_fixes": search_count(f"repo:{repo} is:pr is:merged merged:{day} regression in:title"),
    }


# GitHub's secondary limits count content-creating writes per account (~80/min, ~500/h by its published guidance), apart
# from the hourly quota. Owner 2026-10-24: watch them. A day's writes by actor and hour, from the repo's own records:
# comments, write-type issue/PR events (labels, closes, ready, merges) and the PRs and issues opened.
WRITE_EVENTS = ("labeled", "unlabeled", "closed", "reopened", "ready_for_review", "convert_to_draft", "renamed",
                "assigned", "unassigned", "review_requested", "merged", "head_ref_force_pushed")
WRITE_GUIDE_H = 500
CREATED_SEARCH = """query($q: String!, $after: String) { search(query: $q, type: ISSUE, first: 100, after: $after) {
  pageInfo { hasNextPage endCursor }
  nodes { ... on PullRequest { createdAt author { login } } ... on Issue { createdAt author { login } } } } }"""


def fetch_writes_day(repo: str, day: str, max_pages: int = 60) -> dict:
    """{actor: {"total", "busiest_hour", "hours": {HH: n}}} for one UTC day. Issue events have no date filter, so they
    are read newest first down to the day (recent days only: about 30 pages for a busy day)."""
    hours: dict[str, Counter] = {}

    def add(actor: str | None, stamp: str) -> None:
        if actor and stamp.startswith(day):
            hours.setdefault(actor, Counter())[stamp[11:13]] += 1

    for page in range(1, max_pages + 1):
        got = gh_json(f"repos/{repo}/issues/comments?since={day}T00:00:00Z&sort=created&direction=asc&per_page=100"
                      f"&page={page}")
        for c in got:
            add((c.get("user") or {}).get("login"), c["created_at"])
        if len(got) < 100 or (got and got[-1]["created_at"][:10] > day):
            break
    for page in range(1, max_pages + 1):
        got = gh_json(f"repos/{repo}/issues/events?per_page=100&page={page}")
        for e in got:
            if e.get("event") in WRITE_EVENTS:
                add((e.get("actor") or {}).get("login"), e["created_at"])
        if len(got) < 100 or (got and got[-1]["created_at"][:10] < day):
            break
    for kind in ("pr", "issue"):
        after = None
        for _ in range(10):
            sr = gh_graphql(CREATED_SEARCH, q=f"repo:{repo} is:{kind} created:{day}",
                            **({"after": after} if after else {}))["search"]
            for n in sr["nodes"]:
                if n:
                    add((n.get("author") or {}).get("login"), n["createdAt"])
            if not sr["pageInfo"]["hasNextPage"]:
                break
            after = sr["pageInfo"]["endCursor"]
    return {a: {"total": sum(c.values()), "busiest_hour": max(c.values()), "hours": dict(sorted(c.items()))}
            for a, c in sorted(hours.items(), key=lambda kv: -sum(kv[1].values()))}


def collect_scorecard(cfg: Config, data: dict, today: date, days: int = SCORE_DAYS) -> dict:
    sc = data.get("scorecard") or {"days": {}}
    window = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    for d in window:  # finished days once, the recent ones on every run
        if d not in sc["days"] or d >= (today - timedelta(days=REFRESH_DAYS - 1)).isoformat():
            try:
                sc["days"][d] = fetch_scorecard_day(cfg.repo, d, cfg)
            except RuntimeError as e:
                print(f"::warning::scorecard for {d} not refreshed: {e}", file=sys.stderr)
    for d in window[-REFRESH_DAYS:]:  # writes: the recent days only (issue events are read newest first)
        try:
            sc["days"].setdefault(d, {})["writes"] = fetch_writes_day(cfg.repo, d)
        except RuntimeError as e:
            print(f"::warning::writes for {d} not counted: {e}", file=sys.stderr)
    sc["open_now"] = search_count(f"repo:{cfg.repo} is:issue is:open")
    sc["open_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    return sc


# ---------------------------------------------------------------- goals sheet
# Owner 2026-10-24: the dashboard shows the factory's project-independent numbers; goals.svg shows the progress of
# whatever the factory is pointed at. The orchestrator publishes each goal in factory.json["goals"] in one generic shape:
# {id, title, unit, levels (low -> high), target, group_label, measured, history {day: {level: n}},
#  groups {group: {level: n}}, blockers [{name, group, holes, note, issues}]}.
LEVEL_CLASSES = ("failed", "bots", "agent", "other", "agents", "people")  # lowest level red ... highest blue/purple


def render_goals(data: dict, today: date, cfg: Config) -> str | None:
    goals = (data.get("factory") or {}).get("goals") or []
    if not goals:
        return None
    width, pad = 920, 24
    out, y = [], 34
    out.append(f'<text class="h1" x="{pad}" y="{y}">{escape(cfg.repo)}: goal progress</text>')
    out.append(f'<text class="lead" x="{pad}" y="{y + 20}">What the factory is pointed at, and how far it has got. '
               'The main dashboard has the numbers that hold for any project.</text>')
    y += 52
    for g in goals:
        levels, target = list(g.get("levels") or []), g.get("target")
        cls = {lv: ("agents" if lv == target else LEVEL_CLASSES[min(i, 3)]) for i, lv in enumerate(levels)}
        for lv in levels[levels.index(target) + 1:] if target in levels else []:
            cls[lv] = "people"  # above the target
        hist = {d: v for d, v in sorted((g.get("history") or {}).items())}
        now_lv = (list(hist.values())[-1] if hist else {}) or {}
        total = sum(now_lv.values())
        days = list(hist)
        first = hist[days[0]] if days else {}
        week = next((hist[d] for d in days if d >= (today - timedelta(days=7)).isoformat()), first)
        at = sum(now_lv.get(lv, 0) for lv in levels[levels.index(target):]) if target in levels else 0
        at_first = sum(first.get(lv, 0) for lv in levels[levels.index(target):]) if target in levels else 0
        at_week = sum(week.get(lv, 0) for lv in levels[levels.index(target):]) if target in levels else 0
        blockers = g.get("blockers") or []
        groups = g.get("groups") or {}
        done_groups = sum(1 for c in groups.values() if sum(c.values()) and
                          sum(c.get(lv, 0) for lv in levels[levels.index(target):]) == sum(c.values()))
        out.append(f'<text class="h2" x="{pad}" y="{y + 12}">{escape(g.get("title", g.get("id", "goal")))}</text>')
        out.append(f'<text class="meta" x="{width - pad}" y="{y + 12}" text-anchor="end">target {escape(str(target))} · '
                   f'measured {escape(str(g.get("measured") or "?").replace("T", " "))}</text>')
        y += 24
        unit = g.get("unit", "items")
        below = [f"{lv} {now_lv.get(lv, 0)}" for lv in levels if lv in now_lv and (target not in levels or
                                                                                   levels.index(lv) < levels.index(target))]
        tiles = [
            (f"AT {target} OR ABOVE", f"{at} of {total}", f"{100 * at / total:.0f}% of {unit}" if total else ""),
            ("CHANGE", f"{at - at_first:+d}", f"since {_fmt_day(days[0])}; {at - at_week:+d} in 7 days" if days else ""),
            (f"BELOW {target}", f"{total - at}", " · ".join(below)),
            ("BLOCKED", f"{len(blockers)}", f"{sum(1 for b in blockers if not b.get('issues'))} with no open issue · "
             f"{sum(int(b.get('holes') or 0) for b in blockers)} holes"),
            (f"{str(g.get('group_label', 'group')).upper()}S DONE", f"{done_groups} of {len(groups)}",
             f"every {unit[:-1] if unit.endswith('s') else unit} at {target}+"),
        ]
        tw = (width - 2 * pad - 4 * 10) / 5
        for i, (k, v, sub) in enumerate(tiles):
            x = pad + i * (tw + 10)
            out.append(f'<rect class="tile" x="{x:.1f}" y="{y}" width="{tw:.1f}" height="70" rx="8"/>'
                       f'<text class="k" x="{x + 12:.1f}" y="{y + 18}">{escape(k)}</text>'
                       f'<text class="v" x="{x + 12:.1f}" y="{y + 43}">{escape(v)}</text>'
                       f'<text class="sub" x="{x + 12:.1f}" y="{y + 60}">{escape(sub)}</text>')
        y += 96

        # level mix over time
        if days:
            top, chart_h, left, right = y + 18, 150, pad + 36, width - pad
            out.append(f'<text class="h2" x="{pad}" y="{y + 4}">{escape(unit.capitalize())} by level, each measured day</text>')
            mx, step = _nice_axis(max(sum(v.values()) for v in hist.values()) * 1.12 or 1)  # headroom for the labels
            for t in range(round(mx / step) + 1):
                yy = top + chart_h - chart_h * t * step / mx
                out.append(f'<line class="{"base" if t == 0 else "grid"}" x1="{left}" x2="{right}" y1="{yy:.1f}" y2="{yy:.1f}"/>'
                           f'<text class="tick" x="{left - 6}" y="{yy + 3:.1f}" text-anchor="end">{t * step:g}</text>')
            slot = (right - left) / max(len(days), 1)
            bw = min(48.0, slot * 0.7)
            for i, d in enumerate(days):
                x, yy = left + i * slot + (slot - bw) / 2, top + chart_h
                for lv in levels:
                    v = hist[d].get(lv, 0)
                    if v:
                        h = chart_h * v / mx
                        out.append(f'<rect class="{cls[lv]} seg" x="{x:.1f}" y="{yy - h:.1f}" width="{bw:.1f}" height="{h:.1f}">'
                                   f'<title>{_fmt_day(d)}: {v} {unit} at {lv}</title></rect>')
                        yy -= h
                n_at = sum(hist[d].get(lv, 0) for lv in levels[levels.index(target):]) if target in levels else 0
                out.append(f'<text class="vl" x="{x + bw / 2:.1f}" y="{yy - 4:.1f}" text-anchor="middle">{target} {n_at}</text>'
                           f'<text class="tick" x="{x + bw / 2:.1f}" y="{top + chart_h + 14}" text-anchor="middle">'
                           f'{_fmt_day(d)}</text>')
            lx = right
            for lv in reversed(levels):
                lx -= 46
                out.append(f'<rect class="{cls[lv]}" x="{lx:.1f}" y="{y - 4}" width="10" height="10" rx="2"/>'
                           f'<text class="lbl" x="{lx + 14:.1f}" y="{y + 5}">{escape(lv)}</text>')
            y = top + chart_h + 40

        # by group: one 100% bar each, best first
        if groups:
            label = str(g.get("group_label", "group"))
            out.append(f'<text class="h2" x="{pad}" y="{y}">By {escape(label)}: share at each level (count at {target}+ / total)</text>')
            y += 12
            name_w, bar_l, bar_r = 190, pad + 196, width - pad - 70
            order = sorted(groups.items(), key=lambda kv: (-(sum(kv[1].get(lv, 0) for lv in levels[levels.index(target):])
                                                            / max(1, sum(kv[1].values()))), kv[0]))
            for name, c in order:
                n = sum(c.values())
                x = bar_l
                out.append(f'<text class="lbl" x="{pad + name_w:.1f}" y="{y + 11}" text-anchor="end">{escape(_short(name, 30))}</text>')
                for lv in levels:
                    v = c.get(lv, 0)
                    if v:
                        w = (bar_r - bar_l) * v / n
                        out.append(f'<rect class="{cls[lv]} seg" x="{x:.1f}" y="{y + 2}" width="{w:.1f}" height="12">'
                                   f'<title>{escape(name)}: {v} of {n} at {lv}</title></rect>')
                        x += w
                k = sum(c.get(lv, 0) for lv in levels[levels.index(target):])
                out.append(f'<text class="vl" x="{bar_r + 8:.1f}" y="{y + 12}">{k}/{n}</text>')
                y += 18
            y += 22

        # blockers
        if blockers:
            out.append(f'<text class="h2" x="{pad}" y="{y}">Blocked: not translated whole ({len(blockers)})</text>')
            y += 18
            cols = [(pad, "program"), (pad + 170, label if groups else "group"), (pad + 380, "holes"), (pad + 430, "issues"),
                    (pad + 540, "why")]
            for x, h in cols:
                out.append(f'<text class="k" x="{x}" y="{y}">{escape(h.upper())}</text>')
            y += 6
            for b in sorted(blockers, key=lambda b: (bool(b.get("issues")), -int(b.get("holes") or 0)))[:12]:
                y += 16
                iss = ", ".join(f"#{n}" for n in (b.get("issues") or [])[:3]) or "none"
                vals = [_short(Path(str(b.get("name"))).name, 24), _short(str(b.get("group")), 30), str(b.get("holes") or 0),
                        iss, _short(str(b.get("note") or ""), 52)]
                for n_col, ((x, _), v) in enumerate(zip(cols, vals)):
                    klass = "noissue" if n_col == 3 and v == "none" else "lbl"  # nobody is on it: the one to act on
                    out.append(f'<text class="{klass}" x="{x}" y="{y}">{escape(v)}</text>')
            y += 28
        y += 10
    out.append(f'<text class="meta" x="{pad}" y="{y}">Source: the factory&apos;s own goal measurements (factory.json '
               '"goals", published by the orchestrator). A goal is any ordered set of levels with a target.</text>')
    height = y + 20
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'role="img" aria-label="{escape(cfg.repo)} goal progress">\n<style>{STYLE}</style>\n<g class="root">\n'
            f'  <rect class="bg" x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="10"/>\n  '
            + "\n  ".join(out) + "\n</g>\n</svg>\n")


# ---------------------------------------------------------------- CLI

def _opt_float(s: str | None) -> float | None:
    return float(s) if s not in (None, "") else None


def main(argv: list[str] | None = None) -> int:
    env = os.environ.get
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", default=env("FLEET_REPORT_REPO") or env("GITHUB_REPOSITORY"))
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--out", type=Path, default=Path("dashboard"))
    p.add_argument("--title", default="")
    p.add_argument("--bot-workflows", default="", help="comma-separated names of an orchestrator's own bookkeeping workflows")
    p.add_argument("--agent-branch-prefixes", default=DEFAULT_AGENT_PREFIXES)
    p.add_argument("--agent-markers", default=DEFAULT_AGENT_MARKERS, help="regex matched against PR bodies")
    p.add_argument("--bot-branch-prefixes", default="", help="head-branch prefixes of the bot this dashboard follows")
    p.add_argument("--bot-name", default="bot")
    p.add_argument("--bot-start", default="", help="YYYY-MM-DD the bot went live (default: its first merged PR)")
    p.add_argument("--events", default="", help="'YYYY-MM-DD: label; ...' marked on the charts")
    p.add_argument("--sample-fraction", type=float, default=0.05, help="share of each day's runs measured")
    p.add_argument("--sample-min", type=int, default=30, help="never fewer runs than this a day")
    p.add_argument("--sample-runs", type=int, help="deprecated: the old fixed per-day sample; read as --sample-min")
    p.add_argument("--sample-budget", type=int, default=400, help="new run lookups per invocation (API budget)")
    p.add_argument("--runner-pool", default="", help="concurrent runners; estimates above pool x 24 h are flagged")
    p.add_argument("--occupancy-scope", choices=["org", "repo"], default="org",
                   help="count every repository of the owner (they share the runner pool; default) or --repo only")
    p.add_argument("--occupancy-sample", action="store_true",
                   help=f"append one runner-occupancy reading to <out>/{OCC_FILE} and exit (run every ~15 min)")
    p.add_argument("--detail-backfill", type=int, default=DETAIL_BACKFILL,
                   help="older days to fill with the sampled detail this run (each costs ~30 API calls)")
    p.add_argument("--summary", type=Path, help="append the Markdown report here (the Action passes $GITHUB_STEP_SUMMARY)")
    p.add_argument("--max-wait-p90", default="", help="alert when the 7-day p90 job queue wait (min) exceeds this")
    p.add_argument("--max-runner-hours-per-pr", default="", help="alert when 7-day runner hours per merged PR exceed this")
    p.add_argument("--cost-max", default="", help="top of the cost-per-PR chart axis, hours (default: fit the data)")
    p.add_argument("--fail-on-alert", action="store_true")
    p.add_argument("--render-only", action="store_true", help="redraw from metrics.json without fetching")
    a = p.parse_args(argv)
    if not a.repo:
        p.error("--repo is required (or GITHUB_REPOSITORY)")
    cfg = Config(repo=a.repo, title=a.title, bot_workflows=_csv(a.bot_workflows),
                 agent_prefixes=_csv(a.agent_branch_prefixes), agent_markers=a.agent_markers,
                 sample_fraction=a.sample_fraction, sample_min=a.sample_runs or a.sample_min,
                 sample_budget=a.sample_budget, runner_pool=int(a.runner_pool) if str(a.runner_pool).strip() else None,
                 bot_prefixes=_csv(a.bot_branch_prefixes), bot_name=a.bot_name or "bot", bot_start=a.bot_start,
                 events=_events(a.events), cost_max=_opt_float(a.cost_max))
    a.out.mkdir(parents=True, exist_ok=True)
    occ_file = a.out / OCC_FILE
    if a.occupancy_sample:
        reading = occupancy_reading(cfg.repo, a.occupancy_scope)
        cut = (datetime.now(timezone.utc) - timedelta(days=OCC_KEEP_DAYS)).strftime("%Y-%m-%d")
        kept = [r for r in read_occupancy(occ_file) if str(r.get("t", "")) >= cut]
        occ_file.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in [*kept, reading]))
        print(f"occupancy ({reading['scope']}): {reading['running']} jobs running, {reading['queued']} queued in running "
              f"runs, {reading['queued_runs']} runs waiting; {reading['repos_read']} repos read: "
              + ", ".join(f"{k} {v['running']}" for k, v in sorted(reading["by_repo"].items())), file=sys.stderr)
        return 0
    data_file = a.out / "metrics.json"
    data = json.loads(data_file.read_text()) if data_file.exists() else {}
    today = datetime.now(timezone.utc).date()
    if not a.render_only:
        data = collect(cfg, a.days, data, today, a.detail_backfill)
        data_file.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        # a run that starts before midnight UTC finishes after it: draw "today" as of the finish (the "updated" stamp),
        # so the day that just ended isn't labelled "today so far"
        today = datetime.now(timezone.utc).date()
    readings = read_occupancy(occ_file)
    for d, row in data.get("days", {}).items():  # occupancy is attached at render time: no API, always current
        if readings and "detail" in row and (occ := occupancy_day(readings, d, repo=cfg.repo)):
            row["detail"]["occupancy"] = occ  # this repository's runner time
            if any(r.get("scope") == "org" for r in readings) and (pool := occupancy_day(readings, d)):
                row["detail"]["occupancy_org"] = pool  # every repository's: the pool's real load
    factory_file = a.out / "factory.json"  # an orchestrator's own numbers, published to the same branch (optional)
    if factory_file.exists():
        try:
            data = {**data, "factory": json.loads(factory_file.read_text())}
        except ValueError as e:
            print(f"::warning title=Fleet CI report::factory.json unreadable: {e}", file=sys.stderr)
    (a.out / "dashboard.svg").write_text(render(data, a.days, today, cfg), encoding="utf-8")
    goals_svg = render_goals(data, today, cfg)
    if goals_svg:
        (a.out / "goals.svg").write_text(goals_svg, encoding="utf-8")
    alerts = check_alerts(data, a.days, today, _opt_float(a.max_wait_p90), _opt_float(a.max_runner_hours_per_pr),
                          cfg.runner_pool)
    for msg in alerts:
        print(f"::warning title=Fleet CI report::{msg}")
    if a.summary:
        with a.summary.open("a", encoding="utf-8") as f:
            f.write(summary_md(data, cfg, a.days, today, alerts))
    print(f"wrote {a.out}/dashboard.svg and metrics.json ({len(data['days'])} days of history)", file=sys.stderr)
    return 1 if alerts and a.fail_on_alert else 0


if __name__ == "__main__":
    raise SystemExit(main())
