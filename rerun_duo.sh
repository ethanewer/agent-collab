#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

set -a
source .env
set +a

MODEL="anthropic/claude-sonnet-4-6"
N_CONCURRENT=3

TASKS="multi-source-data-merger:1 path-tracing:1 torch-tensor-parallelism:1"

echo "==> Rerunning DUO failed trials (OOM during install)"
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
    --jobs-dir "jobs/rerun-duo" \
    -k "${k}" \
    -n "${N_CONCURRENT}" \
    --agent-import-path "duo_agent:DuoClaudeCode" \
    -m "${MODEL}" \
    --ae "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" \
    -i "${task}"
  echo ""
done

echo "==> Duo reruns complete. Results in jobs/rerun-duo/"
