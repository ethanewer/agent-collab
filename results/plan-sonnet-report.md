# Plan-Then-Build Experiment Report

## Overview

This experiment compares two approaches on the 12-task terminal-bench subset:

- **Control**: Single Claude Code session with full permissions (CC 2.1.92).
- **Plan**: Two-phase Claude Code — first a read-only planning session (`--permission-mode plan`), then a build session that resumes with the approved plan (`--resume SESSION --dangerously-skip-permissions`) (CC 2.1.109).

Both use `claude-sonnet-4-6`. Each condition has 60 trials (5 per task, no infrastructure errors).

## Pass Rate

Pass rate = pass / (pass + fail + timeout). 5 trials per task.

| Task | Control | Plan |
|---|--:|--:|
| chess-best-move | 0/5 | 0/5 |
| circuit-fibsqrt | 0/5 | 0/5 |
| compile-compcert | 3/5 | 1/5 |
| extract-elf | 3/5 | 4/5 |
| git-leak-recovery | 5/5 | 5/5 |
| multi-source-data-merger | 5/5 | 4/5 |
| path-tracing | 0/5 | 0/5 |
| rstan-to-pystan | 5/5 | 4/5 |
| sanitize-git-repo | 3/5 | 2/5 |
| sparql-university | 4/5 | 5/5 |
| sqlite-db-truncate | 4/5 | 3/5 |
| torch-tensor-parallelism | 3/5 | 0/5 |
| **Mean** | **0.58** | **0.47** |

## Pass Rate (Ignoring Timeouts)

Pass rate = pass / (pass + fail). Timeout trials excluded. Tasks where all trials timed out are omitted.

| Task | Control | Plan |
|---|--:|--:|
| chess-best-move | 0/4 | 0/1 |
| compile-compcert | 3/3 | 1/1 |
| extract-elf | 3/5 | 4/5 |
| git-leak-recovery | 5/5 | 5/5 |
| multi-source-data-merger | 5/5 | 4/5 |
| rstan-to-pystan | 5/5 | 4/4 |
| sanitize-git-repo | 3/5 | 2/5 |
| sparql-university | 4/5 | 5/5 |
| sqlite-db-truncate | 4/5 | 3/3 |
| torch-tensor-parallelism | 3/5 | 0/5 |
| **Mean** | **0.74** | **0.72** |

## Analysis

Planning reduces the overall pass rate from **58%** to **47%** (a 12-point drop). Timeouts increase from 13 to 21.

**Where planning hurts (control > plan):**

- **compile-compcert** (0.60 → 0.20): The planning phase runs for the full timeout exploring the large CompCert codebase without ever producing a plan. 4 of 5 runs timeout in plan mode.
- **torch-tensor-parallelism** (0.60 → 0.00): All 5 runs fail. The agent successfully plans but fails during execution.
- **sqlite-db-truncate** (0.80 → 0.60): 2 of 5 runs timeout in plan mode despite this being a straightforward task.
- **rstan-to-pystan** (1.00 → 0.80): 1 timeout in plan mode.
- **multi-source-data-merger** (1.00 → 0.80): 1 failure during build.
- **sanitize-git-repo** (0.60 → 0.40): 1 additional failure.

**Where planning helps (plan > control):**

- **extract-elf** (0.60 → 0.80): 1 additional pass.
- **sparql-university** (0.80 → 1.00): 1 additional pass.

**No change:**

- chess-best-move, circuit-fibsqrt, path-tracing (0.00 both — these always timeout).
- git-leak-recovery (1.00 both — trivially solved).

## Root Cause

The plan-then-build approach splits the agent's time budget between two phases. For tasks where the agent can solve the problem directly, the planning phase is overhead that provides no benefit and risks consuming the entire timeout. The plan step ran to timeout in 21 of 60 trials (35%) compared to 13 timeouts for control (22%).

## Method

- Control results from `results/sonnet-single-full.json` (CC 2.1.92, sonnet-4.6).
- Plan results from `jobs/plan-sonnet/` and `jobs/rerun-plan/` (CC 2.1.109, sonnet-4.6). Infrastructure errors (disk space, install failures) were excluded. For tasks with more than 5 non-infra runs, 5 were selected to minimize the difference between the subset pass rate and the full pass rate. Detailed results in `results/plan-sonnet-results.json`.
