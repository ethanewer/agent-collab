#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

set -a
source .env
set +a

MODEL="anthropic/claude-sonnet-4-6"
N_CONCURRENT=8

TASKS="chess-best-move:1 circuit-fibsqrt:5 compile-compcert:3 extract-elf:1 git-leak-recovery:1 multi-source-data-merger:2 sanitize-git-repo:1 sparql-university:2 sqlite-db-truncate:2 torch-tensor-parallelism:1"

echo "==> Rerunning CONTROL failed trials (OOM during install)"
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
    --jobs-dir "jobs/rerun-control" \
    -k "${k}" \
    -n "${N_CONCURRENT}" \
    --agent-import-path "control_agent:ControlClaudeCode" \
    -m "${MODEL}" \
    --ae "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" \
    -i "${task}"
  echo ""
done

echo "==> Control reruns complete. Results in jobs/rerun-control/"
