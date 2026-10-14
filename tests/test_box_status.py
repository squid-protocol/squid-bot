"""Tests for squid_bot.box_status admission control logic."""

import pytest
from squid_bot.box_status import decide


def test_box_status_decide_green():
    snapshot = {
        "cores": 12,
        "load1": 2.0,
        "cpu_psi": 2.0,
        "io_psi": 1.0,
        "mem_avail_gb": 24.0,
        "mem_total_gb": 32.0,
        "swap_used_gb": 0.0,
        "mem_psi": 0.0,
        "slots": 3,
        "slots_held": 0,
        "culprit": None,
        "blocked": 0,
    }
    res = decide(snapshot)
    assert res["verdict"] == "GREEN"
    assert res["free_slots"] == 3
    assert res["reasons"] == ["headroom"]


def test_box_status_decide_yellow_on_slots_held():
    snapshot = {
        "cores": 12,
        "load1": 3.0,
        "cpu_psi": 2.0,
        "io_psi": 1.0,
        "mem_avail_gb": 24.0,
        "mem_total_gb": 32.0,
        "swap_used_gb": 0.0,
        "mem_psi": 0.0,
        "slots": 3,
        "slots_held": 3,
        "culprit": None,
        "blocked": 0,
    }
    res = decide(snapshot)
    assert res["verdict"] == "YELLOW"
    assert res["free_slots"] == 0
    assert any("slots held" in r for r in res["reasons"])


def test_box_status_decide_red_on_overload():
    snapshot = {
        "cores": 12,
        "load1": 15.0,  # > cores
        "cpu_psi": 45.0,  # >= 40%
        "io_psi": 1.0,
        "mem_avail_gb": 24.0,
        "mem_total_gb": 32.0,
        "swap_used_gb": 0.0,
        "mem_psi": 0.0,
        "slots": 3,
        "slots_held": 1,
        "culprit": None,
        "blocked": 0,
    }
    res = decide(snapshot)
    assert res["verdict"] == "RED"
    assert any("load1" in r for r in res["reasons"])
    assert any("CPU pressure" in r for r in res["reasons"])


def test_box_status_decide_red_on_low_memory():
    snapshot = {
        "cores": 12,
        "load1": 1.0,
        "cpu_psi": 0.0,
        "io_psi": 0.0,
        "mem_avail_gb": 2.0,  # < 10% of 32 GB
        "mem_total_gb": 32.0,
        "swap_used_gb": 10.0,
        "mem_psi": 25.0,  # >= 20%
        "slots": 3,
        "slots_held": 0,
        "culprit": None,
        "blocked": 0,
    }
    res = decide(snapshot)
    assert res["verdict"] == "RED"
    assert any("memory available" in r for r in res["reasons"])
