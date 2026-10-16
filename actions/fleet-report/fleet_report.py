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
import json
import os
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
DEFAULT_AGENT_PREFIXES = "claude/,codex/,copilot/,cursor/,devin/"
DEFAULT_AGENT_MARKERS = (r"Generated with \[?Claude Code|Co-Authored-By: Claude|Co-authored-by: Copilot|"
                         r"agy-authored|Devin AI|Cursor Agent|OpenAI Codex|Gemini CLI")
CLASSES = ("agent", "other", "automation")


def _csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


@dataclass
class Config:
    repo: str
    title: str = ""
    bot_workflows: list[str] = field(default_factory=list)
    agent_prefixes: list[str] = field(default_factory=lambda: _csv(DEFAULT_AGENT_PREFIXES))
    agent_markers: str = DEFAULT_AGENT_MARKERS
    sample_runs: int = 24

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
    """agent: an agent branch prefix or an agent marker in the body; automation: any other bot account
    (dependabot, renovate, release bots); other: everything else (people, or agents that leave no trace)."""
    branch = pr.get("headRefName") or ""
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


def rerun_counts(repo: str, day: str) -> dict:
    """Exact: the day's runs that were rerun (run_attempt > 1) and how their latest attempt ended, per workflow.
    A rerun that failed again usually retried a real failure; one that passed was a flake or an outage."""
    by_wf: dict[str, list[int]] = {}
    attempts = 0
    for h in range(0, 24, 2):  # a listing stops at 1,000 runs: 2-hour windows stay under it on a busy day
        span = f"{day}T{h:02d}:00:00Z..{day}T{h + 1:02d}:59:59Z"
        for page in range(1, 11):
            runs = gh_json(f"repos/{repo}/actions/runs?created={span}&per_page=100&page={page}")["workflow_runs"]
            for r in runs:
                if (r.get("run_attempt") or 1) > 1 and r.get("status") == "completed":
                    attempts += r["run_attempt"] - 1
                    c = by_wf.setdefault(r["name"], [0, 0, 0])
                    c[{"success": 0, "failure": 1}.get(r.get("conclusion"), 2)] += 1
            if len(runs) < 100:
                break
    return {"runs": sum(sum(c) for c in by_wf.values()), "attempts": attempts,
            "passed": sum(c[0] for c in by_wf.values()), "failed_again": sum(c[1] for c in by_wf.values()),
            "by_workflow": by_wf}


def fetch_detail(repo: str, day: str, total_runs: int, bots: dict[str, int], origin, cfg: Config) -> dict:
    """Bot-run count and CI-change PRs (exact); job queue wait and runner minutes (sampled)."""
    bot_runs = sum(gh_count(f"repos/{repo}/actions/workflows/{i}/runs?created={day}&per_page=1") for i in bots.values())
    changes: dict[int, str] = {}
    for c in gh_json(f"repos/{repo}/commits?path=.github/workflows&since={day}T00:00:00Z&until={day}T23:59:59Z&per_page=100"):
        title = c["commit"]["message"].splitlines()[0]
        m = re.search(r"\(#(\d+)\)\s*$", title)
        if m:  # squash merges of PRs; direct bot commits carry no PR number
            changes[int(m.group(1))] = title
    waits: list[float] = []
    run_min: list[float] = []
    by_wf: Counter[str] = Counter()
    by_origin: Counter[str] = Counter()
    per_window = max(1, cfg.sample_runs // 4)
    for h in (0, 6, 12, 18):
        page = gh_json(f"repos/{repo}/actions/runs?created={day}T{h:02d}:00:00Z..{day}T{h + 5:02d}:59:59Z&per_page=100")
        runs = [r for r in page["workflow_runs"] if r.get("conclusion") != "skipped" and r.get("status") == "completed"]
        step = max(1, len(runs) // per_window)
        for r in runs[::step][:per_window]:
            try:
                jobs = gh_json(f"repos/{repo}/actions/runs/{r['id']}/jobs?per_page=100")["jobs"]
            except RuntimeError as e:  # one unreadable run shrinks the sample, it doesn't stop the report
                print(f"::notice::skipped run {r['id']} in the sample: {e}", file=sys.stderr)
                continue
            minutes = 0.0
            for j in jobs:
                if j.get("started_at") and j.get("completed_at") and j.get("conclusion") != "skipped":
                    m = (_iso(j["completed_at"]) - _iso(j["started_at"])).total_seconds() / 60
                    if m > 0.05:  # a job that never really ran held no runner
                        minutes += m
                        waits.append(max(0.0, (_iso(j["started_at"]) - _iso(j["created_at"])).total_seconds() / 60))
            run_min.append(minutes)
            by_wf[r["name"]] += minutes
            by_origin[origin(r)] += minutes
    return {
        "bot_runs": bot_runs,
        "ci_changes": [{"pr": n, "title": t} for n, t in sorted(changes.items())],
        "wait_median": round(_pct(waits, 0.5), 2),
        "wait_p90": round(_pct(waits, 0.9), 2),
        "runner_min_est": round(sum(run_min) / len(run_min) * total_runs) if run_min else None,
        "sampled_runs": len(run_min),
        "sampled_min_by_workflow": {k: round(v, 1) for k, v in by_wf.items()},
        "sampled_min_by_origin": {k: round(v, 1) for k, v in by_origin.items()},
        "reruns": rerun_counts(repo, day),
    }


def fetch_day(repo: str, day: str, cfg: Config) -> dict:
    # total_count tops out at 2,500 per query, a busy day passes that: count it in 6-hour windows
    runs = {"total": sum(
        gh_count(f"repos/{repo}/actions/runs?created={day}T{h:02d}:00:00Z..{day}T{h + 5:02d}:59:59Z&per_page=1")
        for h in (0, 6, 12, 18)
    )}
    for outcome in OUTCOMES:
        runs[outcome] = gh_count(f"repos/{repo}/actions/runs?created={day}&status={outcome}&per_page=1")
    return {**merged_counts(repo, day, cfg), "runs": runs}


def days_to_fetch(have: dict, today: date, days: int) -> list[str]:
    window = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    fresh = {(today - timedelta(days=i)).isoformat() for i in range(REFRESH_DAYS)}
    return [d for d in window if d not in have or d in fresh]


def collect(cfg: Config, days: int, data: dict, today: date, backfill: int = DETAIL_BACKFILL) -> dict:
    repo = cfg.repo
    if data.get("repo") != repo:
        data = {"repo": repo, "days": {}}
    fresh = days_to_fetch(data["days"], today, days)
    for d in fresh:
        data["days"][d] = fetch_day(repo, d, cfg)
    window = [(today - timedelta(days=i)).isoformat() for i in range(days)]
    for d in window:  # history from before PR classing: one GraphQL query a day fills it in
        if d in data["days"] and "merged_by_class" not in data["days"][d]:
            data["days"][d].update(merged_counts(repo, d, cfg))
    bots = bot_workflow_ids(repo, cfg)
    origin = RunOrigin(repo, cfg)
    missing = [d for d in window if d not in fresh and d in data["days"]
               and not {"sampled_min_by_origin", "reruns"} <= data["days"][d].get("detail", {}).keys()]
    for d in fresh + missing[:backfill]:
        r = data["days"][d]["runs"]
        try:
            data["days"][d]["detail"] = fetch_detail(repo, d, r["total"] - r["skipped"], bots, origin, cfg)
        except RuntimeError as e:  # keep the day's previous detail (if any); the next run tries again
            print(f"::warning::sampled detail for {d} not refreshed: {e}", file=sys.stderr)
    data["open_prs"] = gh_count(f"search/issues?q=repo:{repo}+is:pr+is:open&per_page=1")
    data["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    return data


# ---------------------------------------------------------------- aggregation

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
    agent = sum(r["merged_by_class"]["agent"] for r in classed)
    runs = sum(r["runs"]["total"] for r in rows)
    ok = sum(r["runs"]["success"] for r in rows)
    bad = sum(r["runs"]["failure"] for r in rows)
    det = [r["detail"] for r in rows if "detail" in r]
    est = [(r["detail"]["runner_min_est"], r["merged"]) for r in rows if r.get("detail", {}).get("runner_min_est") is not None]
    return {
        "days": n,
        "merged_per_day": merged / n,
        "agent_share": agent / max(1, sum(r["merged"] for r in classed)) if classed else None,
        "runs_per_day": runs / n,
        "pass_rate": ok / (ok + bad) if ok + bad else None,
        "wait_median": sum(x["wait_median"] for x in det) / len(det) if det else None,
        "wait_p90": sum(x["wait_p90"] for x in det) / len(det) if det else None,
        "runner_h_per_pr": sum(m for m, _ in est) / max(1, sum(k for _, k in est)) / 60 if est else None,
        "bot_share": sum(x["bot_runs"] for x in det) / max(1, sum(r["runs"]["total"] for r in rows if "detail" in r)) if det else None,
        "ci_changes": [c for x in det for c in x["ci_changes"]],
        **rerun_stats(det),
    }


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
    done = finished(window_rows(data, days, today), today)
    cur, prev = week_stats(done[-7:]), week_stats(done[-14:-7])
    out = [f"## {cfg.heading}", "",
           f"Updated {data['updated'].replace('T', ' ').replace('Z', ' UTC')}. Last 7 finished days vs the 7 before. "
           f"Days are UTC; wait and runner time are sampled from about {cfg.sample_runs} runs a day.", ""]
    if alerts:
        out += ["> [!WARNING]", *[f"> {a}" for a in alerts], ""]
    out += ["| Measure | Last 7 days | Previous 7 days |", "|---|---|---|"]
    lines = [("Merged PRs / day", "merged_per_day", "n"), ("Agent share of merged PRs", "agent_share", "pct"),
             ("CI runs / day", "runs_per_day", "n"), ("CI pass rate", "pass_rate", "pct"),
             ("Job queue wait, median (sampled)", "wait_median", "min"), ("Job queue wait, p90 (sampled)", "wait_p90", "min"),
             ("Runner time per merged PR (est.)", "runner_h_per_pr", "h")]
    if cfg.bot_workflows:
        lines.append(("Bot workflow share of runs", "bot_share", "pct"))
    lines += [("Reruns / day", "reruns_per_day", "n"), ("Reruns whose latest attempt passed", "rerun_pass_rate", "pct")]
    for label, k, kind in lines:
        out.append(f"| {label} | {_fmt(cur.get(k), kind)} | {_fmt(prev.get(k), kind)} |")
    out.append(f"| CI changes merged | {len(cur.get('ci_changes', []))} | {len(prev.get('ci_changes', []))} |")
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
    org = runner_split(done[-7:], "sampled_min_by_origin")
    if org:
        names = {"agent": "Agent PRs", "other": "Other PRs", "automation": "Automation PRs",
                 "main": "Default branch & schedules"}
        out += ["", "### Runner time by origin (sampled, last 7 days)", "", "| Origin | Share |", "|---|---|"]
        out += [f"| {names.get(k, k)} | {100 * v:.1f}% |" for k, v in org]
    if cur.get("ci_changes"):
        out += ["", "### CI changes merged (last 7 days)", ""]
        out += [f"- #{c['pr']}: {c['title']}" for c in cur["ci_changes"][-25:]]
        if len(cur["ci_changes"]) > 25:
            out.append(f"- …and {len(cur['ci_changes']) - 25} earlier in the week")
    out += ["", "<sub>Exact: merged PRs, run counts and outcomes, bot runs, CI changes, reruns. Sampled: queue wait and runner "
            "time. Agent PRs: an agent branch prefix or an agent marker in the PR body.</sub>", ""]
    return "\n".join(out)


def check_alerts(data: dict, days: int, today: date, max_wait_p90: float | None, max_h_per_pr: float | None) -> list[str]:
    cur = week_stats(finished(window_rows(data, days, today), today)[-7:])
    out = []
    if max_wait_p90 is not None and cur.get("wait_p90") is not None and cur["wait_p90"] > max_wait_p90:
        out.append(f"Job queue wait p90 averaged {cur['wait_p90']:.1f} min over the last 7 days (limit {max_wait_p90:g}).")
    if max_h_per_pr is not None and cur.get("runner_h_per_pr") is not None and cur["runner_h_per_pr"] > max_h_per_pr:
        out.append(f"Runner time per merged PR was {cur['runner_h_per_pr']:.1f} h over the last 7 days (limit {max_h_per_pr:g}).")
    return out


# ---------------------------------------------------------------- SVG rendering

STYLE = """
  .root { --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781; --grid:#e1e0d9;
          --axis:#c3c2b7; --border:rgba(11,11,11,0.10); --tile:#f4f3f0; --series:#2a78d6; --series2:#1baf7a;
          --pass:#2a78d6; --fail:#eb6834; --neutral:#c3c2b7; }
  @media (prefers-color-scheme: dark) {
    .root { --surface:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --muted:#898781; --grid:#2c2c2a;
            --axis:#383835; --border:rgba(255,255,255,0.10); --tile:#232322; --series:#3987e5; --series2:#199e70;
            --pass:#3987e5; --fail:#d95926; --neutral:#5c5c58; }
  }
  text { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; fill: var(--ink); }
  .bg { fill: var(--surface); stroke: var(--border); }
  .tile { fill: var(--tile); }
  .h1 { font-size: 16px; font-weight: 650; }
  .h2 { font-size: 12.5px; font-weight: 600; }
  .meta, .tick, .lbl { font-size: 10.5px; fill: var(--muted); }
  .tick { font-variant-numeric: tabular-nums; }
  .k { font-size: 11px; fill: var(--ink2); }
  .v { font-size: 22px; font-weight: 650; }
  .sub { font-size: 10.5px; fill: var(--ink2); }
  .grid { stroke: var(--grid); stroke-width: 1; }
  .base { stroke: var(--axis); stroke-width: 1; }
  .s, .agent { fill: var(--series); } .other { fill: var(--series2); } .automation { fill: var(--neutral); }
  .ok { fill: var(--pass); } .bad { fill: var(--fail); } .na { fill: var(--neutral); }
  .partial { opacity: 0.5; }
  .l1 { fill: none; stroke: var(--series); stroke-width: 2; } .l1d { fill: var(--series); stroke: var(--surface); stroke-width: 2; }
  .l2 { fill: none; stroke: var(--fail); stroke-width: 2; } .l2d { fill: var(--fail); stroke: var(--surface); stroke-width: 2; }
  .mark { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 2 3; } .markd { fill: var(--muted); }
"""


def _nice_max(v: float) -> int:
    for step in (1, 2, 3, 5, 10, 15, 20, 25, 30, 40, 50, 75, 100, 150, 200, 250, 300, 400, 500, 750, 1000, 1500, 2000, 2500, 5000):
        if v <= step * 4:
            return step * 4
    return int(v) + 1


def _bar_path(x: float, y: float, w: float, h: float, r: float) -> str:
    """Bar with rounded top corners only (the data end), square at the baseline."""
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z")


def _short(name: str, n: int = 22) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


def _fmt_day(d: str) -> str:
    return date.fromisoformat(d).strftime("%b %-d")


def render(data: dict, days: int, today: date, cfg: Config | None = None) -> str:
    cfg = cfg or Config(repo=data["repo"])
    rows = window_rows(data, days, today)
    if not rows:
        raise SystemExit("no data to render")
    today_s = today.isoformat()
    W, pad = 880, 24
    out: list[str] = []

    out.append(f'<text class="h1" x="{pad}" y="34">{escape(cfg.heading)}</text>')
    out.append(f'<text class="meta" x="{W - pad}" y="34" text-anchor="end">last {len(rows)} days · '
               f'updated {escape(data["updated"].replace("T", " ").replace("Z", " UTC"))}</text>')

    # stat tiles
    merged = [r["merged"] for _, r in rows]
    done = finished(rows, today) or [r for _, r in rows]
    last7 = done[-7:]
    total_runs = sum(r["runs"]["total"] for _, r in rows)
    ok = sum(r["runs"]["success"] for _, r in rows)
    bad = sum(r["runs"]["failure"] for _, r in rows)
    rate = f"{100 * ok / (ok + bad):.1f}%" if ok + bad else "–"
    tiles = [
        ("Merged PRs", f"{sum(merged):,}", f"{sum(r['merged'] for r in last7) / len(last7):.1f}/day over the last 7 days"),
        ("CI runs", f"{total_runs:,}", f"{sum(r['runs']['total'] for r in last7) / len(last7):,.0f}/day over the last 7 days"),
        ("CI pass rate", rate, f"{ok:,} passed · {bad:,} failed"),
        ("Open PRs", f"{data.get('open_prs', 0):,}", "right now"),
    ]
    det7 = [r for r in done if "detail" in r][-7:]
    if det7:
        s = week_stats(det7)
        n7 = s["days"]
        third = (("Agent share of merged PRs", _fmt(s["agent_share"], "pct"), f"last {n7} days · branch or body marker")
                 if s["agent_share"] is not None else
                 ("Bot workflow runs", _fmt(s["bot_share"], "pct"), f"last {n7} days · configured bot workflows"))
        tiles += [
            ("Job queue wait (p90)", _fmt(s["wait_p90"], "min"), f"median {_fmt(s['wait_median'], 'min')} · sampled, last {n7} days"),
            ("Runner time per merged PR", _fmt(s["runner_h_per_pr"], "h"), f"est. from sampled jobs · last {n7} days"),
            third,
            ("CI changes merged", f"{len(s['ci_changes'])}", f"last {n7} days · ◆ on the wait chart"),
        ]
        rr = rerun_split(det7)
        if rr:
            tot, passed, failed = sum(t[1] for t in rr), sum(t[2] for t in rr), sum(t[3] for t in rr)
            top, flaky = rr[0], max(rr, key=lambda t: (t[2], -t[3]))
            tiles += [
                ("Reruns", _fmt(s["reruns_per_day"], "n") + "/day", f"{tot:,} runs rerun · last {n7} days"),
                ("Reruns that passed", _fmt(s["rerun_pass_rate"], "pct"), f"{passed:,} passed · {failed:,} failed again"),
                ("Most rerun workflow", f"{top[1]}", f"{_short(top[0])} · {top[3]} failed again"),
                ("Most rescued by reruns", f"{flaky[2]}", _short(flaky[0]) if flaky[2] else "no rerun passed"),
            ]
    gap = 12
    tw = (W - 2 * pad - 3 * gap) / 4
    for i, (k, v, sub) in enumerate(tiles):
        x = pad + (i % 4) * (tw + gap)
        y = 52 + (i // 4) * 86
        out.append(f'<rect class="tile" x="{x:.1f}" y="{y}" width="{tw:.1f}" height="74" rx="8"/>')
        out.append(f'<text class="k" x="{x + 14:.1f}" y="{y + 21}">{k}</text>')
        out.append(f'<text class="v" x="{x + 14:.1f}" y="{y + 49}">{v}</text>')
        out.append(f'<text class="sub" x="{x + 14:.1f}" y="{y + 65}">{escape(sub)}</text>')
    tiles_bottom = 52 + ((len(tiles) - 1) // 4) * 86 + 74

    left, right = pad + 40, W - pad
    slot = (right - left) / len(rows)
    bw = max(2.0, slot - 2)  # 2px surface gap between adjacent bars

    def axes(top: int, h: int, vmax: int, title: str) -> None:
        out.append(f'<text class="h2" x="{pad}" y="{top - 14}">{title}</text>')
        for i in range(5):
            y = top + h - h * i / 4
            out.append(f'<line class="{"base" if i == 0 else "grid"}" x1="{left}" x2="{right}" y1="{y:.1f}" y2="{y:.1f}"/>')
            out.append(f'<text class="tick" x="{left - 8}" y="{y + 3.5:.1f}" text-anchor="end">{int(vmax * i / 4):,}</text>')
        for idx, (d, _) in enumerate(rows):
            if (len(rows) - 1 - idx) % 7 == 0:
                cx = left + idx * slot + slot / 2
                out.append(f'<text class="tick" x="{cx:.1f}" y="{top + h + 16}" text-anchor="middle">{_fmt_day(d)}</text>')

    def legend(y: int, items: list[tuple[str, str]]) -> None:
        lx = right
        for label, cls in reversed(items):
            lx -= 7 * len(label) + 16
            out.append(f'<rect class="{cls}" x="{lx:.1f}" y="{y - 9}" width="10" height="10" rx="2"/>')
            out.append(f'<text class="lbl" x="{lx + 15:.1f}" y="{y}">{label}</text>')
            lx -= 6

    def stacked(x: float, base: float, h: int, vmax: int, segs: list[tuple[str, int]], partial: bool, tip: str) -> str:
        segs = [(c, n) for c, n in segs if n > 0]
        g = ['<g class="partial">' if partial else "<g>", f"<title>{escape(tip)}</title>"]
        y = base
        for j, (c, n) in enumerate(segs):
            sh = h * n / vmax
            seg_h = sh - (2 if j < len(segs) - 1 and sh > 3 else 0)  # 2px surface gap between segments
            if j == len(segs) - 1:
                g.append(f'<path class="{c}" d="{_bar_path(x, y - sh, bw, seg_h, 4)}"/>')
            else:
                g.append(f'<rect class="{c}" x="{x:.1f}" y="{y - seg_h:.1f}" width="{bw:.1f}" height="{max(seg_h, 0.5):.1f}"/>')
            y -= sh
        g.append("</g>")
        return "".join(g)

    # chart 1: merged PRs per day, split by origin when the history carries it
    top1, h1 = tiles_bottom + 44, 150
    vmax1 = _nice_max(max(merged) or 1)
    classed = any("merged_by_class" in r for _, r in rows)
    axes(top1, h1, vmax1, "Merged PRs per day" + (", by origin" if classed else ""))
    for idx, (d, r) in enumerate(rows):
        x = left + idx * slot + (slot - bw) / 2
        bc = r.get("merged_by_class")
        segs = [(c, bc[c]) for c in CLASSES] if bc else [("s", r["merged"])]
        tip = f"{_fmt_day(d)}: {r['merged']} merged" + (
            f" · {bc['agent']} agent · {bc['other']} other · {bc['automation']} automation" if bc else "") + (
            " (today, so far)" if d == today_s else "")
        out.append(stacked(x, top1 + h1, h1, vmax1, segs, d == today_s, tip))
    peak_i = max(range(len(rows)), key=lambda i: rows[i][1]["merged"])
    px = left + peak_i * slot + slot / 2
    py = top1 + h1 - h1 * rows[peak_i][1]["merged"] / vmax1
    out.append(f'<text class="lbl" x="{px:.1f}" y="{py - 6:.1f}" text-anchor="middle">{rows[peak_i][1]["merged"]}</text>')
    if classed:
        legend(top1 - 14, [("agent", "agent"), ("other", "other"), ("automation", "automation")])

    # chart 2: CI runs per day by outcome (stacked; success at the baseline)
    top2, h2 = top1 + h1 + 78, 150
    vmax2 = _nice_max(max(r["runs"]["total"] for _, r in rows) or 1)
    axes(top2, h2, vmax2, "CI runs per day, by outcome")
    for idx, (d, r) in enumerate(rows):
        x = left + idx * slot + (slot - bw) / 2
        runs = r["runs"]
        other = max(0, runs["total"] - runs["success"] - runs["failure"])
        tip = (f"{_fmt_day(d)}: {runs['total']:,} runs · {runs['success']:,} passed · {runs['failure']:,} failed"
               f" · {other:,} cancelled/skipped/other" + (" (today, so far)" if d == today_s else ""))
        out.append(stacked(x, top2 + h2, h2, vmax2, [("ok", runs["success"]), ("bad", runs["failure"]), ("na", other)],
                           d == today_s, tip))
    legend(top2 - 14, [("✓ passed", "ok"), ("✕ failed", "bad"), ("cancelled / skipped", "na")])

    bottom = top2 + h2
    # chart 3: job queue wait (sampled), with a marker on each day a CI change merged
    wrows = [(i, d, r["detail"]) for i, (d, r) in enumerate(rows) if "detail" in r]
    if wrows:
        top3, h3 = top2 + h2 + 78, 130
        vmax3 = _nice_max(max(x["wait_p90"] for _, _, x in wrows) or 1)
        axes(top3, h3, vmax3, "Job queue wait per day, minutes (sampled)")
        cx = lambda i: left + i * slot + slot / 2  # noqa: E731
        cy = lambda v: top3 + h3 - h3 * v / vmax3  # noqa: E731
        for i, d, x in wrows:
            if x["ci_changes"]:
                prs = ", ".join(f"#{c['pr']}" for c in x["ci_changes"][:8]) + (" …" if len(x["ci_changes"]) > 8 else "")
                out.append(f'<g><title>{_fmt_day(d)}: {len(x["ci_changes"])} CI change(s) merged: {escape(prs)}</title>'
                           f'<line class="mark" x1="{cx(i):.1f}" x2="{cx(i):.1f}" y1="{top3}" y2="{top3 + h3}"/>'
                           f'<path class="markd" d="M{cx(i):.1f},{top3 - 5} l4,4 l-4,4 l-4,-4 Z"/></g>')
        for key, cls, label in (("wait_median", "l1", "median"), ("wait_p90", "l2", "p90")):
            pts = " ".join(f"{cx(i):.1f},{cy(x[key]):.1f}" for i, _, x in wrows)
            out.append(f'<polyline class="{cls}" points="{pts}"/>')
            for i, d, x in wrows:
                out.append(f'<circle class="{cls}d" cx="{cx(i):.1f}" cy="{cy(x[key]):.1f}" r="4">'
                           f'<title>{_fmt_day(d)}: {label} wait {x[key]:.1f} min ({x["sampled_runs"]} runs sampled)</title></circle>')
        ly = top3 - 14
        lx = right
        for label, cls in reversed([("median", "l1d"), ("p90", "l2d"), ("◆ CI change merged", "")]):
            lx -= 7 * len(label) + (16 if cls else 0)
            if cls:
                out.append(f'<circle class="{cls}" cx="{lx + 5:.1f}" cy="{ly - 4}" r="4"/>')
            out.append(f'<text class="lbl" x="{lx + (15 if cls else 0):.1f}" y="{ly}">{label}</text>')
            lx -= 10
        bottom = top3 + h3

    H = bottom + 58
    out.append(f'<text class="meta" x="{pad}" y="{H - 14}">Source: GitHub API. Faded bar = today so far. Days are UTC. '
               f'Wait and runner time are sampled from about {cfg.sample_runs} runs a day.</text>')
    body = "\n  ".join(out)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
            f'role="img" aria-label="{escape(cfg.heading)}: merged PRs, CI runs and queue wait per day">\n'
            f'<style>{STYLE}</style>\n<g class="root">\n  <rect class="bg" x="0.5" y="0.5" width="{W - 1}" '
            f'height="{H - 1}" rx="10"/>\n  {body}\n</g>\n</svg>\n')


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
    p.add_argument("--sample-runs", type=int, default=24)
    p.add_argument("--detail-backfill", type=int, default=DETAIL_BACKFILL,
                   help="older days to fill with the sampled detail this run (each costs ~30 API calls)")
    p.add_argument("--summary", type=Path, help="append the Markdown report here (the Action passes $GITHUB_STEP_SUMMARY)")
    p.add_argument("--max-wait-p90", default="", help="alert when the 7-day p90 job queue wait (min) exceeds this")
    p.add_argument("--max-runner-hours-per-pr", default="", help="alert when 7-day runner hours per merged PR exceed this")
    p.add_argument("--fail-on-alert", action="store_true")
    p.add_argument("--render-only", action="store_true", help="redraw from metrics.json without fetching")
    a = p.parse_args(argv)
    if not a.repo:
        p.error("--repo is required (or GITHUB_REPOSITORY)")
    cfg = Config(repo=a.repo, title=a.title, bot_workflows=_csv(a.bot_workflows),
                 agent_prefixes=_csv(a.agent_branch_prefixes), agent_markers=a.agent_markers, sample_runs=a.sample_runs)
    a.out.mkdir(parents=True, exist_ok=True)
    data_file = a.out / "metrics.json"
    data = json.loads(data_file.read_text()) if data_file.exists() else {}
    today = datetime.now(timezone.utc).date()
    if not a.render_only:
        data = collect(cfg, a.days, data, today, a.detail_backfill)
        data_file.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    (a.out / "dashboard.svg").write_text(render(data, a.days, today, cfg), encoding="utf-8")
    alerts = check_alerts(data, a.days, today, _opt_float(a.max_wait_p90), _opt_float(a.max_runner_hours_per_pr))
    for msg in alerts:
        print(f"::warning title=Fleet CI report::{msg}")
    if a.summary:
        with a.summary.open("a", encoding="utf-8") as f:
            f.write(summary_md(data, cfg, a.days, today, alerts))
    print(f"wrote {a.out}/dashboard.svg and metrics.json ({len(data['days'])} days of history)", file=sys.stderr)
    return 1 if alerts and a.fail_on_alert else 0


if __name__ == "__main__":
    raise SystemExit(main())
