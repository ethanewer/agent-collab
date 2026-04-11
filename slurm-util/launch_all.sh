#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="/wbl-fast/usrs/ee/agent-collab"
cd "${SCRIPT_DIR}"

# All 89 tasks
TASKS=(
  adaptive-rejection-sampler bn-fit-modify break-filter-js-from-html build-cython-ext
  build-pmars build-pov-ray caffe-cifar-10 cancel-async-tasks chess-best-move
  circuit-fibsqrt cobol-modernization code-from-image compile-compcert
  configure-git-webserver constraints-scheduling count-dataset-tokens crack-7z-hash
  custom-memory-heap-crash db-wal-recovery distribution-search dna-assembly dna-insert
  extract-elf extract-moves-from-video feal-differential-cryptanalysis
  feal-linear-cryptanalysis filter-js-from-html financial-document-processor
  fix-code-vulnerability fix-git fix-ocaml-gc gcode-to-text git-leak-recovery
  git-multibranch gpt2-codegolf headless-terminal hf-model-inference
  install-windows-3.11 kv-store-grpc large-scale-text-editing largest-eigenval
  llm-inference-batching-scheduler log-summary-date-ranges mailman make-doom-for-mips
  make-mips-interpreter mcmc-sampling-stan merge-diff-arc-agi-task
  model-extraction-relu-logits modernize-scientific-stack mteb-leaderboard mteb-retrieve
  multi-source-data-merger nginx-request-logging openssl-selfsigned-cert overfull-hbox
  password-recovery path-tracing path-tracing-reverse polyglot-c-py polyglot-rust-c
  portfolio-optimization protein-assembly prove-plus-comm pypi-server pytorch-model-cli
  pytorch-model-recovery qemu-alpine-ssh qemu-startup query-optimize raman-fitting
  regex-chess regex-log reshard-c4-data rstan-to-pystan sam-cell-seg sanitize-git-repo
  schemelike-metacircular-eval sparql-university sqlite-db-truncate sqlite-with-gcov
  torch-pipeline-parallelism torch-tensor-parallelism train-fasttext tune-mjcf
  video-processing vulnerable-secret winning-avg-corewars write-compressor
)

TOTAL=${#TASKS[@]}
SHARDS=5
PER_SHARD=$(( (TOTAL + SHARDS - 1) / SHARDS ))

# Write task files
TASK_DIR="${SCRIPT_DIR}/task-shards"
mkdir -p "${TASK_DIR}"
rm -f "${TASK_DIR}"/*.txt

for s in $(seq 0 $((SHARDS - 1))); do
  START=$((s * PER_SHARD))
  END=$((START + PER_SHARD))
  [ "$END" -gt "$TOTAL" ] && END=$TOTAL
  TASK_FILE="${TASK_DIR}/shard-${s}.txt"
  for i in $(seq $START $((END - 1))); do
    echo "${TASKS[$i]}" >> "$TASK_FILE"
  done
  echo "Shard $s: $(wc -l < "$TASK_FILE") tasks"
done

echo ""

# Cancel reserve jobs to free nodes
echo "Cancelling reserve jobs..."
squeue -u $USER -o "%i %j" --noheader | grep reserve | awk '{print $1}' | xargs -r scancel 2>/dev/null || true
sleep 2

declare -a EXP_NAMES=("opus-duo" "opus-sonnet-duo" "terminus2-duo")
declare -a AGENT_IMPORTS=("duo_opus_agent:DuoOpusClaudeCode" "duo_mixed_agent:DuoMixedClaudeCode" "duo_terminus2_agent:DuoTerminus2")

for e in 0 1 2; do
  EXP="${EXP_NAMES[$e]}"
  AGENT="${AGENT_IMPORTS[$e]}"
  mkdir -p "jobs/${EXP}"

  echo "=== Submitting ${EXP} (5 shards) ==="
  for s in $(seq 0 $((SHARDS - 1))); do
    JOB_NAME="${EXP}-shard-${s}"
    LOG_DIR="jobs/${EXP}"
    TASK_FILE="${TASK_DIR}/shard-${s}.txt"

    JOB_ID=$(sbatch \
      --job-name="${JOB_NAME}" \
      --output="${LOG_DIR}/${JOB_NAME}.log" \
      --error="${LOG_DIR}/${JOB_NAME}.err" \
      slurm_worker.sh "${EXP}" "${s}" "${AGENT}" "${TASK_FILE}" \
      2>&1 | awk '{print $4}')

    echo "  ${JOB_NAME}: job ${JOB_ID} ($(wc -l < "$TASK_FILE") tasks)"
  done
  echo ""
done

echo "All 15 jobs submitted."
