# Two Claude Code Agents Are Better Than One

Two Claude Code instances collaborating on the same task outperform a single instance on Terminal Bench 2.0, achieving a **0.65 pass rate** vs **0.46 for a single agent** — with only a short, outcome-focused collaboration prompt.

## Setup

Two Claude Code 2.1.92 instances (`claude-sonnet-4-6`) run concurrently in the same Docker container via [Harbor](https://github.com/cthulhoo/harbor) 0.3.0. Each instance has its own `CLAUDE_CONFIG_DIR` to avoid session conflicts. They share an append-only log file for coordination. The trial succeeds if at least one agent exits with code 0.

Both agents receive the same system prompt, differing only in a one-character identity placeholder (`p` or `q`):

```
You are agent {id}, one of two AI agents working on the same task in the same
environment.

You share an append-only log file at {collab_file} for communication.
- Write: echo "[{id}|$(date -u +%H:%M:%S)] <msg>" >> {collab_file}
- Read: cat {collab_file}

Communicate constantly. Always be aware of what the other agent has done,
is currently doing, and plans to do.

Your goal is to produce better results together than either of you could
alone. Divide work, verify independently, and build on each other's
contributions. In every successful collaboration, agents built on each
other's work. In every failed one, an agent replaced the other's work
— confident they were improving it, but wrong. When you disagree, talk
it out in the log.
```

The prompt is intentionally outcome-focused: it describes what successful and failed collaborations look like, but does not prescribe specific behaviors (no checklists, no "do NOT" rules, no task-specific warnings). Agents discover their own coordination strategies.

## Results

5 trials per task, 60 total. Control is a single Claude Code instance, 10 trials per task, 120 total. "Control (pass@2)" estimates the probability of at least one success in two independent single-agent attempts — a fairer cost comparison since the duo uses ~2x compute.

| Task | Control | Control (pass@2) | Duo |
|------|---------|------------------|-----|
| chess-best-move | 0.00 | 0.00 | 0.00 |
| circuit-fibsqrt | 0.00 | 0.00 | 0.00 |
| compile-compcert | 0.00 | 0.00 | **0.60** |
| extract-elf | 0.70 | 0.93 | 0.20 |
| git-leak-recovery | 1.00 | 1.00 | 1.00 |
| multi-source-data-merger | 1.00 | 1.00 | 1.00 |
| path-tracing | 0.00 | 0.00 | 0.00 |
| rstan-to-pystan | 0.10 | 0.20 | **1.00** |
| sanitize-git-repo | 0.20 | 0.38 | **1.00** |
| sparql-university | 1.00 | 1.00 | 1.00 |
| sqlite-db-truncate | 1.00 | 1.00 | 1.00 |
| torch-tensor-parallelism | 0.50 | 0.78 | **1.00** |
| **Aggregate** | **0.46** | **0.52** | **0.65** |

The duo achieves **7 perfect scores** vs 3 for the control, and beats the pass@2 estimate (0.65 vs 0.52), suggesting collaboration provides value beyond simply having two independent attempts.

## Where collaboration helps

- **rstan-to-pystan (0.10 → 1.00)**: Agents divide the R-to-Python translation and catch each other's bugs — PyStan API mismatches, parameter errors, and resource management issues that a single agent misses.
- **sanitize-git-repo (0.20 → 1.00)**: One agent identifies what needs sanitizing while the other implements and verifies, catching edge cases in git history rewriting.
- **torch-tensor-parallelism (0.50 → 1.00)**: Agents divide the parallelism problem naturally and verify each other's tensor sharding logic.
- **compile-compcert (0.00 → 0.60)**: CompCert compilation requires careful sequencing. Agents coordinate build steps through the log, with one monitoring while the other compiles.

## Where collaboration hurts

- **extract-elf (0.70 → 0.20)**: Both agents independently make the same technical error (applying base address 0x400000 to a PIE binary), and the second agent often overwrites the first's correct solution with an incorrect "fix." The outcome-focused prompt discourages this ("an agent replaced the other's work — confident they were improving it, but wrong") but is less effective than an explicit prohibition would be.

## Prompt design

The prompt went through several iterations. Earlier versions used prescriptive rules — numbered checklists, explicit "do NOT overwrite" prohibitions, OOM warnings — which improved specific tasks (extract-elf) but constrained agents on tasks where the rules weren't needed. The final outcome-focused version scores higher overall because it lets agents apply their own judgment, leading to more natural and effective collaboration on the majority of tasks.

The key line — *"In every successful collaboration, agents built on each other's work. In every failed one, an agent replaced the other's work — confident they were improving it, but wrong"* — encodes the single most important lesson from experimentation: agents that build on each other's contributions outperform agents that overwrite them, even when the overwriting agent is confident they're right.

## Reproduction

```bash
# Requires: Harbor 0.3.0, Docker, ANTHROPIC_API_KEY in .env
source .env
harbor run \
  -d "terminal-bench@2.0" \
  --env docker \
  --no-force-build \
  --no-delete \
  --jobs-dir "jobs/duo" \
  -k 5 -n 6 \
  --agent-import-path "duo_agent:DuoClaudeCode" \
  -m "anthropic/claude-sonnet-4-6" \
  --ae "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" \
  -i "extract-elf" -i "rstan-to-pystan"  # or any subset of tasks
```
