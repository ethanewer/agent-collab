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

| Task | Control | Duo | Delta |
|---|---|---|---|
| chess-best-move | 0.0 (4/5) | 0.0 (5/5) | 0.0 |
| circuit-fibsqrt | 0.0 (3/5) | 0.2 (5/5) | +0.2 |
| compile-compcert | 0.0 (2/5) | 0.2 (5/5) | +0.2 |
| extract-elf | 0.6 (4/5) | 0.2 (5/5) | -0.4 |
| git-leak-recovery | 0.8 (4/5) | 1.0 (5/5) | +0.2 |
| multi-source-data-merger | 0.6 (3/5) | 0.8 (4/5) | +0.2 |
| path-tracing | 0.0 (5/5) | 0.0 (4/5) | 0.0 |
| rstan-to-pystan | 0.0 (5/5) | 0.0 (5/5) | 0.0 |
| sanitize-git-repo | 0.4 (4/5) | 0.8 (5/5) | +0.4 |
| sparql-university | 0.6 (3/5) | 1.0 (5/5) | +0.4 |
| sqlite-db-truncate | 0.6 (3/5) | 1.0 (5/5) | +0.4 |
| torch-tensor-parallelism | 0.2 (4/5) | 0.0 (4/5) | -0.2 |
| **Aggregate** | **0.317 (19/60)** | **0.433 (26/60)** | **+0.117** |

Scores are mean reward across 5 attempts. Values in parentheses show how many of the 5 trials completed without an agent exception (the remainder errored before producing a result).

### Error breakdown

| Error type | Control | Duo |
|---|---|---|
| AgentTimeoutError | 15 | 16 |
| NonZeroAgentExitCodeError | 19 | 3 |
| **Total errors** | **34** | **19** |

## Key Observations

- The duo achieved a **37% relative improvement** over the control (0.433 vs 0.317 mean score).
- The duo reached **perfect scores** (5/5) on three tasks: `git-leak-recovery`, `sparql-university`, and `sqlite-db-truncate`.
- The duo improved on 7 of 12 tasks, tied on 3, and regressed on 2.
- Agent crashes dropped sharply in the duo setup (3 vs 19), suggesting that having a second agent provides resilience — if one agent fails, the other can still complete the task.
- Timeout rates were comparable (16 vs 15), indicating that coordination overhead did not significantly increase the likelihood of hitting time limits.
- The duo regressed on `extract-elf` (-0.4), likely due to both agents modifying the same output file despite coordination prompts.
- Three tasks remained unsolved by both setups: `chess-best-move`, `path-tracing`, and `rstan-to-pystan`.
- Wall-clock time for the duo run was approximately 2x the control (3h 25m vs 1h 28m), as concurrency was halved to account for doubled API usage per trial.
