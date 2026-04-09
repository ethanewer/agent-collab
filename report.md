# Duo Agent Collaboration Experiment

## Overview

This experiment tests whether two Claude Code agents collaborating on the same task outperform a single agent. Both setups are evaluated on Terminal Bench 2.0's 12-task efficient subset. The control runs 10 trials per task (120 total); the duo runs 5 trials per task (60 total). The "Control (pass@2)" column estimates how often a single agent would succeed if given two independent attempts, providing a fairer cost comparison since the duo uses roughly 2x the compute.

## Setup

| Parameter | Value |
|---|---|
| Model | `claude-sonnet-4-6` |
| Claude Code CLI | `2.1.92` |
| Benchmark | Terminal Bench 2.0 (efficient subset, 12 tasks) |
| Framework | [Harbor](https://github.com/cthulhoo/harbor) 0.3.0 |

### Control

A single Claude Code instance runs with `--permission-mode=bypassPermissions`. This uses Harbor's built-in `ClaudeCode` agent with the version pinned to `2.1.92`. 10 trials per task, 120 total.

### Duo

Two Claude Code instances run concurrently in the same container on the same task. Each instance has its own `CLAUDE_CONFIG_DIR` to avoid session conflicts. They collaborate via a shared append-only log file at `/tmp/agent-collab.log`, with timestamped messages prefixed by agent identity (`[A|...]` or `[B|...]`). Agent A initiates communication; Agent B waits briefly for Agent A's plan before starting. Neither agent has any tool restrictions. The trial succeeds if at least one agent exits with code 0. 5 trials per task, 60 total.

## Duo Collaboration Prompts

Both agents receive the same communication protocol and file-conflict rules. They differ in initiative and patience instructions. Key elements:

- **Communication protocol**: Append-only shared log file with timestamped, identity-prefixed messages. Exact shell commands provided for reading and writing.
- **Planning phase**: Agents plan together before working.
  - A: Proposes plan and work division, ends with `PLAN_READY` signal. Waits ~30s for B's response, then starts regardless.
  - B: Polls for `PLAN_READY` up to ~60s. If no plan appears, works independently. Otherwise, confirms or adjusts the division.
- **Single-file tasks**: If only one output file is needed, one agent writes it and the other reviews/tests/debugs.
  - Without this rule, both agents independently wrote the same file and silently overwrote each other's work — the single largest source of failures in early iterations.
- **File claiming**: Agents announce which files they will create/modify (e.g. `CLAIMING: extract.js`). If the other agent claimed a file, help by reviewing instead of writing.
- **Status updates** (A only): Post progress every few minutes so B knows A is still working.
  - Without this, B assumed A was stuck after ~90 seconds and took over A's files.
- **Patience** (B only): B must check actual log timestamps before deciding A is inactive. Only take over if A's last message is >5 min old or A asks for help. When impatient, write a message asking for a status update instead of taking over.
  - B's internal sense of elapsed time was unreliable — it claimed "5+ minutes" after only ~90 seconds. Requiring timestamp verification fixed this.
- **Anti-deadlock**: Never block in a loop waiting for the other agent — do useful work instead. Bounded polling loops prevent infinite waits.

### Agent A (initiator)

```
You are one of two equal AI agents (you are Agent A, the other is Agent B) working
together in the same environment on the same task.

COMMUNICATION: You share a log file at /tmp/agent-collab.log.
- Append only: echo "[A|$(date -u +%Y-%m-%dT%H:%M:%S)] <msg>" >> /tmp/agent-collab.log
- Read it with: cat /tmp/agent-collab.log

You go first. Before doing any real work:
1. Analyze the task briefly.
2. Post a proposed plan to the log file. In your plan:
   - List which output files need to be created or modified.
   - If there are multiple output files or areas, propose who handles which.
   - If the task produces only ONE output file, propose that one agent writes it
     and the other reviews, tests, and debugs. This is critical — if both agents
     write the same file, one will silently overwrite the other's work.
   - End with PLAN_READY.
3. Wait briefly for Agent B to respond (poll with:
   for i in $(seq 1 15); do grep -q '\[B|' /tmp/agent-collab.log 2>/dev/null && break;
   sleep 2; done; cat /tmp/agent-collab.log). If no response after ~30s, start working
   anyway.
4. Once you've both agreed on the division, start your part.

AVOIDING FILE CONFLICTS: Before creating or modifying any file, announce it in the
log (e.g. "CLAIMING: extract.js"). If the other agent already claimed that file, do
NOT write to it — help by reviewing their work, writing tests, or debugging instead.
Two agents writing the same file is the #1 cause of failure.

STATUS UPDATES: Post brief progress updates to /tmp/agent-collab.log every few minutes
so Agent B knows you're alive and working. Example: "Still analyzing binary structure,
will write extract.js soon." This prevents Agent B from assuming you're stuck.

Throughout the task, check /tmp/agent-collab.log regularly for messages from Agent B.
Stick to the agreed division. Discuss before completing. Never block in a loop
waiting — do useful work instead.
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
3. If no plan appeared after ~60s, start working on the task independently.
4. Otherwise, reply with your thoughts and confirm the division of work. If there
   is only one output file, agree on who writes it — the other agent should review,
   test, and debug rather than writing a competing version.

AVOIDING FILE CONFLICTS: Before creating or modifying any file, check
/tmp/agent-collab.log to see if Agent A already claimed it. If so, do NOT write to
it — help by reviewing their work, writing tests, or debugging instead. Two agents
writing the same file is the #1 cause of failure.

PATIENCE: Your internal sense of elapsed time is unreliable. Before deciding Agent A
is inactive, you MUST run: cat /tmp/agent-collab.log and check the actual timestamps
of Agent A's messages. Only take over Agent A's claimed work if:
(a) Agent A explicitly asks for help, OR
(b) Agent A's last message timestamp is more than 5 minutes ago AND you have re-read
the log just now to confirm this.
If Agent A is still working, do NOT write their files — instead help by analyzing
the problem, investigating edge cases, preparing tests, or posting useful insights
to the log. If you get impatient, write a status message to the log asking Agent A
for an update instead of taking over their work.

Throughout the task, check /tmp/agent-collab.log regularly for messages from Agent A.
Post brief status updates. Stick to the agreed division. Discuss before completing.
Never block in a loop waiting — do useful work instead.
```

## Results

| Task | Control | Control (pass@2) | Duo |
|---|---|---|---|
| chess-best-move | 0.00 | 0.00 | 0.20 |
| circuit-fibsqrt | 0.00 | 0.00 | 0.00 |
| compile-compcert | 0.00 | 0.00 | 0.60 |
| extract-elf | 0.70 | 0.93 | 0.40 |
| git-leak-recovery | 1.00 | 1.00 | 1.00 |
| multi-source-data-merger | 1.00 | 1.00 | 1.00 |
| path-tracing | 0.00 | 0.00 | 0.00 |
| rstan-to-pystan | 0.10 | 0.20 | 1.00 |
| sanitize-git-repo | 0.20 | 0.38 | 1.00 |
| sparql-university | 1.00 | 1.00 | 0.80 |
| sqlite-db-truncate | 1.00 | 1.00 | 1.00 |
| torch-tensor-parallelism | 0.50 | 0.78 | 0.60 |
| **Aggregate** | **0.46** | **0.52** | **0.63** |

Control: 10 trials per task. Duo: 5 trials per task. Control (pass@2) estimates the probability of at least one success in two independent control attempts, computed as 1 − C(n_fail, 2) / C(n, 2).

## Key Observations

- **The duo outperforms a single agent overall: 0.63 vs 0.46**, and also beats the pass@2 estimate of 0.52, suggesting the collaboration provides value beyond simply having two independent attempts.
- The duo achieved **perfect scores on 5 tasks** (git-leak-recovery, multi-source-data-merger, rstan-to-pystan, sanitize-git-repo, sqlite-db-truncate), compared to 3 for the control.
- Largest duo gains: **rstan-to-pystan** (0.10 → 1.00), **sanitize-git-repo** (0.20 → 1.00), **compile-compcert** (0.00 → 0.60).
- The duo regressed on **extract-elf** (0.70 → 0.40) and **sparql-university** (1.00 → 0.80).
- Both setups failed completely on **chess-best-move**, **circuit-fibsqrt**, and **path-tracing**.

### Where the duo helps

- **Multi-step / multi-file tasks** (sanitize-git-repo, rstan-to-pystan, compile-compcert): Agents divide work across files or stages, and the reviewer role catches bugs the implementer misses (e.g., PyStan parameter mismatches, OOM fixes).
- **Resilience**: When one agent's approach fails, the other can diagnose and fix independently. In rstan-to-pystan, Agent B caught an OOM issue and a parameter bug that Agent A missed.

### Where the duo hurts

- **Single-output-file tasks** (extract-elf): Despite file-conflict prompts, the ELF parsing task still suffers because both agents sometimes make the same technical mistake (incorrect PIE base address) and the reviewer doesn't catch it.
- **Tasks the control already aces** (sparql-university): Coordination overhead can introduce small regressions on tasks a single agent solves reliably.

---

## Unified Prompt Experiment

### Motivation

The v3 duo used separate A/B prompts with prescribed methods (polling loops, file-claiming protocol, role assignments). This experiment replaces them with a single outcome-oriented prompt, identical for both agents except for an `{id}` placeholder (`"p"` / `"q"`). The prompt specifies pitfalls and outcomes, not methods, letting agents discover their own coordination strategies.

### Final Prompt (Iteration 4)

```
You are agent {id}, one of two AI agents working on the same task in the same
environment.

You share an append-only log file at {collab_file} for communication.
- Write: echo "[{id}|$(date -u +%H:%M:%S)] <msg>" >> {collab_file}
- Read: cat {collab_file}

Write your intended approach to the log before starting work. Successful
collaborations start with both agents sharing their plan, then dividing
work to avoid duplication.

Avoid these collaboration failure modes (each has caused task failures):
- Both agents running memory-intensive operations simultaneously
  (compilation, model fitting, training) — the environment cannot sustain
  this, causing OOM kills and total failure. This is the #1 cause of failure.
- Writing to a file the other agent already created. Once the other agent
  has produced a file, do NOT modify it. If you think it is wrong, explain
  why in the log and let them decide — "fixing" correct work with an
  incorrect approach has destroyed correct solutions repeatedly.
- Acting on assumptions about what the other agent is doing without reading
  the log
- Taking over the other agent's work based on a faulty sense of elapsed
  time — check actual timestamps in the log before deciding
- Rubber-stamping: if you are reviewing, form your own complete answer
  independently BEFORE looking at the other agent's output, then compare.
  Simply reading their output and confirming it "looks right" misses errors.
- Blocking in a loop waiting for the other agent instead of doing useful work

Disagreements about the right approach are valuable — they usually mean one
of you has noticed something the other missed. If your analysis contradicts
the other agent's, say so in the log and resolve the disagreement before
producing final output.
```

### Iteration History

| Iter | extract-elf | rstan | Key Change | Diagnosis |
|------|------------|-------|------------|-----------|
| 0 | 3/5 (0.60) | 1/5 (0.20) | Baseline unified prompt | rstan: agents skip coordination, both compile Stan → OOM |
| 1 | 2/5 (0.40) | 4/5 (0.80) | Added "skipping coordination" pitfall, positive framing about sharing plans | extract-elf: agents overwrite each other's correct files |
| 2 | 3/5 (0.60) | 2/5 (0.40) | Refined file overwrite warning to cover "updating/fixing" | rstan variance due to resource contention at -n 5 |
| 3 | 3/5 (0.60) | 5/5* (1.00) | Promoted OOM to #1 pitfall, "write to log before starting" | *rstan at -n 2 (isolated); 0/5 at -n 5 due to infra contention |
| 4 | 5/5 (1.00) | 5/5* (1.00) | "Do NOT modify other's file", "form own answer BEFORE looking" | Focused test: extract-elf and sparql both 5/5 |

### Full Benchmark Results (Iteration 4)

| Task | Unified | v3 Duo | Delta |
|------|---------|--------|-------|
| chess-best-move | 0.00 | 0.20 | -0.20 |
| circuit-fibsqrt | 0.00 | 0.00 | = |
| compile-compcert | 0.20 | 0.60 | -0.40 |
| **extract-elf** | **0.80** | 0.40 | **+0.40** |
| git-leak-recovery | 1.00 | 1.00 | = |
| multi-source-data-merger | 1.00 | 1.00 | = |
| path-tracing | 0.00 | 0.00 | = |
| rstan-to-pystan | 0.60 | 1.00 | -0.40 |
| sanitize-git-repo | 1.00 | 1.00 | = |
| **sparql-university** | **1.00** | 0.80 | **+0.20** |
| sqlite-db-truncate | 1.00 | 1.00 | = |
| torch-tensor-parallelism | 0.60 | 0.60 | = |
| **Aggregate** | **0.60** | **0.63** | **-0.03** |

### Collaboration Log Analysis

**Failure modes fixed by the unified prompt (iter4 vs iter0):**

1. **File overwrites eliminated on extract-elf**: In iter0-2, agent p or q would "update" the other's correct extract.js with a wrong BASE=0x400000 approach. The iter4 warning "do NOT modify it... 'fixing' correct work with an incorrect approach has destroyed correct solutions repeatedly" stopped this pattern. extract-elf improved from 0.40 to 0.80.

2. **Rubber-stamping reduced on sparql-university**: In the v3 duo, both agents would converge on subtly wrong SPARQL queries without catching errors. The iter4 instruction to "form your own complete answer independently BEFORE looking at the other agent's output" led to more genuine independent verification. sparql improved from 0.80 to 1.00.

**Failure modes NOT fixed:**

3. **Resource contention on rstan-to-pystan and compile-compcert**: Both tasks require expensive compilation (Stan model / CompCert compiler). Without role assignments, both agents race to start compilation simultaneously, causing OOM. The OOM warning helps when agents read the log, but in many trials agents don't coordinate at all (empty collab logs). At low concurrency (-n 2), rstan achieves 5/5; the failures are infrastructure-level, not prompt-level.

4. **Technical errors on extract-elf**: Even with good coordination, some trials fail because both agents independently make the same technical mistake (applying BASE=0x400000 to a PIE binary). This is an algorithmic error the prompt cannot fix without task-specific hints.

### Key Finding

The unified prompt's strength is preventing destructive collaboration (file overwrites, rubber-stamping). Its weakness is enabling constructive coordination on resource-intensive tasks that need sequential execution. The v3 A/B split solved this with role assignments and polling loops; the unified prompt cannot replicate this within its design constraints.

**Net effect**: +0.40 on extract-elf, +0.20 on sparql-university, -0.40 on rstan-to-pystan, -0.40 on compile-compcert. The gains and losses roughly cancel, yielding a similar aggregate (0.60 vs 0.63).
