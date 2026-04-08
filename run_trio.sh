#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

set -a
source .env
set +a

N_ATTEMPTS=5
N_CONCURRENT=4  # Low: each task runs 3 Claude Code instances
MODEL="anthropic/claude-sonnet-4-6"
TASKS=(
  chess-best-move
  circuit-fibsqrt
  compile-compcert
  extract-elf
  git-leak-recovery
  multi-source-data-merger
  path-tracing
  rstan-to-pystan
  sanitize-git-repo
  sparql-university
  sqlite-db-truncate
  torch-tensor-parallelism
)

CMD=(
  harbor run
  -d "terminal-bench@2.0"
  --env docker
  --no-force-build
  --no-delete
  --jobs-dir "jobs/full-trio"
  -k "${N_ATTEMPTS}"
  -n "${N_CONCURRENT}"
  --agent-import-path "trio_agent:TrioClaudeCode"
  -m "${MODEL}"
  --ae "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}"
)

for task in "${TASKS[@]}"; do
  CMD+=(-i "${task}")
done

echo "==> Running TRIO (three Claude Codes) on terminal-bench@2.0"
echo "    Model:       ${MODEL}"
echo "    Attempts:    ${N_ATTEMPTS}"
echo "    Concurrency: ${N_CONCURRENT}"
echo "    Tasks:       ${TASKS[*]}"
echo ""

"${CMD[@]}"

echo ""
echo "==> Trio benchmark complete. Results in jobs/full-trio/"
