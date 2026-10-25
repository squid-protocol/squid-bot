#!/usr/bin/env bash
# occupancy-sampler.sh
# One runner-occupancy reading (jobs holding a hosted runner right now, across every repository of the owner) taken
# from this host, appended to occupancy.jsonl on the dashboard branch. Run it every 15 min from a systemd user timer
# (templates/systemd/). Why a host and not a scheduled workflow (2026-10-25): the */15 workflow ran 7 times in 22 h
# (GitHub drops scheduled runs under load), a run waited up to 28 min for a runner from the very pool it measures (so
# readings skipped the saturated hours) and each reading counted its own job. A host reads off the pool, on time.
# Readings are kept locally and pushed at most once per SQUID_OCC_PUSH_MIN (60), merged with the branch's by time, so
# a missed push or another writer loses nothing. Needs git, python3 and gh logged in (fleet_report.py calls gh api).
#   SQUID_OCC_REPO       the report's repo; its owner is the pool counted   (squid-protocol/gitgalaxy)
#   SQUID_OCC_DASHBOARD  the repo whose branch holds occupancy.jsonl        (squid-protocol/squid-bot)
#   SQUID_OCC_BRANCH     that branch                                         (dashboard)
#   SQUID_OCC_STATE      local state: clone, readings, last push             (~/.local/state/squid-occupancy)
#   SQUID_OCC_REMOTE     where to clone and push (tests point it at a local copy)  (https://github.com/$SQUID_OCC_DASHBOARD.git)
set -euo pipefail
repo=${SQUID_OCC_REPO:-squid-protocol/gitgalaxy}
dash=${SQUID_OCC_DASHBOARD:-squid-protocol/squid-bot}
branch=${SQUID_OCC_BRANCH:-dashboard}
state=${SQUID_OCC_STATE:-$HOME/.local/state/squid-occupancy}
push_min=${SQUID_OCC_PUSH_MIN:-60}
remote=${SQUID_OCC_REMOTE:-https://github.com/$dash.git}
mkdir -p "$state/readings"
exec 9>"$state/lock"
flock -n 9 || { echo "occupancy-sampler: another reading is running" >&2; exit 0; }

# the clone tracks the action's code on main and the dashboard branch; the reader is always main's
if [ ! -d "$state/repo/.git" ]; then
  git clone -q --depth=1 --no-single-branch "$remote" "$state/repo"
fi
git -C "$state/repo" fetch -q --depth=1 origin main "$branch"
git -C "$state/repo" show origin/main:actions/fleet-report/fleet_report.py > "$state/fleet_report.py"
python3 "$state/fleet_report.py" --repo "$repo" --out "$state/readings" --occupancy-sample --occupancy-scope org

stamp="$state/last-push"
if [ -f "$stamp" ] && [ $(( $(date +%s) - $(stat -c %Y "$stamp") )) -lt $(( push_min * 60 - 120 )) ]; then
  exit 0
fi
cd "$state/repo"
for try in 1 2 3; do
  git fetch -q --depth=1 origin "$branch"
  git checkout -q -B "$branch" FETCH_HEAD
  # the branch's readings and ours, one per timestamp, oldest first (fleet_report keeps 35 days of them)
  python3 - "$state/readings/occupancy.jsonl" occupancy.jsonl <<'PY'
import json, sys
mine, theirs = sys.argv[1], sys.argv[2]
rows = {}
for path in (theirs, mine):
    try:
        lines = open(path).read().splitlines()
    except FileNotFoundError:
        continue
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        rows[r.get("t", "")] = r
open(theirs, "w").write("".join(json.dumps(rows[t], sort_keys=True) + "\n" for t in sorted(rows) if t))
PY
  git add occupancy.jsonl
  if git diff --cached --quiet; then touch "$stamp"; exit 0; fi
  last=$(tail -n 1 occupancy.jsonl | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["t"])')
  git commit -q -m "occupancy: readings to $last ($(hostname -s))"
  if git push -q origin "HEAD:$branch"; then touch "$stamp"; exit 0; fi
  sleep $(( try * 5 ))
done
echo "occupancy-sampler: push failed 3 times; the readings stay in $state/readings and go with the next push" >&2
exit 1
