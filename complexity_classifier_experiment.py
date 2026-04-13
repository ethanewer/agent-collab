#!/usr/bin/env python3
"""
Complexity Classifier Experiment for Duo Agent Routing

Runs Claude Haiku as a task-complexity classifier 5 times per task (445 total
API calls, all in parallel), then computes the expected benchmark score if the
classifier were used to route tasks to single vs duo agent mode.
"""

import json
import re
import sys
import time
import yaml
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
RESULTS_DIR = Path("/home/eewer/agent-collab/results/89-task-full")
TASKS_DIR = Path("/home/eewer/agent-collab/terminal-bench/original-tasks")
N_CLASSIFY_RUNS = 5
HAIKU_MODEL = "claude-haiku-4-5"
MAX_WORKERS = 50

# ---------------------------------------------------------------------------
# Classification prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You route tasks to one AI agent or two collaborating agents (duo). Use duo by \
default - it is almost always better.

Use single ONLY for tasks where the ENTIRE deliverable is a single, \
self-contained file (one script, one program, one proof) AND the task has no \
setup, installation, or configuration steps alongside it. In these cases, two \
agents would fight over writing the same file.

If the task involves ANY of: installing software, configuring services, working \
with multiple files, debugging an existing system, or running tests - use duo \
even if there is a main output file, because the other agent can handle the \
peripheral work.

Respond with ONLY valid JSON: {"use_duo": true/false, "reasoning": "brief"}\
"""


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def parse_local_results(filepath: Path) -> dict[str, list[int]]:
    """Parse a local results JSON into {task_name: [0/1, ...]}."""
    with open(filepath) as f:
        data = json.load(f)
    evals = data["stats"]["evals"]
    eval_key = list(evals.keys())[0]
    reward_stats = evals[eval_key]["reward_stats"]["reward"]
    results: dict[str, list[int]] = defaultdict(list)
    for trial_id in reward_stats.get("1.0", []):
        results[trial_id.rsplit("__", 1)[0]].append(1)
    for trial_id in reward_stats.get("0.0", []):
        results[trial_id.rsplit("__", 1)[0]].append(0)
    return dict(results)


def parse_opus_single_file(filepath: Path) -> dict[str, list[int]]:
    """Parse the opus single file scraped from tbench.ai."""
    with open(filepath) as f:
        data = json.load(f)
    return {
        task: [1] * info["successes"] + [0] * (info["trials"] - info["successes"])
        for task, info in data["tasks"].items()
    }


def load_task_instructions(task_names: list[str]) -> dict[str, str]:
    """Load task instructions from terminal-bench repo."""
    instructions = {}
    for name in task_names:
        p = TASKS_DIR / name / "task.yaml"
        if p.exists():
            with open(p) as f:
                instructions[name] = yaml.safe_load(f).get("instruction", "")
        else:
            instructions[name] = f"Task: {name}"
    return instructions


def load_task_metadata(task_names: list[str]) -> dict[str, dict]:
    """Load task metadata (difficulty, category, tags, timeout) from task.yaml."""
    metadata = {}
    for name in task_names:
        p = TASKS_DIR / name / "task.yaml"
        if p.exists():
            with open(p) as f:
                ty = yaml.safe_load(f)
            metadata[name] = {
                "difficulty": ty.get("difficulty", "unknown"),
                "category": ty.get("category", "unknown"),
                "tags": ty.get("tags", []),
                "timeout": ty.get("max_agent_timeout_sec", 0),
            }
        else:
            metadata[name] = {
                "difficulty": "unknown",
                "category": "unknown",
                "tags": [],
                "timeout": 0,
            }
    return metadata


def build_user_message(task_name: str, instruction: str, meta: dict) -> str:
    """Build user message with metadata context for the classifier."""
    m = meta.get(task_name, {})
    parts = [f"Task name: {task_name}", ""]
    parts.append(f"Difficulty: {m.get('difficulty', 'unknown')}")
    parts.append(f"Category: {m.get('category', 'unknown')}")
    tags = m.get("tags", [])
    if tags:
        parts.append(f"Tags: {', '.join(tags)}")
    parts.append(f"Agent timeout: {m.get('timeout', 0):.0f}s")
    parts.append("")
    parts.append(f"Task instruction:\n{instruction}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Classification (single call, used inside thread pool)
# ---------------------------------------------------------------------------
def _classify_one(
    client: anthropic.Anthropic,
    task_name: str,
    instruction: str,
    run_idx: int,
    user_msg_override: str = "",
) -> tuple[str, int, bool]:
    """Single classification call.  Returns (task_name, run_idx, use_duo)."""
    user_msg = (
        user_msg_override
        or f"Task name: {task_name}\n\nTask instruction:\n{instruction}"
    )
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=300,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
                temperature=0.3,
            )
            text = resp.content[0].text.strip()  # type: ignore[union-attr]
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)
            result = json.loads(text)
            return (task_name, run_idx, bool(result.get("use_duo", False)))
        except anthropic.RateLimitError:
            time.sleep(10 + attempt * 10)
        except (json.JSONDecodeError, KeyError, IndexError):
            if attempt < 2:
                time.sleep(1)
        except Exception as e:
            print(f"  ERR {task_name} run{run_idx}: {e}", file=sys.stderr)
            if attempt < 2:
                time.sleep(2)
    return (task_name, run_idx, False)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def compute_expected_score(
    task_names: list[str],
    single_trials: dict[str, list[int]],
    duo_trials: dict[str, list[int]],
    classifications: dict[str, list[bool]],
) -> dict:
    """Expected score under the classifier.

    E[task_score] = P(classify_duo)*duo_rate + P(classify_single)*single_rate
    Overall score  = mean over tasks.
    """
    n_tasks = len(task_names)
    task_details: dict = {}
    total = 0.0

    for task in task_names:
        s = single_trials.get(task, [0] * 5)
        d = duo_trials.get(task, [0] * 5)
        c = classifications.get(task, [False] * 5)

        sr = sum(s) / max(len(s), 1)
        dr = sum(d) / max(len(d), 1)
        pd = sum(c) / max(len(c), 1)

        expected = pd * dr + (1 - pd) * sr
        total += expected
        task_details[task] = {
            "expected_score": expected,
            "single_rate": sr,
            "duo_rate": dr,
            "n_duo_classify": sum(c),
            "duo_better": dr > sr,
            "optimal_choice": "duo" if dr > sr else "single",
            "classifier_majority_correct": (
                (sum(c) > 2) == (dr > sr) if dr != sr else True
            ),
        }

    def _rate(trials: dict, t: str) -> float:
        v = trials.get(t, [0] * 5)
        return sum(v) / max(len(v), 1)

    always_single = sum(_rate(single_trials, t) for t in task_names) / n_tasks
    always_duo = sum(_rate(duo_trials, t) for t in task_names) / n_tasks
    oracle = (
        sum(max(_rate(single_trials, t), _rate(duo_trials, t)) for t in task_names)
        / n_tasks
    )

    return {
        "overall_expected_score": total / n_tasks,
        "always_single_score": always_single,
        "always_duo_score": always_duo,
        "oracle_score": oracle,
        "n_tasks": n_tasks,
        "n_duo_better": sum(1 for d in task_details.values() if d["duo_better"]),
        "n_classifier_correct": sum(
            1 for d in task_details.values() if d["classifier_majority_correct"]
        ),
        "task_details": task_details,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    t0 = time.time()
    print("=" * 80)
    print("COMPLEXITY CLASSIFIER EXPERIMENT")
    print("=" * 80)

    # ---- 1. Load results ----
    print("\n--- Loading results ---")
    sonnet_single = parse_local_results(RESULTS_DIR / "sonnet-single-full.json")
    sonnet_duo = parse_local_results(RESULTS_DIR / "sonnet-duo-full.json")
    opus_duo = parse_local_results(RESULTS_DIR / "opus-duo-full.json")
    opus_single = parse_opus_single_file(RESULTS_DIR / "opus-single-full.json")

    all_tasks = sorted(
        set(sonnet_single) & set(sonnet_duo) & set(opus_single) & set(opus_duo)
    )
    print(f"Found {len(all_tasks)} tasks across all result sets")

    # ---- 2. Load task instructions and metadata ----
    print("\n--- Loading task instructions and metadata ---")
    instructions = load_task_instructions(all_tasks)
    metadata = load_task_metadata(all_tasks)
    n_found = sum(1 for v in instructions.values() if not v.startswith("Task:"))
    print(f"Loaded instructions for {n_found}/{len(all_tasks)} tasks")
    print(f"Loaded metadata for {len(metadata)} tasks")

    # ---- 3. Ground truth ----
    print("\n--- Ground truth: tasks where duo outperforms single ---")
    for label, st, dt in [
        ("OPUS", opus_single, opus_duo),
        ("SONNET", sonnet_single, sonnet_duo),
    ]:
        better = []
        for t in all_tasks:
            sr = sum(st.get(t, [0] * 5)) / 5
            dr = sum(dt.get(t, [0] * 5)) / 5
            if dr > sr:
                better.append((t, sr, dr))
        print(f"\n  {label}: {len(better)} tasks where duo > single:")
        for t, sr, dr in sorted(better, key=lambda x: x[2] - x[1], reverse=True):
            print(f"    {t:45s}  single={sr:.1f}  duo={dr:.1f}  delta=+{dr - sr:.1f}")

    # ---- 4. Parallel classification ----
    total_calls = N_CLASSIFY_RUNS * len(all_tasks)
    print(
        f"\n--- Running {total_calls} Haiku classifications "
        f"({N_CLASSIFY_RUNS} runs x {len(all_tasks)} tasks, "
        f"{MAX_WORKERS} workers) ---"
    )

    client = anthropic.Anthropic()
    classifications: dict[str, list] = {t: [False] * N_CLASSIFY_RUNS for t in all_tasks}
    done = 0

    # Build user messages with metadata context
    user_messages = {
        t: build_user_message(t, instructions.get(t, ""), metadata) for t in all_tasks
    }

    jobs = [
        (task, instructions.get(task, ""), run_idx, user_messages.get(task, ""))
        for run_idx in range(N_CLASSIFY_RUNS)
        for task in all_tasks
    ]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_classify_one, client, t, instr, r, msg): (t, r)
            for t, instr, r, msg in jobs
        }
        for fut in as_completed(futures):
            task_name, run_idx, use_duo = fut.result()
            classifications[task_name][run_idx] = use_duo
            done += 1
            if done % 50 == 0 or done == total_calls:
                elapsed = time.time() - t0
                print(f"  {done}/{total_calls} done  ({elapsed:.0f}s elapsed)")

    elapsed = time.time() - t0
    print(f"\n  All classifications complete in {elapsed:.1f}s")

    # ---- 5. Report ----
    print("\n" + "=" * 80)
    print("RESULTS")
    print("=" * 80)

    for label, st, dt in [
        ("OPUS  (single=CC 2.1.34 Opus, duo=DuoCC Opus)", opus_single, opus_duo),
        (
            "SONNET (single=ControlCC Sonnet, duo=DuoCC Sonnet)",
            sonnet_single,
            sonnet_duo,
        ),
    ]:
        print(f"\n{'=' * 80}")
        print(f"Scenario: {label}")
        print(f"{'=' * 80}")

        sc = compute_expected_score(all_tasks, st, dt, classifications)

        print(f"\n  Classifier expected score:  {sc['overall_expected_score']:.4f}")
        print(f"  Always-single baseline:     {sc['always_single_score']:.4f}")
        print(f"  Always-duo baseline:        {sc['always_duo_score']:.4f}")
        print(f"  Oracle (pick best):         {sc['oracle_score']:.4f}")
        print(f"\n  Tasks where duo > single:   {sc['n_duo_better']}/{sc['n_tasks']}")
        print(
            f"  Classifier majority-correct: {sc['n_classifier_correct']}/{sc['n_tasks']}"
        )

        lift = sc["overall_expected_score"] - sc["always_single_score"]
        oracle_lift = sc["oracle_score"] - sc["always_single_score"]
        pct = (lift / oracle_lift * 100) if oracle_lift > 0 else 0
        print(f"\n  Lift over always-single:    {lift:+.4f}")
        print(f"  Oracle lift:                {oracle_lift:+.4f}")
        print(f"  Classifier captures:        {pct:.1f}% of oracle lift")

        mis = [
            (t, d)
            for t, d in sc["task_details"].items()
            if not d["classifier_majority_correct"]
        ]
        if mis:
            print(f"\n  Misclassified tasks ({len(mis)}):")
            for t, d in sorted(
                mis,
                key=lambda x: abs(x[1]["duo_rate"] - x[1]["single_rate"]),
                reverse=True,
            ):
                print(
                    f"    {t:45s}  s={d['single_rate']:.1f} d={d['duo_rate']:.1f} "
                    f"opt={d['optimal_choice']:6s} cls_duo={d['n_duo_classify']}/5"
                )

    # ---- 6. Save ----
    output_path = Path("/home/eewer/agent-collab/classification_results.json")
    with open(output_path, "w") as f:
        json.dump(
            {
                "model": HAIKU_MODEL,
                "system_prompt": SYSTEM_PROMPT,
                "n_classify_runs": N_CLASSIFY_RUNS,
                "classifications": dict(classifications),
            },
            f,
            indent=2,
        )
    print(f"\nRaw results saved to {output_path}")
    print(f"\nTotal time: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
