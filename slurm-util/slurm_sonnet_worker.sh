#!/usr/bin/env bash
#SBATCH --partition=m7i-cpu
#SBATCH --nodes=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=60G
#SBATCH --time=24:00:00

# Runs opus-sonnet-duo only. Loads images from shared cache first.
# Arguments: <shard_id> <task_file>

set -euo pipefail

SHARD_ID="$1"
TASK_FILE="$2"
IMAGE_CACHE="/wbl-fast/usrs/ee/agent-collab/docker-image-cache"
DISK_LOW_KB=3145728

SCRIPT_DIR="/wbl-fast/usrs/ee/agent-collab"
cd "${SCRIPT_DIR}"

source .venv/bin/activate
set -a
source .env
set +a

NODE=$(hostname)
mkdir -p "${IMAGE_CACHE}"

echo "==> sonnet-shard-${SHARD_ID} on ${NODE} at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Docker setup
sudo bash -c 'cat > /etc/docker/daemon.json <<JSON
{
    "runtimes": {"nvidia": {"args": [], "path": "nvidia-container-runtime"}},
    "default-address-pools": [
        {"base": "172.17.0.0/12", "size": 24},
        {"base": "192.168.0.0/16", "size": 24},
        {"base": "10.0.0.0/8", "size": 24}
    ]
}
JSON
systemctl restart docker 2>/dev/null || true'

echo "${DOCKER_PAT_2}" | docker login -u "${DOCKER_USERNAME_2}" --password-stdin 2>&1 | tail -1
echo "Disk free: $(df -h /dev/root | tail -1 | awk '{print $4}')"

mapfile -t TASKS < "${TASK_FILE}"
echo "Tasks in shard: ${#TASKS[@]}"

TASK_FLAGS=()
for task in "${TASKS[@]}"; do
  TASK_FLAGS+=(-i "${task}")
done

# Background cache manager
cache_manager() {
  while true; do
    for img in $(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep "alexgshaw/"); do
      safe=$(echo "$img" | tr '/:' '_')
      if [ ! -f "${IMAGE_CACHE}/${safe}.tar" ]; then
        if docker save "$img" -o "${IMAGE_CACHE}/${safe}.tar.tmp" 2>/dev/null; then
          mv "${IMAGE_CACHE}/${safe}.tar.tmp" "${IMAGE_CACHE}/${safe}.tar"
        fi
      fi
    done
    FREE_KB=$(df /dev/root | tail -1 | awk '{print $4}')
    if [ "$FREE_KB" -lt "$DISK_LOW_KB" ]; then
      RUNNING_IMGS=$(docker ps --format '{{.Image}}' 2>/dev/null | sort -u)
      for img in $(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep "alexgshaw/"); do
        if ! echo "$RUNNING_IMGS" | grep -qF "$img"; then
          docker rmi "$img" >/dev/null 2>&1 && echo "[cache-mgr] Evicted: $img"
        fi
      done
    fi
    sleep 10
  done
}
cache_manager &
CACHE_PID=$!
trap "kill $CACHE_PID 2>/dev/null; wait $CACHE_PID 2>/dev/null" EXIT

# Load cached images for our shard
echo "Loading cached images..."
LOADED=0
while IFS= read -r task; do
  for tar in "${IMAGE_CACHE}"/alexgshaw_${task}_*.tar; do
    if [ -f "$tar" ]; then
      if ! docker image inspect "alexgshaw/${task}:20251031" &>/dev/null; then
        docker load -i "$tar" 2>/dev/null && LOADED=$((LOADED+1)) || true
      else
        LOADED=$((LOADED+1))
      fi
    fi
  done
done < "${TASK_FILE}"
echo "Loaded/cached: ${LOADED}/${#TASKS[@]} images"
echo "Disk free: $(df -h /dev/root | tail -1 | awk '{print $4}')"

# Run opus-sonnet-duo
EXP="opus-sonnet-duo"
JOB_NAME="${EXP}-shard-${SHARD_ID}"
echo ""
echo "=== ${JOB_NAME}: starting at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

harbor run -y \
  -d "terminal-bench@2.0" \
  --env docker \
  --no-delete \
  --jobs-dir "jobs/${EXP}/${JOB_NAME}" \
  -k 5 -n 4 \
  --agent-import-path "duo_mixed_agent:DuoMixedClaudeCode" \
  -m "anthropic/claude-opus-4-6" \
  --ae "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" \
  "${TASK_FLAGS[@]}"

echo "=== ${JOB_NAME}: done at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# Final save
sleep 15
echo "Cache tars: $(ls ${IMAGE_CACHE}/*.tar 2>/dev/null | wc -l)"
echo "==> sonnet-shard-${SHARD_ID} complete at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
