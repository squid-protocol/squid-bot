"""scripts/backfill_exact_runner_time.py summarize -> runner_exact.json, as fleet_report.py reads it."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "actions" / "fleet-report"))
import fleet_report as dashboard  # noqa: E402


def test_summarize_writes_what_the_report_reads(tmp_path):
    w = [{"repo": r, "day": f"{d}T00:00:00Z"} for d in ("2026-10-27", "2026-10-28") for r in ("o/gg", "o/x")]
    jobs = [  # two hours of one gitgalaxy job, one hour of another repo's, a skipped job, a 1-second one, a duplicate
        {"repo": "o/gg", "run": 1, "job": 11, "start": "2026-10-28T10:00:00Z", "end": "2026-10-28T12:00:00Z", "conclusion": "SUCCESS"},
        {"repo": "o/gg", "run": 1, "job": 11, "start": "2026-10-28T10:00:00Z", "end": "2026-10-28T12:00:00Z", "conclusion": "SUCCESS"},
        {"repo": "o/x", "run": 2, "job": 21, "start": "2026-10-28T11:00:00Z", "end": "2026-10-28T12:00:00Z", "conclusion": "FAILURE"},
        {"repo": "o/x", "run": 3, "job": 31, "start": "2026-10-28T11:00:00Z", "end": "2026-10-28T13:00:00Z", "conclusion": "SKIPPED"},
        {"repo": "o/gg", "run": 4, "job": 41, "start": "2026-10-28T11:00:00Z", "end": "2026-10-28T11:00:01Z", "conclusion": "SUCCESS"},
        {"repo": "o/gg", "run": 5, "job": 51, "start": "2026-10-27T23:30:00Z", "end": "2026-10-28T00:30:00Z", "conclusion": "SUCCESS"},
    ]
    (tmp_path / "windows.jsonl").write_text("".join(json.dumps(x) + "\n" for x in w))
    (tmp_path / "jobs.jsonl").write_text("".join(json.dumps(x) + "\n" for x in jobs))
    out = tmp_path / "runner_exact.json"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "backfill_exact_runner_time.py"), "--state", str(tmp_path),
                    "summarize", "--pool", "2", "--repo", "o/gg", "--minutes-since", "2026-10-28", "--write", str(out)],
                   check=True, capture_output=True)
    doc = json.loads(out.read_text())
    assert list(doc["days"]) == ["2026-10-28"]  # the first day is left out: the day before it wasn't read
    day = doc["days"]["2026-10-28"]
    assert day["runner_min"] == 120 + 60 + 30 and day["by_repo"] == {"o/gg": 150, "o/x": 60}
    assert day["peak"] == 2 and day["min_at_pool"] == 60  # 11:00-12:00 both runners
    assert len(doc["minutes"]["2026-10-28"]["org"]) == 1440 and doc["minutes"]["2026-10-28"]["repo"][660] == 1
    ex = dashboard.exact_detail(doc, "2026-10-28", "o/gg")
    assert ex["runner_min"] == 150 and ex["org_runner_min"] == 210
