#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

set -a
source .env
set +a

MODEL="anthropic/claude-sonnet-4-6"
N_CONCURRENT=8

# Rerun counts based on failed/errored trials from the initial run.
# Format: task:k where k = number of additional runs needed to reach 5 good trials.
TASKS="chess-best-move:5 circuit-fibsqrt:5 compile-compcert:5 extract-elf:3 git-leak-recovery:4 multi-source-data-merger:3 path-tracing:5 rstan-to-pystan:4 sanitize-git-repo:2 sparql-university:3 sqlite-db-truncate:3 torch-tensor-parallelism:3"

echo "==> Rerunning PLAN failed trials (disk space errors during install)"
echo "    Model:       ${MODEL}"
echo "    Concurrency: ${N_CONCURRENT}"
echo ""

for entry in ${TASKS}; do
  task="${entry%%:*}"
  k="${entry##*:}"
  echo "--- ${task} (k=${k}) ---"
  harbor run \
    -d "terminal-bench@2.0" \
    --env docker \
    --no-force-build \
    --no-delete \
    --jobs-dir "jobs/rerun-plan" \
    -k "${k}" \
    -n "${N_CONCURRENT}" \
    --agent-import-path "plan_agent:PlanClaudeCode" \
    -m "${MODEL}" \
    --ae "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" \
    -i "${task}"
  echo ""
done

echo "==> Plan reruns complete. Results in jobs/rerun-plan/"
