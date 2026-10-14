"""Tests for the live fleet dashboard (scripts/dashboard.py): fetch planning and rendering."""

import sys
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dashboard  # noqa: E402

TODAY = date(2026, 10, 14)


def _day(merged: int, ok: int, bad: int, other: int) -> dict:
    return {"merged": merged, "runs": {"total": ok + bad + other, "success": ok, "failure": bad,
                                       "cancelled": other, "skipped": 0}}


def _data(days: int = 30) -> dict:
    return {
        "repo": "owner/repo",
        "updated": "2026-10-14T18:00Z",
        "open_prs": 9,
        "days": {(TODAY - timedelta(days=i)).isoformat(): _day(30 + i, 1500, 40, 300) for i in range(days)},
    }


def test_days_to_fetch_backfills_the_window_then_only_refreshes_recent_days():
    assert len(dashboard.days_to_fetch({}, TODAY, 30)) == 30
    have = _data()["days"]
    assert dashboard.days_to_fetch(have, TODAY, 30) == ["2026-10-12", "2026-10-13", "2026-10-14"]


def test_render_is_valid_svg_with_one_bar_per_day_and_real_totals():
    svg = dashboard.render(_data(), 30, TODAY)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    titles = [t.text for t in root.iter("{http://www.w3.org/2000/svg}title")]
    assert sum("merged" in t for t in titles) == 30
    assert sum("runs" in t for t in titles) == 30
    merged_total = sum(30 + i for i in range(30))
    assert f"{merged_total:,}" in svg
    assert "97.4%" in svg  # 1500 / (1500 + 40)


def test_render_ignores_history_outside_the_window():
    data = _data(40)
    svg = dashboard.render(data, 30, TODAY)
    assert "last 30 days" in svg
