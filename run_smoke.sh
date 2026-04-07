#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# Load API key
set -a
source .env
set +a

MODEL="anthropic/claude-sonnet-4-6"
SMOKE_TASK="extract-elf"

echo "============================================"
echo " Smoke Test: Control (single Claude Code)"
echo " Task: ${SMOKE_TASK}"
echo " Model: ${MODEL}"
echo "============================================"

harbor run \
  -d "terminal-bench@2.0" \
  --env docker \
  --no-force-build \
  --no-delete \
  --jobs-dir "jobs/smoke-control" \
  -k 1 \
  -n 1 \
  --agent-import-path "control_agent:ControlClaudeCode" \
  -m "${MODEL}" \
  --ae "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" \
  -i "${SMOKE_TASK}"

echo ""
echo "============================================"
echo " Smoke Test: Duo (two Claude Codes)"
echo " Task: ${SMOKE_TASK}"
echo " Model: ${MODEL}"
echo "============================================"

harbor run \
  -d "terminal-bench@2.0" \
  --env docker \
  --no-force-build \
  --no-delete \
  --jobs-dir "jobs/smoke-duo" \
  -k 1 \
  -n 1 \
  --agent-import-path "duo_agent:DuoClaudeCode" \
  -m "${MODEL}" \
  --ae "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" \
  -i "${SMOKE_TASK}"

echo ""
echo "============================================"
echo " Smoke Test Complete"
echo "============================================"

echo ""
echo "Results:"
CONTROL_JOB="$(ls -td jobs/smoke-control/*/ 2>/dev/null | head -1)"
DUO_JOB="$(ls -td jobs/smoke-duo/*/ 2>/dev/null | head -1)"

if [[ -n "${CONTROL_JOB}" ]]; then
  echo "  Control job: ${CONTROL_JOB}"
  find "${CONTROL_JOB}" -name "result.json" -exec echo "  Result:" \; -exec python3 -c "
import json, sys
r = json.load(open(sys.argv[1]))
reward = r.get('verifier_result', {}).get('rewards', {}).get('reward', 'N/A')
print(f'    Task: {r.get(\"task_name\")}, Reward: {reward}')
" {} \;
fi

if [[ -n "${DUO_JOB}" ]]; then
  echo "  Duo job: ${DUO_JOB}"
  find "${DUO_JOB}" -name "result.json" -exec echo "  Result:" \; -exec python3 -c "
import json, sys
r = json.load(open(sys.argv[1]))
reward = r.get('verifier_result', {}).get('rewards', {}).get('reward', 'N/A')
print(f'    Task: {r.get(\"task_name\")}, Reward: {reward}')
" {} \;

  echo ""
  echo "Collaboration logs:"
  find "${DUO_JOB}" -name "agent-collab.log" | while read -r f; do
    echo "  Log file: ${f}"
    echo "  --- contents ---"
    cat "${f}"
    echo "  --- end ---"
  done
fi
