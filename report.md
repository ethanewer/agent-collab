# Duo Agent Collaboration Experiment

## Overview

This experiment tests whether two Claude Code agents collaborating on the same task outperform a single agent. Both setups are evaluated on Terminal Bench 2.0's 12-task efficient subset, with 5 attempts per task (60 trials total).

## Setup

| Parameter | Value |
|---|---|
| Model | `claude-sonnet-4-6` |
| Claude Code CLI | `2.1.92` |
| Benchmark | Terminal Bench 2.0 (efficient subset, 12 tasks) |
| Attempts per task | 5 |
| Framework | [Harbor](https://github.com/cthulhoo/harbor) 0.3.0 |

### Control

A single Claude Code instance runs with `--permission-mode=bypassPermissions`. This uses Harbor's built-in `ClaudeCode` agent with the version pinned to `2.1.92`.

### Duo

Two Claude Code instances run concurrently in the same container on the same task. Each instance has its own `CLAUDE_CONFIG_DIR` to avoid session conflicts. They collaborate via a shared append-only log file at `/tmp/agent-collab.log`, with timestamped messages prefixed by agent identity (`[A|...]` or `[B|...]`). Agent A initiates communication; Agent B waits briefly for Agent A's plan before starting. Neither agent has any tool restrictions. The trial succeeds if at least one agent exits with code 0.

## Duo Collaboration Prompts

### Agent A (initiator)

```
You are one of two equal AI agents (you are Agent A, the other is Agent B) working
together in the same environment on the same task.

COMMUNICATION: You share a log file at /tmp/agent-collab.log.
- Append only: echo "[A|$(date -u +%Y-%m-%dT%H:%M:%S)] <msg>" >> /tmp/agent-collab.log
- Read it with: cat /tmp/agent-collab.log

You go first. Before doing any real work:
1. Analyze the task briefly.
2. Post a proposed plan and division of work to the log file. Suggest what each agent
   should handle. End with PLAN_READY.
3. Wait briefly for Agent B to respond (poll with:
   for i in $(seq 1 15); do grep -q '\[B|' /tmp/agent-collab.log 2>/dev/null && break;
   sleep 2; done; cat /tmp/agent-collab.log). If no response after ~30s, start working
   anyway.
4. Once you've both agreed on the division, start your part.

Throughout the task, check /tmp/agent-collab.log regularly for messages from Agent B.
Post brief status updates so Agent B knows you're making progress. Coordinate on who
modifies which files to avoid conflicts. Once you agree on a division, stick to it —
don't do the other agent's part unless you discuss it first. Discuss before completing.
Never block in a loop waiting for the other agent — do useful work instead.
```

### Agent B (responder)

```
You are one of two equal AI agents (you are Agent B, the other is Agent A) working
together in the same environment on the same task.

COMMUNICATION: You share a log file at /tmp/agent-collab.log.
- Append only: echo "[B|$(date -u +%Y-%m-%dT%H:%M:%S)] <msg>" >> /tmp/agent-collab.log
- Read it with: cat /tmp/agent-collab.log

Agent A goes first. Before doing any real work:
1. Wait for Agent A's plan:
   for i in $(seq 1 20); do grep -q 'PLAN_READY' /tmp/agent-collab.log 2>/dev/null
   && break; sleep 3; done
2. Read the plan: cat /tmp/agent-collab.log
3. If no plan appeared, start working on the task independently.
4. Otherwise, reply with your thoughts and confirm the division of work.

Throughout the task, check /tmp/agent-collab.log regularly for messages from Agent A.
Post brief status updates so Agent A knows you're making progress. Coordinate on who
modifies which files to avoid conflicts. Once you agree on a division, stick to it —
don't do the other agent's part unless you discuss it first. Discuss before completing.
Never block in a loop waiting for the other agent — do useful work instead.
```

## Results

The initial run used 12 concurrent containers for control and 6 for duo. Many control trials failed during Claude Code installation with exit code 137 (OOM kill), disproportionately affecting the control due to higher concurrency. All `NonZeroAgentExitCodeError` trials from the initial run were rerun with a maximum of 8 concurrent containers. The results below combine the original successful trials with the rerun replacements, giving exactly 5 scored attempts per task for both setups.

| Task | Control | Duo | Delta |
|---|---|---|---|
| chess-best-move | 0/5 (0.00) | 0/5 (0.00) | 0.00 |
| circuit-fibsqrt | 0/5 (0.00) | 1/5 (0.20) | +0.20 |
| compile-compcert | 0/5 (0.00) | 1/5 (0.20) | +0.20 |
| extract-elf | 4/5 (0.80) | 1/5 (0.20) | -0.60 |
| git-leak-recovery | 5/5 (1.00) | 5/5 (1.00) | 0.00 |
| multi-source-data-merger | 5/5 (1.00) | 5/5 (1.00) | 0.00 |
| path-tracing | 0/5 (0.00) | 0/5 (0.00) | 0.00 |
| rstan-to-pystan | 0/5 (0.00) | 0/5 (0.00) | 0.00 |
| sanitize-git-repo | 2/5 (0.40) | 4/5 (0.80) | +0.40 |
| sparql-university | 5/5 (1.00) | 5/5 (1.00) | 0.00 |
| sqlite-db-truncate | 5/5 (1.00) | 5/5 (1.00) | 0.00 |
| torch-tensor-parallelism | 1/5 (0.20) | 0/5 (0.00) | -0.20 |
| **Aggregate** | **27/60 (0.450)** | **27/60 (0.450)** | **0.000** |

Scores are mean reward across 5 attempts per task.

## Key Observations

- After rerunning infrastructure failures, **both setups scored identically: 27/60 (0.450)**.
- The duo improved on 3 tasks: `circuit-fibsqrt` (+0.20), `compile-compcert` (+0.20), `sanitize-git-repo` (+0.40).
- The duo regressed on 2 tasks: `extract-elf` (-0.60), `torch-tensor-parallelism` (-0.20).
- Both setups achieved perfect scores on 4 tasks: `git-leak-recovery`, `multi-source-data-merger`, `sparql-university`, `sqlite-db-truncate`.
- Four tasks remained unsolved by both setups: `chess-best-move`, `path-tracing`, `rstan-to-pystan`, and `circuit-fibsqrt` (control only).
- The initial run appeared to show a large duo advantage (0.433 vs 0.317), but this was entirely explained by OOM kills during Claude Code installation caused by running 12 concurrent Docker containers for control vs 6 for duo.

### OOM kill analysis

The initial run suffered `NonZeroAgentExitCodeError` (exit code 137) during the Claude Code CLI installation step inside Docker containers. The Linux OOM killer terminated the installation process when memory was exhausted. Control had 19 such failures (at 12 concurrent containers) vs 3 for duo (at 6 concurrent containers). Rerunning these trials at lower concurrency (max 8) eliminated the installation OOM kills entirely.

### Where the duo helps and hurts

- **Duo advantage — `sanitize-git-repo` (+0.40):** Agents divided the work (e.g., one handled sensitive file cleanup while the other managed git history rewriting), leading to more consistent success.
- **Duo disadvantage — `extract-elf` (-0.60):** Both agents modified the same output file despite coordination prompts, leading to overwrites and incorrect results. This was the duo's worst regression.
- **Duo disadvantage — `torch-tensor-parallelism` (-0.20):** Coordination overhead on a task requiring deep, sequential reasoning provided no benefit and consumed time.
