#!/usr/bin/env python3
"""Display results from Harbor benchmark jobs. Read-only -- writes nothing."""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

JOBS_DIR = Path(__file__).parent / "jobs"

TIMEOUT_ERRORS = {"AgentTimeoutError"}

SYM_PASS = "\033[32m\u2713\033[0m"
SYM_FAIL = "\033[31m\u2717\033[0m"
SYM_TIMEOUT = "\033[33mT\033[0m"
SYM_ERROR = "\033[90mE\033[0m"
SYM_PENDING = "\033[90m\u00b7\033[0m"


def find_job_groups(jobs_dir: Path) -> list[Path]:
    """Find top-level job group directories (contain shard subdirs or run dirs)."""
    return sorted(e for e in jobs_dir.iterdir() if e.is_dir())


def find_shards(job_group: Path) -> list[Path]:
    """Return shard directories, or [job_group] itself if no shards."""
    shards = sorted(e for e in job_group.iterdir() if e.is_dir())
    return shards if shards else [job_group]


def find_latest_run(shard: Path) -> Path | None:
    """Find the most recent run directory (timestamp-named) in a shard."""
    runs = sorted(e for e in shard.iterdir() if e.is_dir() and (e / "config.json").exists())
    return runs[-1] if runs else None


def get_expected_tasks_and_attempts(run_dir: Path) -> tuple[list[str], int]:
    """Read config.json to get expected task list and attempt count."""
    try:
        with open(run_dir / "config.json") as f:
            cfg = json.load(f)
        tasks = []
        for ds in cfg.get("datasets", []):
            tasks.extend(ds.get("task_names") or [])
        return tasks, cfg.get("n_attempts", 1)
    except (json.JSONDecodeError, OSError, KeyError):
        return [], 1


def read_trial_results(run_dir: Path) -> list[dict]:
    """Read all individual trial result.json files from a run directory."""
    trials = []
    for entry in sorted(run_dir.iterdir()):
        result_file = entry / "result.json"
        if entry.is_dir() and result_file.exists():
            try:
                with open(result_file) as f:
                    trials.append(json.load(f))
            except (json.JSONDecodeError, OSError):
                pass
    return trials


def classify_trial(trial: dict) -> tuple[str, float | None]:
    """Classify a trial. Returns (status, reward).

    status: 'pass', 'fail', 'timeout', or 'error'
    reward: float if scored, None if errored
    """
    exc = trial.get("exception_info")
    verifier = trial.get("verifier_result")

    if exc is None:
        reward = verifier["rewards"]["reward"] if verifier else None
        return ("pass" if reward is not None and reward >= 0.5 else "fail", reward)

    exc_type = exc.get("exception_type", "")
    if exc_type in TIMEOUT_ERRORS:
        reward = None
        if verifier and verifier.get("rewards"):
            reward = verifier["rewards"]["reward"]
        return ("timeout", reward)

    return ("error", None)


def main():
    parser = argparse.ArgumentParser(description="Display Harbor benchmark job results.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show per-task breakdown")
    args = parser.parse_args()

    if not JOBS_DIR.is_dir():
        print(f"Jobs directory not found: {JOBS_DIR}", file=sys.stderr)
        sys.exit(1)

    job_groups = find_job_groups(JOBS_DIR)
    if not job_groups:
        print("No job groups found.", file=sys.stderr)
        sys.exit(1)

    for group in job_groups:
        shards = find_shards(group)
        all_trials = []
        run_count = 0
        all_expected_tasks: set[str] = set()
        n_attempts = 1

        for shard in shards:
            run_dir = find_latest_run(shard)
            if run_dir is None:
                continue
            run_count += 1
            tasks, attempts = get_expected_tasks_and_attempts(run_dir)
            all_expected_tasks.update(tasks)
            n_attempts = max(n_attempts, attempts)
            all_trials.extend(read_trial_results(run_dir))

        if not all_trials:
            continue

        scored_rewards = []
        n_completed = 0
        n_timeout = 0
        n_errored = 0
        task_results: dict[str, list[tuple[str, float | None]]] = defaultdict(list)
        error_types: dict[str, int] = defaultdict(int)

        for trial in all_trials:
            status, reward = classify_trial(trial)
            task_name = trial.get("task_name", "unknown")
            task_results[task_name].append((status, reward))

            if status in ("pass", "fail"):
                n_completed += 1
                if reward is not None:
                    scored_rewards.append(reward)
            elif status == "timeout":
                n_timeout += 1
                if reward is not None:
                    scored_rewards.append(reward)
            else:
                n_errored += 1
                exc_type = trial.get("exception_info", {}).get("exception_type", "Unknown")
                error_types[exc_type] += 1

        n_total_expected = len(all_expected_tasks) * n_attempts
        n_pending = n_total_expected - len(all_trials)

        score = sum(scored_rewards) / len(scored_rewards) if scored_rewards else 0.0
        n_pass = sum(1 for r in scored_rewards if r >= 0.5)
        n_fail = sum(1 for r in scored_rewards if r < 0.5)

        print(f"{'=' * 70}")
        print(f"  Job: {group.name}")
        print(f"{'=' * 70}")
        print(f"  Score:     {score:.1%}  ({n_pass} pass / {n_fail} fail of {len(scored_rewards)} scored)")
        print(f"  Completed: {n_completed + n_timeout}  ({n_completed} ok + {n_timeout} timeout)")
        print(f"  Errored:   {n_errored}")
        if n_pending > 0:
            print(f"  Pending:   {n_pending}")
        print(f"  Total:     {len(all_trials)}/{n_total_expected}  "
              f"(across {run_count} shards, {n_attempts} attempts/task)")

        if error_types:
            print(f"\n  Error breakdown:")
            for etype, count in sorted(error_types.items(), key=lambda x: -x[1]):
                print(f"    {etype}: {count}")

        if args.verbose:
            print(f"\n  Per-task results ({n_attempts} attempts each):")
            for task_name in sorted(all_expected_tasks):
                entries = task_results.get(task_name, [])
                parts = []
                for status, _reward in entries:
                    if status == "pass":
                        parts.append(SYM_PASS)
                    elif status == "fail":
                        parts.append(SYM_FAIL)
                    elif status == "timeout":
                        parts.append(SYM_TIMEOUT)
                    else:
                        parts.append(SYM_ERROR)
                while len(parts) < n_attempts:
                    parts.append(SYM_PENDING)
                print(f"    {task_name:45s} {' '.join(parts)}")

        print()


if __name__ == "__main__":
    main()
