#!/usr/bin/env python3
"""Exact runner time from every job's own start and end, for days the dashboard only estimated.

Why (2026-10-31): occupancy readings before that day left out jobs running inside runs GitHub still lists as `queued`
(read 14.75 jobs on average while 19.25 ran), and days before the readings only have a 5% stratified run sample
(about +-15%). Every job's start and end stay readable, so a day's runner time can be measured instead of estimated.

Cost: run listings are REST (one call per 100 runs, a window split while it reaches GitHub's 1,000-run listing cap),
held to --rest-per-hour and never below --rest-floor remaining, so an orchestrator sharing the token keeps its budget.
Jobs come from GraphQL, 50 runs a query (WorkflowRun.checkSuite.checkRuns: a check run's id and times are the job's,
every attempt included), about 1 point each, so measuring a 10,000-run day costs ~200 points, not 10,000 REST calls.

Resumable: every fetched window and batch is appended under --state; a restart skips what is there.

    python3 backfill_exact_runner_time.py fetch --owner squid-protocol --since 2026-09-14 --until 2026-10-31T12:30:00Z
    python3 backfill_exact_runner_time.py cancelled
    python3 backfill_exact_runner_time.py summarize --pool 20 [--readings occupancy.jsonl ...] [--metrics metrics.json]
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

LIST_CAP = 1000
MIN_WINDOW = timedelta(minutes=5)
BATCH = 50
QUERY = """query($ids:[ID!]!) { rateLimit { cost remaining resetAt }
  nodes(ids:$ids) { ... on WorkflowRun { id databaseId checkSuite { checkRuns(first:100, filterBy:{checkType:ALL}) {
    totalCount pageInfo { hasNextPage endCursor }
    nodes { databaseId name startedAt completedAt conclusion } } } } } }"""
MORE = """query($id:ID!, $after:String) { rateLimit { cost remaining resetAt }
  node(id:$id) { ... on WorkflowRun { checkSuite { checkRuns(first:100, after:$after, filterBy:{checkType:ALL}) {
    pageInfo { hasNextPage endCursor } nodes { databaseId name startedAt completedAt conclusion } } } } } }"""


def iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def zfmt(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(*a) -> None:
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


class Budget:
    """Paces REST to rest_per_hour and keeps rest_floor calls (and gql_floor points) for whoever shares the token."""

    def __init__(self, rest_per_hour: int, rest_floor: int, gql_floor: int):
        self.gap = 3600 / rest_per_hour
        self.floor, self.gql_floor = rest_floor, gql_floor
        self.last = 0.0
        self.calls = 0

    def rest(self) -> None:
        wait = self.last + self.gap - time.time()
        if wait > 0:
            time.sleep(wait)
        if self.calls % 25 == 0:
            core = json.loads(gh(["api", "rate_limit"]))["resources"]["core"]
            if core["remaining"] < self.floor:
                pause = max(30, core["reset"] - time.time() + 5)
                log(f"REST remaining {core['remaining']} < floor {self.floor}: sleeping {pause / 60:.0f} min")
                time.sleep(pause)
        self.last = time.time()
        self.calls += 1

    def graphql(self, rl: dict | None) -> None:
        if rl and rl["remaining"] < self.gql_floor:
            pause = max(30, (iso(rl["resetAt"]) - datetime.now(timezone.utc)).total_seconds() + 5)
            log(f"GraphQL remaining {rl['remaining']} < floor {self.gql_floor}: sleeping {pause / 60:.0f} min")
            time.sleep(pause)


def gh(args: list[str], tries: int = 6) -> str:
    for i in range(tries):
        p = subprocess.run(["gh", *args], capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout
        err = p.stderr.strip()
        if "Not Found" in err:
            raise LookupError(err)
        wait = 60 * (i + 1) if ("rate limit" in err.lower() or "secondary" in err.lower()) else 5 * (i + 1)
        log(f"gh failed ({err[:160]}); retry in {wait}s")
        time.sleep(wait)
    raise RuntimeError(f"gh {' '.join(args)[:120]} failed {tries} times")


def owner_repos(owner: str, b: Budget) -> list[str]:
    for base in (f"orgs/{owner}/repos", f"users/{owner}/repos"):
        try:
            b.rest()
            got = json.loads(gh(["api", f"{base}?per_page=100&type=all"], tries=2))
            return [r["full_name"] for r in got if not r.get("archived")]
        except (LookupError, RuntimeError):
            continue
    return []


def list_window(repo: str, t0: datetime, t1: datetime, b: Budget):
    """Every run created in [t0, t1), splitting the window while a listing would hit the cap. Yields (t0, t1, runs)
    per leaf window so each can be checkpointed."""
    todo = [(t0, t1)]
    while todo:
        a, z = todo.pop(0)
        span = f"{zfmt(a)}..{zfmt(z - timedelta(seconds=1))}"
        b.rest()
        first = json.loads(gh(["api", f"repos/{repo}/actions/runs?created={span}&per_page=100&page=1"]))
        if first.get("total_count", 0) >= LIST_CAP and z - a > MIN_WINDOW:
            mid = a + (z - a) / 2
            todo[:0] = [(a, mid), (mid, z)]
            continue
        runs, page, got = list(first["workflow_runs"]), 1, first["workflow_runs"]
        while len(got) == 100 and page < LIST_CAP // 100:
            page += 1
            b.rest()
            got = json.loads(gh(["api", f"repos/{repo}/actions/runs?created={span}&per_page=100&page={page}"]))[
                "workflow_runs"]
            runs += got
        yield a, z, [{"id": r["id"], "node": r["node_id"], "name": r.get("name"), "created": r["created_at"],
                      "status": r.get("status"), "conclusion": r.get("conclusion"), "attempt": r.get("run_attempt")}
                     for r in runs]


def graphql(query: str, b: Budget, **vars) -> dict:
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in vars.items():
        args += ["-F" if not isinstance(v, str) else "-f", f"{k}={v}"] if not isinstance(v, list) else \
            [x for i in v for x in ("-f", f"{k}[]={i}")]
    out = json.loads(gh(args))
    if out.get("errors") and not out.get("data"):
        raise RuntimeError(str(out["errors"])[:300])
    b.graphql(out["data"].get("rateLimit"))
    return out["data"]


def jobs_of(runs: list[dict], b: Budget) -> list[dict]:
    out = []
    by_node = {r["node"]: r for r in runs}
    data = graphql(QUERY, b, ids=[r["node"] for r in runs])
    for n in data["nodes"]:
        if not n or not n.get("checkSuite"):
            continue
        cr = n["checkSuite"]["checkRuns"]
        nodes, page = list(cr["nodes"]), cr["pageInfo"]
        while page["hasNextPage"]:
            more = graphql(MORE, b, id=n["id"], after=page["endCursor"])["node"]["checkSuite"]["checkRuns"]
            nodes += more["nodes"]
            page = more["pageInfo"]
        run_id = by_node.get(n["id"], {}).get("id", n.get("databaseId"))
        out += [{"run": run_id, "job": c["databaseId"], "name": c["name"], "start": c["startedAt"],
                 "end": c["completedAt"], "conclusion": c["conclusion"]} for c in nodes]
    return out


def fetch(a) -> int:
    st = Path(a.state).expanduser()
    st.mkdir(parents=True, exist_ok=True)
    b = Budget(a.rest_per_hour, a.rest_floor, a.gql_floor)
    since = iso(a.since + ("T00:00:00Z" if "T" not in a.since else ""))
    until = iso(a.until)
    repos = a.repos.split(",") if a.repos else owner_repos(a.owner, b)
    log(f"{len(repos)} repos, {zfmt(since)} .. {zfmt(until)}")
    windows_f, jobs_f = st / "windows.jsonl", st / "jobs.jsonl"
    done = {(w["repo"], w["day"]) for w in map(json.loads, windows_f.open())} if windows_f.exists() else set()
    day = since
    while day < until:
        nxt = min(day + timedelta(days=1), until)
        for repo in repos:
            key = (repo, zfmt(day))
            if key in done:
                continue
            n_runs = n_jobs = 0
            for w0, w1, runs in list_window(repo, day, nxt, b):
                jobs = []
                for i in range(0, len(runs), BATCH):
                    jobs += jobs_of(runs[i:i + BATCH], b)
                with jobs_f.open("a") as f:
                    f.writelines(json.dumps({"repo": repo, **j}) + "\n" for j in jobs)
                n_runs += len(runs)
                n_jobs += len(jobs)
            with windows_f.open("a") as f:  # the day is done for this repo only once all its jobs are written
                f.write(json.dumps({"repo": repo, "day": zfmt(day), "until": zfmt(nxt), "runs": n_runs,
                                    "jobs": n_jobs, "at": zfmt(datetime.now(timezone.utc))}) + "\n")
            if n_runs:
                log(f"{repo} {zfmt(day)[:10]}: {n_runs} runs, {n_jobs} jobs (REST calls so far {b.calls})")
        day = nxt
    log("fetch complete")
    return 0


def run_node_id(repo_id: int, run_id: int) -> str:
    """A WorkflowRun's GraphQL node id: "WFR_" + base64url(msgpack([2, repo id, run id])), unpadded (checked
    2026-10-31 against the REST node_id; callers check the databaseId that comes back)."""
    def uint(n: int) -> bytes:
        return bytes([n]) if n < 128 else (b"\xce" + n.to_bytes(4, "big") if n < 2 ** 32 else b"\xcf" + n.to_bytes(8, "big"))
    return "WFR_" + base64.urlsafe_b64encode(b"\x93\x02" + uint(repo_id) + uint(run_id)).decode().rstrip("=")


STEPS = """query($ids:[ID!]!) { rateLimit { cost remaining resetAt }
  nodes(ids:$ids) { ... on WorkflowRun { databaseId checkSuite { checkRuns(first:100, filterBy:{checkType:ALL}) {
    nodes { databaseId conclusion steps(first:1) { totalCount } } } } } } }"""


# A job that succeeded ran on a runner. Any other job may never have got one: cancelled while it waited (2026-10-05:
# 54 of 60 at the peak minute) or failed while it waited (2026-10-07: 28 "compile" jobs failing for 8 minutes with
# runner_id 0 and no steps). Their step count decides.
RAN = {"SUCCESS", "SKIPPED"}


def cancelled(a) -> int:
    """A job cancelled while it waited for a runner still gets a start (its creation) and an end (the cancel): on
    2026-10-05 54 of the 60 jobs "running" at the peak minute were such. GitHub's REST job has no runner_id for them;
    GraphQL has no runner, but such a job ran no step (40 checked against REST 2026-10-31: 19 without a runner, all 0
    steps; 21 with one, all with steps). Records each cancelled job's step count in cancelled_steps.jsonl. Small
    batches: steps on 50 runs a query gets a 502."""
    st = Path(a.state).expanduser()
    b = Budget(900, 1500, a.gql_floor)
    out_f = st / "cancelled_steps.jsonl"
    have = {json.loads(x)["job"] for x in out_f.open()} if out_f.exists() else set()
    runs, nodes = defaultdict(set), {}
    for line in (st / "jobs.jsonl").open():
        j = json.loads(line)
        if j["conclusion"] not in RAN and j["job"] not in have:
            runs[(j["repo"], j["run"])].add(j["job"])
    log(f"{sum(map(len, runs.values()))} cancelled jobs in {len(runs)} runs to check")
    repo_ids = {}
    for repo in {r for r, _ in runs}:
        b.rest()
        repo_ids[repo] = json.loads(gh(["api", f"repos/{repo}"]))["id"]
    nodes = {(repo, run): run_node_id(repo_ids[repo], run) for repo, run in runs}
    keys = list(runs)
    for i in range(0, len(keys), a.batch):
        chunk = keys[i:i + a.batch]
        data = graphql(STEPS, b, ids=[nodes[k] for k in chunk])
        rows = []
        for k, n in zip(chunk, data["nodes"]):
            if n and n.get("databaseId") != k[1]:  # a node id built wrong: never take another run's jobs
                raise RuntimeError(f"node id for run {k} came back as run {n.get('databaseId')}")
            for c in ((n or {}).get("checkSuite") or {}).get("checkRuns", {}).get("nodes", []):
                if c["conclusion"] not in RAN:
                    rows.append({"job": c["databaseId"], "steps": c["steps"]["totalCount"]})
        with out_f.open("a") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
        if i % (a.batch * 50) == 0:
            log(f"steps {i}/{len(keys)} runs")
    log("cancelled complete")
    return 0


def summarize(a) -> int:
    """Per UTC day: exact runner minutes (whole pool and per repo), peak and minutes at the pool's limit, against the
    stored estimate and against what the old readings said at their own times."""
    st = Path(a.state).expanduser()
    seen, per_min = set(), defaultdict(lambda: defaultdict(int))  # minute -> repo -> jobs on a runner
    steps_f = st / "cancelled_steps.jsonl"
    steps = {r["job"]: r["steps"] for r in map(json.loads, steps_f.open())} if steps_f.exists() else {}
    unknown = dropped = 0
    for line in (st / "jobs.jsonl").open():
        j = json.loads(line)
        # A re-run of failed jobs carries the run's passed jobs into the new attempt as new check runs with the old start
        # and end (2026-10-21: one "db2 2/4" three times, to the second): one job per run, name, start and end.
        once = (j["repo"], j["run"], j.get("name"), j["start"], j["end"])
        if j["job"] in seen or once in seen or not j["start"] or not j["end"] or j["conclusion"] == "SKIPPED":
            continue
        seen.update((j["job"], once))
        if j["conclusion"] not in RAN:  # cancelled or failed while waiting: no step, no runner (see `cancelled`)
            if j["job"] not in steps:
                unknown += 1
            elif not steps[j["job"]]:
                dropped += 1
                continue
        s, e = iso(j["start"]), iso(j["end"])
        if (e - s).total_seconds() < 3:  # never really held a runner (same cut as measure_run's 0.05 min)
            continue
        # count the job in each minute it overlapped, weighted by the overlap
        t = s.replace(second=0)
        while t < e:
            t2 = t + timedelta(minutes=1)
            per_min[t][j["repo"]] += (min(e, t2) - max(s, t)).total_seconds() / 60
            t = t2
    print(f"{dropped} cancelled jobs never held a runner, left out; {unknown} cancelled jobs not checked "
          f"(counted: run `cancelled` first)", file=sys.stderr)
    windows = [json.loads(x) for x in (st / "windows.jsonl").open()]
    days_done = defaultdict(set)
    for w in windows:
        days_done[w["day"][:10]].add(w["repo"])
    repos_all = {w["repo"] for w in windows}
    est = {}
    if a.metrics:
        est = {d: r.get("detail", {}) for d, r in json.load(open(a.metrics)).get("days", {}).items()}
    days = {}
    for t, by in per_min.items():
        d = days.setdefault(t.strftime("%Y-%m-%d"), {"org_min": 0.0, "by_repo": defaultdict(float), "minutes": {}})
        tot = sum(by.values())
        d["org_min"] += tot
        d["minutes"][t.strftime("%H:%M")] = round(tot, 2)
        for r, v in by.items():
            d["by_repo"][r] += v
    out, first = {}, min(days_done, default="")
    jobs_per_day = defaultdict(int)
    for line in (st / "jobs.jsonl").open():
        j = json.loads(line)
        if j.get("start"):
            jobs_per_day[j["start"][:10]] += 1
    for day in sorted(days):
        # a day not fully fetched for every repo is not measured, nor the first (runs from the day before spill into it)
        if days_done.get(day) != repos_all or day == first:
            continue
        d = days[day]
        mins = list(d["minutes"].values())
        det = est.get(day, {})
        out[day] = {
            "runner_min": round(d["org_min"]), "jobs": jobs_per_day[day],
            "by_repo": {r: round(v) for r, v in sorted(d["by_repo"].items(), key=lambda x: -x[1])},
            "peak": round(max(mins, default=0), 1),
            "min_at_pool": sum(m >= a.pool - 0.5 for m in mins) if a.pool else None,
            "util": round(d["org_min"] / (a.pool * 1440), 3) if a.pool else None,
            "estimate_min": det.get("runner_min_est"), "estimate_ci": det.get("runner_min_ci"),
        }
        print(f"{day}  exact {d['org_min'] / 60:6.0f} h  ({out[day]['util'] or 0:.0%} of pool)  peak {out[day]['peak']:5.1f}"
              f"  at-pool {out[day]['min_at_pool'] or 0:4d} min  estimate(gitgalaxy) "
              f"{(det.get('runner_min_est') or 0) / 60:6.0f} h  exact(gitgalaxy) "
              f"{d['by_repo'].get('squid-protocol/gitgalaxy', 0) / 60:6.0f} h")
    # the old readings against the exact count at their own minute
    for path in a.readings or []:
        diffs = []
        for line in open(path):
            r = json.loads(line)
            t = iso(r["t"]).replace(second=0)
            if t.strftime("%Y-%m-%d") not in out or "running_in_queued_runs" in r:
                continue
            exact = sum(per_min.get(t, {}).values()) if r.get("scope") == "org" else \
                per_min.get(t, {}).get(a.repo, 0)
            diffs.append((r["running"], exact))
        if diffs:
            n = len(diffs)
            print(f"{path}: {n} readings, read {sum(x for x, _ in diffs) / n:.2f} jobs on average, "
                  f"exact {sum(y for _, y in diffs) / n:.2f}")
    # runner_exact.json, as fleet_report.py reads it (EXACT_FILE): per-minute series only from --minutes-since, the days
    # whose occupancy readings they correct
    def series(day: str, repo: str | None) -> list[float]:
        t0 = iso(f"{day}T00:00:00Z")
        return [round(sum(by.values()) if repo is None else by.get(repo, 0), 1)
                for by in (per_min.get(t0 + timedelta(minutes=i), {}) for i in range(1440))]
    doc = {"repo": a.repo, "pool": a.pool, "method": "job-times-v1",
           "made": zfmt(datetime.now(timezone.utc)), "days": out,
           "minutes": {d: {"org": series(d, None), "repo": series(d, a.repo)} for d in out if d >= a.minutes_since}}
    target = Path(a.write) if a.write else st / "runner_exact.json"
    target.write_text(json.dumps(doc, separators=(",", ":"), sort_keys=True) + "\n")
    print(f"wrote {target} ({target.stat().st_size / 1e3:.0f} kB, {len(out)} days)", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", default="~/.local/state/squid-exact-backfill")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--owner", default="squid-protocol")
    f.add_argument("--repos", default="", help="comma list; default every repo of --owner")
    f.add_argument("--since", required=True)
    f.add_argument("--until", required=True)
    f.add_argument("--rest-per-hour", type=int, default=900)
    f.add_argument("--rest-floor", type=int, default=1500)
    f.add_argument("--gql-floor", type=int, default=1500)
    c = sub.add_parser("cancelled", help="step counts of cancelled jobs: the ones with none never held a runner")
    c.add_argument("--batch", type=int, default=5)
    c.add_argument("--gql-floor", type=int, default=1500)
    s = sub.add_parser("summarize")
    s.add_argument("--pool", type=int, default=20)
    s.add_argument("--repo", default="squid-protocol/gitgalaxy")
    s.add_argument("--metrics")
    s.add_argument("--readings", nargs="*")
    s.add_argument("--minutes-since", default="2026-10-24", help="keep per-minute counts from this day (readings era)")
    s.add_argument("--write", help="where to write runner_exact.json (default: in --state)")
    a = p.parse_args()
    return {"fetch": fetch, "cancelled": cancelled, "summarize": summarize}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
