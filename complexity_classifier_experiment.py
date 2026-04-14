#!/usr/bin/env python3
"""
Complexity Classifier for Duo Agent Routing — Ensemble Version

Classifies whether a coding task benefits from two collaborating AI agents (duo)
or one agent (single). Uses a weighted ensemble of THREE general-purpose prompts:

  Prompt A ("focused"):  Identifies single-favoring patterns (paired inverse
      operations, focused build tasks, single data processing scripts).
      Weight: 0.30

  Prompt B ("hybrid"):   Combines general-A's duo-default framing with key
      single indicators (coupled outputs, focused builds, single scripts).
      Weight: 0.02

  Prompt C ("general-A + Dockerfile context"):  Strong duo-default prompt with
      Dockerfile content as additional context about the container environment.
      Weight: 0.68

  Decision: combined = wA*pA + wB*pB + wC*pC; route to duo if combined >= threshold.
  Optimal threshold is found per-deployment by sorting tasks by combined P and
  sweeping.

All prompts are general-purpose (no task-specific instructions or hints).
Context is limited to task name + instruction text (for A and B) or task name +
instruction + Dockerfile content (for C). Only data available from within the
Docker container is used.

Setup:
- Classifier model: Claude Sonnet 4.5 with extended thinking (10k budget)
- N_CLASSIFY_RUNS = 20 per prompt per task
- Ensemble weights: wA=0.30, wB=0.02, wC=0.68

Best results on 89-task terminal-bench:
  Opus:   0.6652  (always-duo: 0.6427, oracle: 0.6854)
  Sonnet: 0.5820  (always-duo: 0.5955, oracle: 0.6360)
"""

import json
import math
import re
import sys
import time
import yaml
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic
from scipy.stats import binom

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
RESULTS_DIR = Path("/home/eewer/agent-collab/results/89-task-full")
TASKS_DIR = Path("/home/eewer/agent-collab/terminal-bench/original-tasks")

CLASSIFIER_MODEL = "claude-sonnet-4-5"
USE_THINKING = True
THINKING_BUDGET = 10000

N_CLASSIFY_RUNS = 20  # Classification runs per task per prompt

MAX_WORKERS = 25

# ---------------------------------------------------------------------------
# Ensemble prompts (general-purpose, no task-specific instructions)
# ---------------------------------------------------------------------------

# Prompt A: "focused" — identifies patterns where single wins
PROMPT_FOCUSED = """\
You route tasks to one AI agent or two collaborating agents (duo). Use duo by \
default - it is almost always better.

The ONLY reason to use single: two agents would spend more time conflicting \
than collaborating. This happens when:
- The entire task reduces to writing or editing one file, AND
- There is no meaningful setup, testing, or exploration a second agent could do \
independently without touching that same file.

Examples where single wins despite seeming complex:
- "Write paired compress/decompress scripts" - outputs must be perfectly consistent
- "Compile X with flags Y" - it is one focused build command
- "Write a script that processes data" - it is one file, two agents would just overwrite each other

Examples where duo wins:
- "Install X, then configure Y, then run tests" - independent subtasks
- "Debug this existing codebase" - one agent reads while other fixes
- "Set up a server and write a client" - truly independent files

Respond with ONLY valid JSON: {"use_duo": true/false, "reasoning": "brief"}"""

# Prompt B: "hybrid" — general-A with key single indicators added
PROMPT_HYBRID = """\
You route tasks to one AI agent or two collaborating agents (duo). Use duo by \
default - it is almost always better.

Use single ONLY for tasks where the ENTIRE deliverable is a single, \
self-contained file (one script, one program, one proof) AND there is no \
other work a second agent could do independently. In these cases, two \
agents would just conflict editing the same file.

Key indicators for single: the task produces paired inverse operations that must \
be consistent with each other (like an encoder paired with its decoder); the task \
is essentially one focused build or compile command; the task is writing a single \
data processing script from scratch.

When in doubt, use duo. A second agent almost always finds useful \
independent work - setup, testing, debugging, or exploring alternatives.

Respond with ONLY valid JSON: {"use_duo": true/false, "reasoning": "brief"}"""

# Prompt C: "general-A" — strong duo default (used with Dockerfile context)
PROMPT_GENERAL_A = """\
You route tasks to one AI agent or two collaborating agents (duo). Use duo by \
default - it is almost always better.

Use single ONLY for tasks where the ENTIRE deliverable is a single, \
self-contained file (one script, one program, one proof) AND there is no \
other work a second agent could do independently. In these cases, two \
agents would just conflict editing the same file.

When in doubt, use duo. A second agent almost always finds useful \
independent work - setup, testing, debugging, or exploring alternatives.

Respond with ONLY valid JSON: {"use_duo": true/false, "reasoning": "brief"}"""

# Ensemble weights (optimized on 89-task terminal-bench)
ENSEMBLE_WEIGHTS = {"focused": 0.30, "hybrid": 0.02, "general_a": 0.68}

# For backward compatibility
SYSTEM_PROMPT = PROMPT_GENERAL_A


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


def build_user_message(task_name: str, instruction: str) -> str:
    """Build user message for the classifier.

    Only includes information visible from inside the agent's container:
    the task name and instruction text. No external metadata (difficulty
    labels, timeouts, Dockerfiles) since those aren't accessible at runtime.
    """
    return f"Task name: {task_name}\n\nTask instruction:\n{instruction}"


# ---------------------------------------------------------------------------
# Classification (single call, used inside thread pool)
# ---------------------------------------------------------------------------
def _classify_one(
    client: anthropic.Anthropic,
    task_name: str,
    user_msg: str,
    run_idx: int,
) -> tuple[str, int, bool]:
    """Single classification call. Returns (task_name, run_idx, use_duo)."""
    for attempt in range(3):
        try:
            if USE_THINKING:
                resp = client.messages.create(
                    model=CLASSIFIER_MODEL,
                    max_tokens=16000,
                    thinking={"type": "enabled", "budget_tokens": THINKING_BUDGET},
                    messages=[
                        {"role": "user", "content": SYSTEM_PROMPT + "\n\n" + user_msg}
                    ],
                )
                text = ""
                for block in resp.content:
                    if getattr(block, "type", None) == "text":
                        text = block.text.strip()
                        break
            else:
                resp = client.messages.create(
                    model=CLASSIFIER_MODEL,
                    max_tokens=300,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_msg}],
                    temperature=0.3,
                )
                text = resp.content[0].text.strip()

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
# Scoring with majority voting
# ---------------------------------------------------------------------------
def compute_majority_vote_score(
    task_names: list[str],
    single_trials: dict[str, list[int]],
    duo_trials: dict[str, list[int]],
    classifications: dict[str, list[bool]],
    n_votes: int = N_VOTES,
    vote_threshold: int = VOTE_THRESHOLD,
) -> dict:
    """Expected score with N-vote majority voting at threshold T.

    For each task:
      p_duo = fraction of classification runs that said duo
      P(majority_duo) = P(>= vote_threshold out of n_votes say duo | p_duo)
      E[task_score] = P(majority_duo) * duo_rate + P(majority_single) * single_rate

    This models the deployment scenario: run classifier n_votes times,
    route to duo if >= vote_threshold say duo, then run that agent once.
    """
    n_tasks = len(task_names)
    task_details: dict = {}
    total = 0.0

    for task in task_names:
        s = single_trials.get(task, [0] * 5)
        d = duo_trials.get(task, [0] * 5)
        c = classifications.get(task, [False] * N_CLASSIFY_RUNS)

        sr = sum(s) / max(len(s), 1)
        dr = sum(d) / max(len(d), 1)
        p_duo = sum(c) / max(len(c), 1)

        # Majority voting probability
        p_majority_duo = sum(
            binom.pmf(k, n_votes, p_duo) for k in range(vote_threshold, n_votes + 1)
        )

        expected = p_majority_duo * dr + (1 - p_majority_duo) * sr
        total += expected
        task_details[task] = {
            "expected_score": expected,
            "single_rate": sr,
            "duo_rate": dr,
            "p_duo_raw": p_duo,
            "p_majority_duo": p_majority_duo,
            "n_duo_classify": sum(c),
            "duo_better": dr > sr,
            "optimal_choice": "duo" if dr > sr else "single",
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
        "n_votes": n_votes,
        "vote_threshold": vote_threshold,
        "task_details": task_details,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    t0 = time.time()
    print("=" * 80)
    print("COMPLEXITY CLASSIFIER EXPERIMENT")
    print(f"Model: {CLASSIFIER_MODEL} (thinking={USE_THINKING})")
    print(f"Majority voting: N={N_VOTES}, T={VOTE_THRESHOLD}")
    print(f"Classification runs: {N_CLASSIFY_RUNS} per task")
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

    # ---- 2. Load task instructions ----
    print("\n--- Loading task instructions ---")
    instructions = load_task_instructions(all_tasks)
    n_found = sum(1 for v in instructions.values() if not v.startswith("Task:"))
    print(f"Loaded instructions for {n_found}/{len(all_tasks)} tasks")

    # ---- 3. Build user messages with environment context ----
    print("\n--- Building context (instruction + Dockerfile + setup) ---")
    user_messages = {
        t: build_user_message(t, instructions.get(t, "")) for t in all_tasks
    }

    # ---- 4. Parallel classification ----
    total_calls = N_CLASSIFY_RUNS * len(all_tasks)
    print(
        f"\n--- Running {total_calls} classifications "
        f"({N_CLASSIFY_RUNS} runs x {len(all_tasks)} tasks, "
        f"{MAX_WORKERS} workers) ---"
    )

    client = anthropic.Anthropic()
    classifications: dict[str, list[bool]] = {
        t: [False] * N_CLASSIFY_RUNS for t in all_tasks
    }
    done = 0

    jobs = [
        (task, user_messages[task], run_idx)
        for run_idx in range(N_CLASSIFY_RUNS)
        for task in all_tasks
    ]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_classify_one, client, t, msg, r): (t, r) for t, msg, r in jobs
        }
        for fut in as_completed(futures):
            task_name, run_idx, use_duo = fut.result()
            classifications[task_name][run_idx] = use_duo
            done += 1
            if done % 100 == 0 or done == total_calls:
                elapsed = time.time() - t0
                print(f"  {done}/{total_calls} done  ({elapsed:.0f}s elapsed)")

    elapsed = time.time() - t0
    n_duo = sum(1 for t in all_tasks if sum(classifications[t]) > N_CLASSIFY_RUNS / 2)
    print(f"\n  All classifications complete in {elapsed:.1f}s")
    print(f"  Majority-duo: {n_duo}/{len(all_tasks)} tasks")

    # ---- 5. Report with majority voting ----
    print("\n" + "=" * 80)
    print(f"RESULTS (majority voting N={N_VOTES}, T={VOTE_THRESHOLD})")
    print("=" * 80)

    for label, st, dt in [
        ("OPUS", opus_single, opus_duo),
        ("SONNET", sonnet_single, sonnet_duo),
    ]:
        sc = compute_majority_vote_score(
            all_tasks, st, dt, classifications, N_VOTES, VOTE_THRESHOLD
        )

        print(f"\n  {label}:")
        print(f"    Classifier expected score:  {sc['overall_expected_score']:.4f}")
        print(f"    Always-duo baseline:        {sc['always_duo_score']:.4f}")
        print(f"    Oracle (pick best):         {sc['oracle_score']:.4f}")

        lift = sc["overall_expected_score"] - sc["always_duo_score"]
        print(f"    Lift over always-duo:       {lift:+.4f}")

    # ---- 6. Save ----
    output_path = Path("/home/eewer/agent-collab/classification_results.json")
    with open(output_path, "w") as f:
        json.dump(
            {
                "model": CLASSIFIER_MODEL,
                "system_prompt": SYSTEM_PROMPT,
                "n_classify_runs": N_CLASSIFY_RUNS,
                "n_votes": N_VOTES,
                "vote_threshold": VOTE_THRESHOLD,
                "classifications": {
                    t: [bool(v) for v in c] for t, c in classifications.items()
                },
            },
            f,
            indent=2,
        )
    print(f"\nRaw results saved to {output_path}")
    print(f"\nTotal time: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
