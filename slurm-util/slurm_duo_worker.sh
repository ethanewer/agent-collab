#!/usr/bin/env bash
#SBATCH --partition=m7i-cpu
#SBATCH --nodes=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=60G
#SBATCH --time=24:00:00

# Runs opus-duo then opus-sonnet-duo on the same shard.
# Uses a background cache manager to save images to the shared volume
# and evict them when disk is low. Before experiment 2, reloads from cache.
#
# Arguments: <shard_id> <task_file>

set -euo pipefail

SHARD_ID="$1"
TASK_FILE="$2"
IMAGE_CACHE="/wbl-fast/usrs/ee/agent-collab/docker-image-cache"
DISK_LOW_KB=3145728  # 3GB

SCRIPT_DIR="/wbl-fast/usrs/ee/agent-collab"
cd "${SCRIPT_DIR}"

source .venv/bin/activate
set -a
source .env
set +a

NODE=$(hostname)
mkdir -p "${IMAGE_CACHE}"

echo "==> shard-${SHARD_ID} on ${NODE} at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

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

# Background cache manager: save images to shared cache, evict when disk low.
# Only evicts images not referenced by running containers.
cache_manager() {
  while true; do
    # Save any unsaved images
    for img in $(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep "alexgshaw/"); do
      safe=$(echo "$img" | tr '/:' '_')
      if [ ! -f "${IMAGE_CACHE}/${safe}.tar" ]; then
        if docker save "$img" -o "${IMAGE_CACHE}/${safe}.tar.tmp" 2>/dev/null; then
          mv "${IMAGE_CACHE}/${safe}.tar.tmp" "${IMAGE_CACHE}/${safe}.tar"
        else
          rm -f "${IMAGE_CACHE}/${safe}.tar.tmp"
        fi
      fi
    done

    # If disk is low, remove images not used by running containers
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

# Load any pre-existing cached images for our shard
echo "Loading cached images..."
while IFS= read -r task; do
  for tar in "${IMAGE_CACHE}"/alexgshaw_${task}_*.tar; do
    if [ -f "$tar" ]; then
      if ! docker image inspect "alexgshaw/${task}:20251031" &>/dev/null; then
        echo "  Loading: $(basename $tar)"
        docker load -i "$tar" 2>/dev/null || true
      fi
    fi
  done
done < "${TASK_FILE}"

# --- Experiment 1: opus-duo ---
EXP1="opus-duo"
JOB1="${EXP1}-shard-${SHARD_ID}"
echo ""
echo "=== ${JOB1}: starting at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

harbor run -y \
  -d "terminal-bench@2.0" \
  --env docker \
  --no-delete \
  --jobs-dir "jobs/${EXP1}/${JOB1}" \
  -k 5 -n 4 \
  --agent-import-path "duo_opus_agent:DuoOpusClaudeCode" \
  -m "anthropic/claude-opus-4-6" \
  --ae "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" \
  "${TASK_FLAGS[@]}"

echo "=== ${JOB1}: done at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
echo "Disk free: $(df -h /dev/root | tail -1 | awk '{print $4}')"

# Wait for cache manager to save remaining images
echo "Waiting for cache manager to save images..."
sleep 30

# Reload all shard images from cache before experiment 2
echo "Reloading images from cache for experiment 2..."
while IFS= read -r task; do
  for tar in "${IMAGE_CACHE}"/alexgshaw_${task}_*.tar; do
    if [ -f "$tar" ]; then
      if ! docker image inspect "alexgshaw/${task}:20251031" &>/dev/null; then
        echo "  Loading: $(basename $tar)"
        docker load -i "$tar" 2>/dev/null || true
      fi
    fi
  done
done < "${TASK_FILE}"

# --- Experiment 2: opus-sonnet-duo ---
EXP2="opus-sonnet-duo"
JOB2="${EXP2}-shard-${SHARD_ID}"
echo ""
echo "=== ${JOB2}: starting at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

harbor run -y \
  -d "terminal-bench@2.0" \
  --env docker \
  --no-delete \
  --jobs-dir "jobs/${EXP2}/${JOB2}" \
  -k 5 -n 4 \
  --agent-import-path "duo_mixed_agent:DuoMixedClaudeCode" \
  -m "anthropic/claude-opus-4-6" \
  --ae "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}" \
  "${TASK_FLAGS[@]}"

echo "=== ${JOB2}: done at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# Final cache save
echo "Final image cache save..."
sleep 20
echo "Cached tars: $(ls ${IMAGE_CACHE}/*.tar 2>/dev/null | wc -l)"
echo "==> shard-${SHARD_ID} fully complete at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
