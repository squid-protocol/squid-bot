#!/usr/bin/env python3
"""Live fleet dashboard: real daily numbers from the GitHub API, rendered as an SVG.

Counted exactly:
  merged PRs per day      search API  (repo:R is:pr is:merged merged:DAY)
  Actions runs per day     actions/runs?created=DAY, total and per conclusion
  bot workflow runs        the same count per BOT_WORKFLOWS workflow (the shepherd's own bookkeeping)
  CI changes merged        PR merges to main that touched .github/workflows, shown as markers
  open PRs right now       search API  (repo:R is:pr is:open)
Sampled (SAMPLE_RUNS runs a day, spread over the day; labelled "est." where it is an estimate):
  job queue wait           job created -> picked up by a runner, median and p90
  runner minutes           mean job minutes per run x the day's runs

History accumulates in <out>/metrics.json: a finished day is fetched once, the last
REFRESH_DAYS days (still moving) are re-fetched on every run. Needs `gh` (authenticated,
or GH_TOKEN set). The scheduled workflow .github/workflows/dashboard.yml runs this and
commits the output to the `dashboard` branch.

Usage:
    python scripts/dashboard.py [--repo owner/repo] [--days 30] [--out dashboard]
    python scripts/dashboard.py --render-only --out dashboard   # redraw from metrics.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

DEFAULT_REPO = os.environ.get("DASHBOARD_REPO", "squid-protocol/gitgalaxy")
REFRESH_DAYS = 3  # today, yesterday and the day before can still change
OUTCOMES = ("success", "failure", "cancelled", "skipped")
SEARCH_PAUSE = 2.2  # search API: 30 requests a minute
BOT_WORKFLOWS = [w.strip() for w in os.environ.get(
    "DASHBOARD_BOT_WORKFLOWS", "CI shepherd (reconcile),CI shepherd (label events),Main Watch").split(",") if w.strip()]
SAMPLE_RUNS = 24  # 6 per 6-hour window
DETAIL_BACKFILL = 4  # older days missing the sampled detail, filled a few per refresh (API budget)


def gh_json(path: str):
    out = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github+json", path],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def gh_count(path: str) -> int:
    return int(gh_json(path)["total_count"])


def _iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0.0


def bot_workflow_ids(repo: str) -> dict[str, int]:
    wfs = gh_json(f"repos/{repo}/actions/workflows?per_page=100")["workflows"]
    return {w["name"]: w["id"] for w in wfs if w["name"] in BOT_WORKFLOWS}


def fetch_detail(repo: str, day: str, total_runs: int, bots: dict[str, int]) -> dict:
    """Bot-run count (exact), CI-change PRs (exact), job queue wait and runner minutes (sampled)."""
    bot_runs = sum(gh_count(f"repos/{repo}/actions/workflows/{i}/runs?created={day}&per_page=1") for i in bots.values())
    changes: dict[int, str] = {}
    for c in gh_json(f"repos/{repo}/commits?path=.github/workflows&since={day}T00:00:00Z&until={day}T23:59:59Z&per_page=100"):
        title = c["commit"]["message"].splitlines()[0]
        m = re.search(r"\(#(\d+)\)\s*$", title)
        if m:  # squash merges of PRs; direct bot commits carry no PR number
            changes[int(m.group(1))] = title
    waits: list[float] = []
    job_min: list[float] = []
    per_window = SAMPLE_RUNS // 4
    for h in (0, 6, 12, 18):
        page = gh_json(f"repos/{repo}/actions/runs?created={day}T{h:02d}:00:00Z..{day}T{h + 5:02d}:59:59Z&per_page=100")
        runs = [r for r in page["workflow_runs"] if r.get("conclusion") != "skipped" and r.get("status") == "completed"]
        step = max(1, len(runs) // per_window)
        for r in runs[::step][:per_window]:
            minutes = 0.0
            for j in gh_json(f"repos/{repo}/actions/runs/{r['id']}/jobs?per_page=100")["jobs"]:
                if j.get("started_at") and j.get("completed_at") and j.get("conclusion") != "skipped":
                    m = (_iso(j["completed_at"]) - _iso(j["started_at"])).total_seconds() / 60
                    if m > 0.05:  # a job that never really ran held no runner
                        minutes += m
                        waits.append(max(0.0, (_iso(j["started_at"]) - _iso(j["created_at"])).total_seconds() / 60))
            job_min.append(minutes)
    return {
        "bot_runs": bot_runs,
        "ci_changes": [{"pr": n, "title": t} for n, t in sorted(changes.items())],
        "wait_median": round(_pct(waits, 0.5), 2),
        "wait_p90": round(_pct(waits, 0.9), 2),
        "runner_min_est": round(sum(job_min) / len(job_min) * total_runs) if job_min else None,
        "sampled_runs": len(job_min),
    }


def fetch_day(repo: str, day: str) -> dict:
    merged = gh_count(f"search/issues?q=repo:{repo}+is:pr+is:merged+merged:{day}&per_page=1")
    time.sleep(SEARCH_PAUSE)
    # total_count tops out at 2,500 per query, a busy day passes that: count it in 6-hour windows
    runs = {"total": sum(
        gh_count(f"repos/{repo}/actions/runs?created={day}T{h:02d}:00:00Z..{day}T{h + 5:02d}:59:59Z&per_page=1")
        for h in (0, 6, 12, 18)
    )}
    for outcome in OUTCOMES:
        runs[outcome] = gh_count(f"repos/{repo}/actions/runs?created={day}&status={outcome}&per_page=1")
    return {"merged": merged, "runs": runs}


def days_to_fetch(have: dict, today: date, days: int) -> list[str]:
    window = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    fresh = {(today - timedelta(days=i)).isoformat() for i in range(REFRESH_DAYS)}
    return [d for d in window if d not in have or d in fresh]


def collect(repo: str, days: int, data: dict, today: date, backfill: int = DETAIL_BACKFILL) -> dict:
    if data.get("repo") != repo:
        data = {"repo": repo, "days": {}}
    fresh = days_to_fetch(data["days"], today, days)
    for d in fresh:
        data["days"][d] = fetch_day(repo, d)
    bots = bot_workflow_ids(repo)
    window = [(today - timedelta(days=i)).isoformat() for i in range(days)]
    missing = [d for d in window if d not in fresh and "detail" not in data["days"].get(d, {"detail": 1})]
    for d in fresh + missing[:backfill]:
        r = data["days"][d]["runs"]
        data["days"][d]["detail"] = fetch_detail(repo, d, r["total"] - r["skipped"], bots)
    data["open_prs"] = gh_count(f"search/issues?q=repo:{repo}+is:pr+is:open&per_page=1")
    data["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    return data


# ---------------------------------------------------------------- rendering

STYLE = """
  .root { --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781; --grid:#e1e0d9;
          --axis:#c3c2b7; --border:rgba(11,11,11,0.10); --tile:#f4f3f0; --series:#2a78d6;
          --pass:#2a78d6; --fail:#eb6834; --neutral:#c3c2b7; }
  @media (prefers-color-scheme: dark) {
    .root { --surface:#1a1a19; --ink:#f0efec; --ink2:#c3c2b7; --muted:#898781; --grid:#2c2c2a;
            --axis:#383835; --border:rgba(255,255,255,0.10); --tile:#232322; --series:#3987e5;
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
  .s { fill: var(--series); } .ok { fill: var(--pass); } .bad { fill: var(--fail); } .na { fill: var(--neutral); }
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


def _fmt_day(d: str) -> str:
    return date.fromisoformat(d).strftime("%b %-d")


def render(data: dict, days: int, today: date) -> str:
    window = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    rows = [(d, data["days"][d]) for d in window if d in data["days"]]
    if not rows:
        raise SystemExit("no data to render")
    today_s = today.isoformat()
    W, pad = 880, 24
    out: list[str] = []

    # header
    repo = escape(data["repo"])
    out.append(f'<text class="h1" x="{pad}" y="34">{repo}: agent fleet activity</text>')
    out.append(f'<text class="meta" x="{W - pad}" y="34" text-anchor="end">last {len(rows)} days · '
               f'updated {escape(data["updated"].replace("T", " ").replace("Z", " UTC"))}</text>')

    # stat tiles
    merged = [r["merged"] for _, r in rows]
    done = [r for d, r in rows if d != today_s] or [r for _, r in rows]
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
    # second row: CI cost and congestion, last 7 finished days that carry the sampled detail
    det7 = [r for r in done if "detail" in r][-7:]
    if det7:
        dt = [r["detail"] for r in det7]
        n7 = len(det7)
        est = [(r["detail"]["runner_min_est"], r["merged"]) for r in det7 if r["detail"].get("runner_min_est") is not None]
        per_pr = sum(m for m, _ in est) / max(1, sum(n for _, n in est)) / 60 if est else None
        bots = sum(x["bot_runs"] for x in dt)
        runs7 = sum(r["runs"]["total"] for r in det7)
        changes = sum(len(x["ci_changes"]) for x in dt)
        tiles += [
            ("Job queue wait (p90)", f"{sum(x['wait_p90'] for x in dt) / n7:.1f} min",
             f"median {sum(x['wait_median'] for x in dt) / n7:.1f} min · sampled, last {n7} days"),
            ("Runner time per merged PR", f"{per_pr:.1f} h" if per_pr is not None else "–", f"est. from sampled jobs · last {n7} days"),
            ("Bot workflow runs", f"{100 * bots / max(1, runs7):.0f}%", f"{bots / n7:,.0f}/day · shepherd, Main Watch"),
            ("CI changes merged", f"{changes}", f"last {n7} days · ◆ on the wait chart"),
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
    plot_w = right - left
    slot = plot_w / len(rows)
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

    # chart 1: merged PRs per day
    top1, h1 = tiles_bottom + 44, 150
    vmax1 = _nice_max(max(merged) or 1)
    axes(top1, h1, vmax1, "Merged PRs per day")
    for idx, (d, r) in enumerate(rows):
        x = left + idx * slot + (slot - bw) / 2
        bh = h1 * r["merged"] / vmax1
        cls = "s partial" if d == today_s else "s"
        tip = f"{_fmt_day(d)}: {r['merged']} merged" + (" (today, so far)" if d == today_s else "")
        if bh > 0:
            out.append(f'<path class="{cls}" d="{_bar_path(x, top1 + h1 - bh, bw, bh, 4)}"><title>{tip}</title></path>')
    peak_i = max(range(len(rows)), key=lambda i: rows[i][1]["merged"])
    px = left + peak_i * slot + slot / 2
    py = top1 + h1 - h1 * rows[peak_i][1]["merged"] / vmax1
    out.append(f'<text class="lbl" x="{px:.1f}" y="{py - 6:.1f}" text-anchor="middle">{rows[peak_i][1]["merged"]}</text>')

    # chart 2: CI runs per day by outcome (stacked; success at the baseline)
    top2, h2 = top1 + h1 + 78, 150
    vmax2 = _nice_max(max(r["runs"]["total"] for _, r in rows) or 1)
    axes(top2, h2, vmax2, "CI runs per day, by outcome")
    for idx, (d, r) in enumerate(rows):
        x = left + idx * slot + (slot - bw) / 2
        runs = r["runs"]
        other = max(0, runs["total"] - runs["success"] - runs["failure"])
        segs = [("ok", runs["success"]), ("bad", runs["failure"]), ("na", other)]
        segs = [(c, n) for c, n in segs if n > 0]
        tip = (f"{_fmt_day(d)}: {runs['total']:,} runs · {runs['success']:,} passed · {runs['failure']:,} failed"
               f" · {other:,} cancelled/skipped/other" + (" (today, so far)" if d == today_s else ""))
        y = top2 + h2
        g = ['<g class="partial">' if d == today_s else "<g>", f"<title>{tip}</title>"]
        for j, (c, n) in enumerate(segs):
            sh = h2 * n / vmax2
            seg_h = sh - (2 if j < len(segs) - 1 and sh > 3 else 0)  # 2px surface gap between segments
            if j == len(segs) - 1:
                g.append(f'<path class="{c}" d="{_bar_path(x, y - sh, bw, seg_h, 4)}"/>')
            else:
                g.append(f'<rect class="{c}" x="{x:.1f}" y="{y - seg_h:.1f}" width="{bw:.1f}" height="{max(seg_h, 0.5):.1f}"/>')
            y -= sh
        g.append("</g>")
        out.append("".join(g))
    # legend: every outcome color carries a symbol and a label
    ly = top2 - 14
    lx = right
    for label, cls in reversed([("✓ passed", "ok"), ("✕ failed", "bad"), ("cancelled / skipped", "na")]):
        tw_ = 7 * len(label) + 16
        lx -= tw_
        out.append(f'<rect class="{cls}" x="{lx:.1f}" y="{ly - 9}" width="10" height="10" rx="2"/>')
        out.append(f'<text class="lbl" x="{lx + 15:.1f}" y="{ly}">{label}</text>')
        lx -= 6

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
    out.append(f'<text class="meta" x="{pad}" y="{H - 14}">Source: GitHub API, refreshed every 6 hours. '
               f'Faded bar = today so far. Days are UTC. Wait and runner time are sampled from about 24 runs a day.</text>')
    body = "\n  ".join(out)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
            f'role="img" aria-label="{repo} merged PRs and CI runs per day">\n'
            f'<style>{STYLE}</style>\n<g class="root">\n  <rect class="bg" x="0.5" y="0.5" width="{W - 1}" '
            f'height="{H - 1}" rx="10"/>\n  {body}\n</g>\n</svg>\n')


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", default=DEFAULT_REPO)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--out", type=Path, default=Path("dashboard"))
    p.add_argument("--detail-backfill", type=int, default=DETAIL_BACKFILL,
                   help="older days to fill with the sampled detail this run (each costs ~30 API calls)")
    p.add_argument("--render-only", action="store_true", help="redraw from metrics.json without fetching")
    a = p.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    data_file = a.out / "metrics.json"
    data = json.loads(data_file.read_text()) if data_file.exists() else {}
    today = datetime.now(timezone.utc).date()
    if not a.render_only:
        data = collect(a.repo, a.days, data, today, a.detail_backfill)
        data_file.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    (a.out / "dashboard.svg").write_text(render(data, a.days, today), encoding="utf-8")
    print(f"wrote {a.out}/dashboard.svg and metrics.json ({len(data['days'])} days of history)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
