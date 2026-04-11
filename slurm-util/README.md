# Terminal-Bench 2.0 SLURM Experiment Runner

How to run duo agent experiments on the SLURM cluster with Terminal-Bench 2.0.

## Cluster constraints

| Resource | Value | Notes |
|----------|-------|-------|
| Partition | `m7i-cpu` | CPU-only nodes |
| Node disk | 44 GB root (`/dev/root`) | ~16 GB free after OS |
| Node RAM | 60 GB | |
| Shared volume | `/wbl-fast` (1.5 PB Lustre) | Visible from all nodes |
| Docker storage | `overlay2` on local `/var/lib/docker` | Lustre is **incompatible** with overlay2, fuse-overlayfs, and vfs |
| Docker Hub rate limit | 200 pulls / 6 hours (authenticated, per account) | |

The small root disk (16 GB free) is the binding constraint. Docker images for
Terminal-Bench tasks range from ~100 MB to ~2 GB each. At most 6–10 images fit
on disk at once, depending on the shard.

## Architecture: sharded multi-node with image cache

The 89 Terminal-Bench 2.0 tasks are split into 5 **shards** of ~18 tasks each.
Each shard runs on its own SLURM node with `N_CONCURRENT=4` (4 trials in
parallel). This gives an effective concurrency of **20 per experiment**.

```
                    ┌─── Node 2:  shard-0  (18 tasks × 5 attempts, 4 concurrent)
                    ├─── Node 3:  shard-1  (18 tasks × 5 attempts, 4 concurrent)
  Experiment ──────>├─── Node 5:  shard-2  (18 tasks × 5 attempts, 4 concurrent)
                    ├─── Node 6:  shard-3  (18 tasks × 5 attempts, 4 concurrent)
                    └─── Node 7:  shard-4  (17 tasks × 5 attempts, 4 concurrent)
```

### Image cache manager

Each worker runs a **background cache manager** that:

1. **Saves** every pulled Docker image as a `.tar` file to the shared volume
   (`docker-image-cache/`) as soon as it appears locally.
2. **Evicts** images not referenced by running containers when free disk drops
   below 3 GB, using targeted `docker rmi` (not `docker image prune`, which
   corrupts containerd mid-pull).
3. **Loads** cached images from the shared volume at startup, avoiding
   redundant Docker Hub pulls.

This means each unique image is pulled from Docker Hub **at most once** across
all nodes. Subsequent nodes (or subsequent experiments on the same node) load
from the shared cache. Total pulls for the full 89-task benchmark ≈ 89, well
within the 200/6hr authenticated limit.

### Running two experiments sequentially on the same nodes

`slurm_duo_worker.sh` runs **two experiments in sequence** on the same node
(opus-duo, then opus-sonnet-duo). The second experiment reuses cached images
from the first — zero additional Docker Hub pulls. This is the most
rate-limit-efficient approach.

`slurm_sonnet_worker.sh` runs **only opus-sonnet-duo** and is used when
launching the second experiment in parallel on separate nodes. It loads images
from the shared cache first, then pulls only missing ones.

## Prerequisites

### Python environment

```bash
# Harbor requires Python 3.12. Cluster default is 3.10.
# A venv is already set up at .venv/
source .venv/bin/activate
harbor --help
```

If recreating:

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install harbor-bench
```

### Environment variables

Required in `.env`:

```
ANTHROPIC_API_KEY=...

# Docker Hub credentials (authenticated = 200 pulls/6hr instead of 100)
DOCKER_USERNAME_2=...
DOCKER_PAT_2=...
```

### Task cache

```bash
# Download terminal-bench tasks (only needed once, cached to .cache/)
harbor datasets download terminal-bench@2.0
```

## How to run experiments

### 1. Generate task shards

```bash
# Creates task-shards/shard-{0..4}.txt, each with ~18 task names
bash launch_all.sh
# (This also submits jobs — see below for manual control)
```

Or generate shards manually — any text file with one task name per line works.

### 2. Submit jobs

**Option A: Combined worker (2 experiments, 1 set of nodes)**

Runs opus-duo then opus-sonnet-duo sequentially on 5 nodes.
Only 89 Docker Hub pulls total.

```bash
for s in 0 1 2 3 4; do
  sbatch \
    --job-name="duo-shard-${s}" \
    --output="jobs/duo-shard-${s}.log" \
    --error="jobs/duo-shard-${s}.err" \
    slurm_duo_worker.sh "${s}" "task-shards/shard-${s}.txt"
done
```

**Option B: Separate workers (parallel on different nodes)**

When you have image cache already populated and want to run the second
experiment immediately:

```bash
for s in 0 1 2 3 4; do
  sbatch \
    --job-name="sonnet-shard-${s}" \
    --output="jobs/sonnet-shard-${s}.log" \
    --error="jobs/sonnet-shard-${s}.err" \
    slurm_sonnet_worker.sh "${s}" "task-shards/shard-${s}.txt"
done
```

### 3. Monitor progress

```bash
# Job status
squeue -u $USER -o "%.10i %.25j %.2t %.10M %.30R" | grep shard

# Per-shard results
for s in 0 1 2 3 4; do
  dir="jobs/opus-duo/opus-duo-shard-${s}"
  latest=$(ls -td "${dir}"/2026* 2>/dev/null | head -1)
  [ -f "$latest/result.json" ] && python3 -c "
import json
r = json.load(open('$latest/result.json'))
for k, v in r['stats'].get('evals', {}).items():
    n, e = v['n_trials'], v['n_errors']
    print(f'shard-$s: {n+e}/90 done, {n} ok, {e} errors')
"
done

# Disk health
for jid in $(squeue -u $USER -o "%i %j" --noheader | grep shard | awk '{print $1}'); do
  srun --jobid=$jid --overlap bash -c \
    "echo \"\$(hostname): \$(df -h /dev/root | tail -1 | awk '{print \$4}') free, imgs=\$(docker images -q | wc -l)\""
done

# Image cache status
ls docker-image-cache/*.tar | wc -l
```

### 4. Merge results

Each shard produces its own `result.json` under
`jobs/<experiment>/<experiment>-shard-<N>/<timestamp>/result.json`.
Use `show_results.py` to aggregate across shards.

## Agent definitions

| Agent class | File | Description |
|-------------|------|-------------|
| `DuoOpusClaudeCode` | `duo_opus_agent.py` | Two Claude Code instances, both Opus 4.6 |
| `DuoMixedClaudeCode` | `duo_mixed_agent.py` | Two Claude Code instances: agent P = Opus, agent Q = Sonnet |
| `DuoTerminus2` | `duo_terminus2_agent.py` | Two Terminus-2 instances (harbor's built-in tmux agent) |

All duo agents use the same collaboration protocol: an append-only shared log
file at `/tmp/agent-collab.log` inside the Docker container. Agents write
timestamped messages and read the log to coordinate.

## Output structure

```
jobs/
  opus-duo/
    opus-duo-shard-0/
      2026-04-11__03-39-33/
        result.json              # Aggregated results for this shard
        task-name__abc1234/      # Per-trial directory
          result.json
          trial.log
          agent/                 # Agent logs, collab log
    opus-duo-shard-1/
      ...
  opus-sonnet-duo/
    opus-sonnet-duo-shard-0/
      ...
  duo-shard-0.log               # SLURM stdout for combined worker
  duo-shard-0.err               # SLURM stderr
```

## Lessons learned (failed approaches)

1. **Docker on Lustre**: overlay2, fuse-overlayfs, and vfs storage drivers all
   fail on the Lustre shared volume. Docker must use local disk.

2. **High concurrency on small disk**: With only 16 GB free, anything above
   `N_CONCURRENT=4` risks disk exhaustion from concurrent image pulls.

3. **`docker image prune -af` during active pulls**: Corrupts containerd
   snapshot metadata ("lease does not exist" errors). Use targeted
   `docker rmi <image>` instead.

4. **`--no-delete` without cache management**: Images accumulate and fill disk.
   The background cache manager (save-then-evict) is essential.

5. **SLURM `--export` with commas**: Passing comma-separated task lists via
   `sbatch --export=TASK_LIST=a,b,c` fails because SLURM uses commas to
   delimit multiple variables. Use task files instead.

6. **Docker Hub rate limits**: 200 pulls/6hr per authenticated account.
   Pre-building with `--force-build` avoids pulls but uses disk for build
   cache. The cache-manager approach (pull once, save to shared volume) is
   the most efficient.

## Currently running experiments (as of 2026-04-11)

- **opus-duo**: 5 nodes (2, 3, 5, 6, 7) running `slurm_duo_worker.sh`
- **opus-sonnet-duo**: 5 nodes (8, 9, 10, 11, 12) running `slurm_sonnet_worker.sh`
- **terminus2-duo**: Not yet started (pending rate limit reset on account 1)

Docker Hub account 2 (`ethanoch`) used for pulls. Image cache at
`docker-image-cache/` on the shared volume.
