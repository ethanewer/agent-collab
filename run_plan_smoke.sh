#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

set -a
source .env
set +a

MODEL="anthropic/claude-sonnet-4-6"
SMOKE_TASK="sqlite-db-truncate"

echo "============================================"
echo " Smoke Test: Plan-then-Build (Claude Code)"
echo " Task: ${SMOKE_TASK}"
echo " Model: ${MODEL}"
echo "============================================"

harbor run \
  -d "terminal-bench@2.0" \
  --env docker \
  --no-force-build \
  --no-delete \
  --jobs-dir "jobs/smoke-plan" \
  -k 1 \
  -n 1 \
  --agent-import-path "plan_agent:PlanClaudeCode" \
  -m "${MODEL}" \
  --ae "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" \
  -i "${SMOKE_TASK}"

echo ""
echo "============================================"
echo " Smoke Test Complete"
echo "============================================"

echo ""
echo "Results:"
PLAN_JOB="$(ls -td jobs/smoke-plan/*/ 2>/dev/null | head -1)"

if [[ -n "${PLAN_JOB}" ]]; then
  echo "  Plan job: ${PLAN_JOB}"
  while IFS= read -r rf; do
    python3 -c "
import json, sys
r = json.load(open(sys.argv[1]))
reward = r.get('verifier_result', {}).get('rewards', {}).get('reward', 'N/A')
print(f'  Result: Task={r.get(\"task_name\")}, Reward={reward}')
" "$rf"
  done < <(find "${PLAN_JOB}" -name "result.json" -not -path "*/2026-*/result.json" | sort)

  echo ""
  echo "Plan outputs:"
  find "${PLAN_JOB}" -name "plan-output.json" | while read -r f; do
    echo "  File: ${f}"
    python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
print(f'    Session: {d.get(\"session_id\", \"N/A\")}')
print(f'    Subtype: {d.get(\"subtype\", \"N/A\")}')
print(f'    Cost:    \${d.get(\"total_cost_usd\", 0)}')
print(f'    Turns:   {d.get(\"num_turns\", 0)}')
denials = d.get('permission_denials', [])
plan_denial = next((x for x in reversed(denials) if 'plan' in x.get('tool_input', {})), None)
if plan_denial:
    plan = plan_denial['tool_input']['plan']
    print(f'    Plan:    {len(plan)} chars')
else:
    print(f'    Plan:    NOT FOUND (denials={len(denials)})')
" "$f"
  done
fi
