# 🦑 squid-bot

**Autonomous CI/CD Shepherd, Host Admission Supervisor & Fleet Orchestrator for AI Coding Fleets.**

[![Tests](https://img.shields.io/badge/tests-78%20passed-brightgreen.svg)]()
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)]()
[![Platform: Linux](https://img.shields.io/badge/platform-Linux%20%7C%20POSIX-lightgrey.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

`squid-bot` is the operational harness that turns fleets of autonomous LLM coding agents into a reliable, high-throughput software factory. It prevents parallel agents from freezing your host machines, eliminates brittle CI wait loops, classifies test failures automatically, and autonomously merges pull requests the moment ruleset checks pass.

---

## 📑 Table of Contents
1. [The Problem: The Multi-Agent Tragedy of the Commons](#-the-problem-the-multi-agent-tragedy-of-the-commons)
2. [Key Capabilities](#-key-capabilities)
3. [Architecture](#-architecture)
4. [Component Deep Dive](#-component-deep-dive)
   - [Host Admission Supervisor (`box_status.py`)](#1-host-admission-supervisor-box_statuspy)
   - [Dynamic Core Allocator (`heavy-run.sh`)](#2-dynamic-core-allocator-heavy-runsh)
   - [State-Reconciling Merge Shepherd (`shepherd_gh.py` & `ci_shepherd.py`)](#3-state-reconciling-merge-shepherd-shepherd_ghpy--ci_shepherdpy)
   - [CI Triage & Local Repro Generator (`ci_digest.py`)](#4-ci-triage--local-repro-generator-ci_digestpy)
   - [PR Inspector (`pr_check.py`)](#5-pr-inspector-pr_checkpy)
5. [Installation & Setup](#-installation--setup)
6. [Configuration Reference (`squid-bot.toml`)](#-configuration-reference-squid-bottonl)
7. [GitHub Actions Workflow Integration](#-github-actions-workflow-integration)
8. [Agent Fleet Integration (Claude / Gemini / Cursor)](#-agent-fleet-integration-claude--gemini--cursor)
9. [The GitGalaxy Proving Ground & Sync Bridge](#-the-gitgalaxy-proving-ground--sync-bridge)
10. [Troubleshooting & FAQ](#-troubleshooting--faq)
11. [License](#-license)

---

## ⚡ The Problem: The Multi-Agent Tragedy of the Commons

When scaling autonomous coding agents (Claude Code, Antigravity, Cursor background workers, OpenHands, Aider) from a single assistant to a concurrent fleet working across multiple branches, engineering bottlenecks immediately shift from **model reasoning** to **systems infrastructure**:

1. **Host Saturation & False Test Diffs:** Parallel agents greedily spawn multi-threaded test runners. Load averages spike to 60–80 on a 12-core host, causing regex/IO timeouts that fake thousands of test failures.
2. **CI Burn & Queue Clogging:** Pushing speculative commits on every attempt exhausts CI runner minutes and clogs GitHub Actions queues with 40-minute matrix runs that could have failed in 15 seconds locally.
3. **The CI Triage Gap:** Agents given raw 10,000-line ANSI CI logs hallucinate or push speculative hope-commits instead of diagnosing the root cause.
4. **Brittle Polling Loops:** Agents running `gh pr checks --watch` or endless sleep loops hang, exhaust rate limits, or crash when webhook events drop or arrive out of order.

`squid-bot` is the **operational operating system** designed to solve this. It introduces deterministic host admission control, dynamic CPU core re-balancing, a state-reconciling CI merge shepherd, and automated local failure reproduction for AI agents.

---

## 🎯 Key Capabilities

* **Linux PSI Admission Control:** Evaluates Pressure Stall Information (CPU, memory, disk I/O) to issue `GREEN`, `YELLOW`, or `RED` verdicts before launching heavy agent jobs.
* **Dynamic Core Balancing:** Partitions available CPU cores dynamically (`SQUID_BOT_CORES = nproc / held_slots`) so concurrent agent test suites cooperatively yield compute.
* **State-Driven Auto-Merge:** Reconciles the GitHub state machine directly, ignoring dropped webhook events, and automatically squash-merges PRs with `shepherd:merge` when green.
* **Zero-Hallucination CI Triage:** Extracts the exact failure category (`infra`, `flake`, `main`, `dirty`, or `real`) and formats the exact local CLI command to reproduce the failure.
* **Shift-Work Handoff Resilience:** Standardizes markdown handoff protocols so agents survive rate limits, context windows, and model preemptions without losing in-flight state.

---

## 🏛️ Architecture

```mermaid
flowchart TD
    subgraph FLEET["Autonomous Agent Fleet (Claude / Gemini / Cursor)"]
        A1["Worker Agent 1\n(Worktree A)"]
        A2["Worker Agent 2\n(Worktree B)"]
        A3["Worker Agent 3\n(Worktree C)"]
    end

    subgraph HOST["Host Layer: Admission & Dynamic Slicing"]
        BS["box-status\n(Linux PSI + Load + Memory)"]
        HR["heavy-run.sh\n(Dynamic Cores: nproc / slots)"]
        LK["exclusive-lock.sh\n(Serialized Golden Baselines)"]
    end

    subgraph CI["CI Layer: Deterministic Merge Shepherd"]
        GH["shepherd_gh\n(State Reconciliation, Not Events)"]
        SHP["ci_shepherd daemon\n(Label-Driven Auto-Merge Queue)"]
        DIG["ci_digest\n(Root-Cause Triage & Local Repro)"]
    end

    FLEET -->|"1. Check Admission"| BS
    FLEET -->|"2. Run Local Fast Gates"| HR
    FLEET -->|"3. Open Draft PR & Label"| GH
    GH -->|"4. Required Checks Pass"| SHP
    GH -->|"5. On Failure -> Structured Triage"| DIG
    DIG -->|"6. Reproduce Locally & Push Once"| FLEET
```

### Core Design Axioms
1. **State Over Events:** GitHub webhooks can drop, queue up, or arrive out of order. Merge decisions must evaluate the current state snapshot, not a sequence of events.
2. **Never Let Agents Poll CI:** Prohibit `gh pr checks --watch` and sleep loops. Waiting is outsourced to a deterministic background daemon (`ci_shepherd`), keeping agent sessions short, bounded, and cheap.
3. **Local Repro Before Push:** Never push to GitHub just to find out whether a test passed. CI failures must be translated into local command-line invocations that run in seconds.

---

## 🔍 Component Deep Dive

### 1. Host Admission Supervisor (`box_status.py`)
CLI command: `box-status`

Monitors host capacity using Linux `/proc/pressure` (PSI) and `/proc/loadavg`. It calculates real-time CPU, memory, and I/O pressure to ensure agent swarms do not starve the host.

```bash
$ box-status
GREEN: headroom
cores 12  load 2.4 / 3.0 / 3.4  cpu-pressure 0.02%  mem-pressure 0.09%
memory available 24.3 / 30.4 GB  swap used 4.9 GB
heavy slots 0/3 held  free for new heavy work: 3
busiest (cores in use, by worktree or command):
   0.08  find
   0.05  agy
disk: io-pressure 1.79% (full 1.57%)  tasks blocked on disk 0
```

#### Decision Matrix
| Verdict | Condition | Action for Orchestrator |
|---|---|---|
| **`GREEN`** | Headroom available, load normal, low pressure | Launch new worker agents or heavy test runs freely. |
| **`YELLOW`** | Slots saturated, memory < 20%, or load >= 70% | Restrict to read-only scouting, editing, or narrow unit tests. No heavy proofs. |
| **`RED`** | Load >= 100%, CPU PSI >= 40%, or memory < 10% | Pause all launches. Signal newest running workers to checkpoint and idle. |

---

### 2. Dynamic Core Allocator (`heavy-run.sh`)
Usage: `scripts/heavy-run.sh <command...>`

Instead of letting every agent run `pytest -n auto` (which grabs all cores and explodes load), `heavy-run.sh` coordinates through a shared lock directory (`/tmp/squid-bot/locks`).
* It acquires one of $N$ shared slots ($N = \text{cores} / 4$, minimum 1).
* It checks how many other slots are held and exports `SQUID_BOT_CORES = nproc / held_slots`.
* Downstream tools (pytest xdist, compiler jobs, gauntlets) scale their inner thread/process count to this dynamic slice.

---

### 3. State-Reconciling Merge Shepherd (`shepherd_gh.py` & `ci_shepherd.py`)
CLI command: `squid-bot run --every 600` or `shepherd`

The Shepherd monitors pull requests carrying `shepherd:*` labels and coordinates auto-merging:

#### Supported Request Labels
* `shepherd:merge`: Mark PR ready and squash-merge as soon as all required checks are green.
* `shepherd:hold`: Never merge. Freezes auto-merge even if checks are green.
* `shepherd:matrix`: Dispatches full cross-platform test suites; waits for green before merging.
* `shepherd:full`: Runs extended full sweeps on the branch.

#### Automated Status Labels
* `shepherd:needs-fix`: Set when a real code failure or merge conflict is detected. Automatically posts a structured digest comment.
* `shepherd:waiting-on-main`: Set when the check failure also reproduces on `origin/main` (meaning main is broken, not the PR).
* `shepherd:retrying`: Set when an infra failure (runner lost, Docker rate limit) or flake is being automatically re-run.

---

### 4. CI Triage & Local Repro Generator (`ci_digest.py`)
CLI command: `ci-digest <PR_NUMBER>`

When CI fails, `ci_digest` scrapes the failed GitHub Actions job logs, strips noisy timestamps and ANSI escapes, classifies the failure, and prints the exact local reproduction command.

```bash
$ ci-digest 4976
#4976 HEAD 5b7f5c5e
  check: full-suite part 3/3 (failure)
  triage: real (code failure)
  failed tests:
    tests/core/test_parser.py::test_utf8_bom_handling
  error:
    AssertionError: assert 'expected' == 'actual'
  local repro:
    python -m pytest tests/core/test_parser.py -k test_utf8_bom_handling
```

---

### 5. PR Inspector (`pr_check.py`)
CLI command: `pr-check <PR_NUMBER> [--digest] [--merge]`

Performs a single-call inspection of a pull request: draft status, mergeability, check suite statuses grouped by outcome, flagged files (baselines, pins, golden masters), and failure digests.

---

## 🛠️ Installation & Setup

### Requirements
* Linux / POSIX system (Linux required for `/proc/pressure` PSI support).
* Python 3.10 or later.
* GitHub CLI (`gh`) authenticated with repository write access.

### Install Package
```bash
git clone https://github.com/squid-protocol/squid-bot.git
cd squid-bot
pip install -e .
```

---

## ⚙️ Configuration Reference (`squid-bot.toml`)

`squid-bot` looks for `squid-bot.toml` in the current working directory, git root, or home directory.

```toml
[repository]
# GitHub repository slug (defaults to $GITHUB_REPOSITORY or auto-detected via gh)
slug = "squid-protocol/gitgalaxy"

[shepherd]
# Required CI checks before auto-merging a PR with the shepherd:merge label.
required_checks = [
    "det",
    "full-suite",
    "ground-truth",
    "mypy-audit",
    "ruff-audit"
]

# Poll interval in seconds for background queue reconciliation (default: 600 = 10 min)
poll_interval_seconds = 600

# Path to state persistence file
state_path = "/tmp/squid-bot/state/shepherd.json"

[supervisor]
# Directory used for slot and exclusive flock files
lock_dir = "/tmp/squid-bot/locks"

# Maximum heavy-run slots (defaults to nproc / 4)
heavy_slots = 3
```

### Environment Variable Overrides
| Variable | Description |
|---|---|
| `SQUID_BOT_REPO` | Overrides repository slug (`owner/repo`). |
| `SQUID_BOT_LOCK_DIR` | Overrides lock directory path. |
| `SQUID_BOT_STATE` | Overrides path to `shepherd.json` state file. |
| `SQUID_BOT_CORES` | Sliced core count exported by `heavy-run.sh`. |

---

## 🚀 GitHub Actions Workflow Integration

To enable label-driven auto-merging on your GitHub repository, copy the provided workflow templates into your `.github/workflows/` directory:

```bash
mkdir -p .github/workflows
cp templates/workflows/shepherd-event.yml .github/workflows/
cp templates/workflows/shepherd-reconcile.yml .github/workflows/
```

* **`shepherd-event.yml`**: Triggers on pull request label events (`labeled`, `unlabeled`) and new commits (`synchronize`). Cleans unapproved labels and initiates merge attempts.
* **`shepherd-reconcile.yml`**: Triggers when CI check suites finish (`workflow_run`), periodically on a cron (every 10 minutes), or via manual dispatch. Evaluates queue states and handles auto-merges.

---

## 🤖 Agent Fleet Integration (Claude / Gemini / Cursor)

`squid-bot` ships with battle-tested skills and system prompts for LLM agents:

### 1. Box Capacity Skill (`templates/skills/box-capacity/SKILL.md`)
Add this skill to your agent configuration (e.g. `.claude/skills/box-capacity/` or Antigravity custom skills). It instructs agents to:
1. Always run `box-status` before kicking off CPU-intensive gates or proof sweeps.
2. Yield when verdict is `YELLOW` or `RED`.
3. Wrap all heavy commands in `scripts/heavy-run.sh`.

### 2. Parallel PR Agents Skill (`templates/skills/parallel-agents/SKILL.md`)
Provides the operating rules for multi-agent swarms:
* **One Worktree Per Agent:** Never work directly in the primary checkout. Spawn dedicated worktrees (`git worktree add ../worktrees/<branch>`).
* **Additive-Only Seams:** Never reorder shared tables or global registers; only append.
* **Re-bless Only Own Drift:** Diff against `origin/main` before re-blessing baselines.
* **Never Poll CI:** Hand off waiting to the Shepherd daemon.

---

## 🔄 The GitGalaxy Proving Ground & Sync Bridge

`squid-bot` was forged and battle-tested inside **[GitGalaxy](https://github.com/squid-protocol/gitgalaxy)** across 5,000+ PRs and hundreds of concurrent agent runs.

To allow active experimentation and domain-specific development to continue natively inside GitGalaxy while keeping `squid-bot` clean and generalized, a bi-directional synchronization bridge is built into `scripts/sync.py`:

```bash
# Check drift between GitGalaxy and squid-bot
python scripts/sync.py status
python scripts/sync.py diff

# Pull improvements from GitGalaxy into squid-bot (with automated parameterization)
python scripts/sync.py pull

# Push generalized fixes from squid-bot back to GitGalaxy
python scripts/sync.py push
```

The bridge automatically parameterizes hardcoded paths, maps package imports, updates lock variables, and adapts test suites.

---

## ❓ Troubleshooting & FAQ

#### Q: Why is `box-status` reporting `YELLOW` even when CPU usage is low?
**A:** `box-status` checks all 3 dimensions of Linux PSI (CPU, Memory, and Disk I/O). If a heavy database container or disk sweep is thrashing your disk, the I/O pressure or D-state wait count will raise a `YELLOW` verdict to protect the host from I/O lockup.

#### Q: My PR has `shepherd:merge` but won't merge. Why?
**A:** Run `pr-check <PR_NUMBER>`. Common reasons:
1. One or more required checks in `required_checks` (or your branch ruleset) are still pending or failed.
2. The PR is behind `main` (the shepherd will automatically update the branch on its next pass if checks were clean).
3. The PR body contains `Depends on #123`, and PR #123 is still open.
4. The label was added by a user without repository write permissions.

#### Q: How does `squid-bot` differ from GitHub Merge Queue or Mergify?
**A:** GitHub Merge Queue and Mergify are designed for human pull request queues. They do not provide host-level admission control (preventing your local server from melting), dynamic CPU core slicing, structured failure taxonomy (`infra` vs `flake` vs `real`), or automated local CLI reproduction commands tailored for AI agent execution loops.

---

## 📜 License

MIT License. Copyright (c) 2026 Joe Esquibel / Squid Protocol.
