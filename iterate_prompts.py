#!/usr/bin/env python3
"""
Rapid prompt iteration for the duo classifier.
Tests a prompt, prints compact results, appends to a log.
"""

import json, re, sys, time, yaml
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic

RESULTS_DIR = Path("/home/eewer/agent-collab/results/89-task-full")
TASKS_DIR = Path("/home/eewer/agent-collab/terminal-bench/original-tasks")
N_RUNS = 5
DEFAULT_MODEL = "claude-haiku-4-5"
WORKERS = 50
LOG_FILE = Path("/home/eewer/agent-collab/prompt_iteration_log.jsonl")


# ── data loading (cached) ──────────────────────────────────────────────────
_cache: dict = {}


def load_data():
    if _cache:
        return _cache

    def _local(p):
        d = json.load(open(p))
        ev = d["stats"]["evals"]
        rs = ev[list(ev)[0]]["reward_stats"]["reward"]
        r: dict[str, list[int]] = defaultdict(list)
        for tid in rs.get("1.0", []):
            r[tid.rsplit("__", 1)[0]].append(1)
        for tid in rs.get("0.0", []):
            r[tid.rsplit("__", 1)[0]].append(0)
        return dict(r)

    def _opus(p):
        d = json.load(open(p))
        return {
            t: [1] * i["successes"] + [0] * (i["trials"] - i["successes"])
            for t, i in d["tasks"].items()
        }

    ss = _local(RESULTS_DIR / "sonnet-single-full.json")
    sd = _local(RESULTS_DIR / "sonnet-duo-full.json")
    od = _local(RESULTS_DIR / "opus-duo-full.json")
    os_ = _opus(RESULTS_DIR / "opus-single-full.json")
    tasks = sorted(set(ss) & set(sd) & set(os_) & set(od))
    instr = {}
    meta = {}
    for name in tasks:
        p = TASKS_DIR / name / "task.yaml"
        if p.exists():
            ty = yaml.safe_load(open(p))
            instr[name] = ty.get("instruction", "")
            meta[name] = {
                "difficulty": ty.get("difficulty", "unknown"),
                "category": ty.get("category", "unknown"),
                "tags": ty.get("tags", []),
                "timeout": ty.get("max_agent_timeout_sec", 0),
            }
        else:
            instr[name] = f"Task: {name}"
            meta[name] = {
                "difficulty": "unknown",
                "category": "unknown",
                "tags": [],
                "timeout": 0,
            }
        # Load Dockerfile if present
        df_path = TASKS_DIR / name / "Dockerfile"
        if df_path.exists():
            raw = df_path.read_text()
            # Strip ASCII art banners and comments-only lines
            lines = [
                l
                for l in raw.splitlines()
                if l.strip()
                and not l.strip().startswith("#")
                and "___" not in l
                and "\\__" not in l
                and "|/" not in l
                and "/\\" not in l
                and "( (" not in l
                and ") )" not in l
                and "|  " not in l
            ]
            meta[name]["dockerfile"] = "\n".join(lines)
        else:
            meta[name]["dockerfile"] = ""
        # Load setup.py / setup.sh if present
        for sp in ["setup.py", "setup.sh"]:
            sp_path = TASKS_DIR / name / sp
            if sp_path.exists():
                meta[name]["setup"] = sp_path.read_text()[:2000]
                break
        else:
            meta[name]["setup"] = ""
    _cache.update(
        sonnet_single=ss,
        sonnet_duo=sd,
        opus_single=os_,
        opus_duo=od,
        tasks=tasks,
        instructions=instr,
        metadata=meta,
    )
    return _cache


# ── classify ───────────────────────────────────────────────────────────────
def classify_one(
    client,
    sys_prompt,
    task,
    instr,
    run_idx,
    use_thinking=False,
    extra="",
    model=DEFAULT_MODEL,
):
    msg = (
        f"Task name: {task}\n\n{extra}Task instruction:\n{instr}"
        if extra
        else f"Task name: {task}\n\nTask instruction:\n{instr}"
    )
    for att in range(3):
        try:
            if use_thinking:
                r = client.messages.create(
                    model=model,
                    max_tokens=16000,
                    thinking={"type": "enabled", "budget_tokens": 10000},
                    messages=[{"role": "user", "content": sys_prompt + "\n\n" + msg}],
                )
                txt = ""
                for block in r.content:
                    if getattr(block, "type", None) == "text":
                        txt = block.text.strip()
                        break
            else:
                r = client.messages.create(
                    model=model,
                    max_tokens=300,
                    system=sys_prompt,
                    messages=[{"role": "user", "content": msg}],
                    temperature=0.3,
                )
                txt = r.content[0].text.strip()  # type: ignore[union-attr]
            if txt.startswith("```"):
                txt = re.sub(r"^```(?:json)?\s*", "", txt)
                txt = re.sub(r"\s*```$", "", txt)
            return (task, run_idx, bool(json.loads(txt).get("use_duo", False)))
        except anthropic.RateLimitError:
            time.sleep(10 + att * 10)
        except (json.JSONDecodeError, KeyError, IndexError):
            time.sleep(1)
        except Exception as e:
            if att == 2:
                print(f"  ERR {task} r{run_idx}: {e}", file=sys.stderr)
            time.sleep(2)
    return (task, run_idx, False)


# ── scoring ────────────────────────────────────────────────────────────────
def score(tasks, st, dt, cls_):
    n = len(tasks)
    total = 0.0
    details = {}
    for t in tasks:
        s = st.get(t, [0] * 5)
        d = dt.get(t, [0] * 5)
        c = cls_.get(t, [False] * 5)
        sr = sum(s) / max(len(s), 1)
        dr = sum(d) / max(len(d), 1)
        pd = sum(c) / max(len(c), 1)
        exp = pd * dr + (1 - pd) * sr
        total += exp
        details[t] = dict(
            exp=exp,
            sr=sr,
            dr=dr,
            nd=sum(c),
            duo_better=dr > sr,
            correct=(sum(c) > 2) == (dr > sr) if dr != sr else True,
        )

    def _r(tr, t):
        v = tr.get(t, [0] * 5)
        return sum(v) / max(len(v), 1)

    a_s = sum(_r(st, t) for t in tasks) / n
    a_d = sum(_r(dt, t) for t in tasks) / n
    orc = sum(max(_r(st, t), _r(dt, t)) for t in tasks) / n
    return dict(
        score=total / n,
        single=a_s,
        duo=a_d,
        oracle=orc,
        n_correct=sum(1 for d in details.values() if d["correct"]),
        n_duo_better=sum(1 for d in details.values() if d["duo_better"]),
        details=details,
        n=n,
    )


# ── message builders ───────────────────────────────────────────────────────
def build_meta_msg(task, instr, meta):
    """Build extra context with metadata fields."""
    m = meta.get(task, {})
    parts = []
    parts.append(f"Difficulty: {m.get('difficulty', 'unknown')}")
    parts.append(f"Category: {m.get('category', 'unknown')}")
    tags = m.get("tags", [])
    if tags:
        parts.append(f"Tags: {', '.join(tags)}")
    parts.append(f"Agent timeout: {m.get('timeout', 0):.0f}s")
    return "\n".join(parts) + "\n\n"


def build_rich_msg(task, instr, meta):
    """Build extra context with metadata + Dockerfile."""
    m = meta.get(task, {})
    parts = []
    parts.append(f"Difficulty: {m.get('difficulty', 'unknown')}")
    parts.append(f"Category: {m.get('category', 'unknown')}")
    tags = m.get("tags", [])
    if tags:
        parts.append(f"Tags: {', '.join(tags)}")
    parts.append(f"Agent timeout: {m.get('timeout', 0):.0f}s")
    df = m.get("dockerfile", "")
    if df:
        parts.append(f"\nDockerfile:\n{df}")
    setup = m.get("setup", "")
    if setup:
        parts.append(f"\nSetup script (runs before agent starts):\n{setup[:1000]}")
    return "\n".join(parts) + "\n\n"


def build_full_context(task, instr, meta):
    """Build richest context: metadata + Dockerfile + file tree + test summary."""
    import os, re as _re

    m = meta.get(task, {})
    parts = []
    parts.append(f"Difficulty: {m.get('difficulty', 'unknown')}")
    parts.append(f"Category: {m.get('category', 'unknown')}")
    tags = m.get("tags", [])
    if tags:
        parts.append(f"Tags: {', '.join(tags)}")
    parts.append(f"Agent timeout: {m.get('timeout', 0):.0f}s")

    td = TASKS_DIR / task
    df = m.get("dockerfile", "")
    if df:
        parts.append(f"\nDockerfile:\n{df}")
    setup = m.get("setup", "")
    if setup:
        parts.append(f"\nSetup script:\n{setup[:800]}")

    # Test structure summary (what files are checked, key patterns)
    tests_dir = td / "tests"
    if tests_dir.exists():
        for tf in sorted(tests_dir.glob("*.py"))[:2]:
            try:
                content = tf.read_text()
            except Exception:
                continue
            # Extract test function names and key path references
            func_names = _re.findall(r"def (test_\w+)", content)
            paths_checked = set()
            for match in _re.finditer(
                r'(?:Path|open)\s*\(\s*["\']([^"\']+)["\']', content
            ):
                paths_checked.add(match.group(1))
            for match in _re.finditer(r"assert.*(?:exists|is_file|is_dir)", content):
                for pm in _re.finditer(r'["\'](/[^"\']+)["\']', match.group()):
                    paths_checked.add(pm.group(1))
            # Extract imports that suggest what's being tested
            imports = [
                l.strip()
                for l in content.split("\n")
                if l.strip().startswith("import ")
                or l.strip().startswith("from ")
                and "pytest" not in l
                and "canary" not in l.lower()
            ][:5]

            summary = []
            summary.append(f"Test file: {tf.name}")
            summary.append(
                f"  Test functions ({len(func_names)}): {', '.join(func_names[:12])}"
            )
            if paths_checked:
                summary.append(
                    f"  Paths checked: {', '.join(sorted(paths_checked)[:8])}"
                )
            if imports:
                summary.append(f"  Key imports: {'; '.join(imports[:4])}")
            # Check for patterns indicating coupled outputs
            if "roundtrip" in content.lower() or (
                "compress" in content.lower() and "decompress" in content.lower()
            ):
                summary.append(
                    "  Pattern: roundtrip/encode-decode testing (outputs must be mutually consistent)"
                )
            if "subprocess.run" in content and (
                "compile" in content.lower()
                or "make" in content.lower()
                or "build" in content.lower()
            ):
                summary.append("  Pattern: compilation/build verification")
            parts.append("\n" + "\n".join(summary))

    return "\n".join(parts) + "\n\n"


# ── main ───────────────────────────────────────────────────────────────────
def run_prompt(
    sys_prompt: str,
    label: str = "",
    build_msg=None,
    model: str = DEFAULT_MODEL,
    use_thinking: bool = False,
    n_runs: int = N_RUNS,
    workers: int = WORKERS,
):
    t0 = time.time()
    data = load_data()
    tasks = data["tasks"]
    instr = data["instructions"]
    meta = data.get("metadata", {})
    client = anthropic.Anthropic()

    print(f"  model={model}  thinking={use_thinking}  runs={n_runs}")

    cls_: dict[str, list] = {t: [False] * n_runs for t in tasks}
    # Build extra context per task if build_msg is provided
    extras = {}
    if build_msg:
        for t in tasks:
            extras[t] = build_msg(t, instr.get(t, ""), meta)
    jobs = [
        (t, instr.get(t, ""), r, extras.get(t, ""))
        for r in range(n_runs)
        for t in tasks
    ]
    done = 0
    total = len(jobs)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(
                classify_one,
                client,
                sys_prompt,
                t,
                i,
                r,
                use_thinking,
                e,
                model,
            ): (t, r)
            for t, i, r, e in jobs
        }
        for f in as_completed(futs):
            tn, ri, ud = f.result()
            cls_[tn][ri] = ud
            done += 1
            if done % 100 == 0 or done == total:
                print(f"  {done}/{total} ({time.time() - t0:.0f}s)")

    elapsed = time.time() - t0
    majority = n_runs / 2
    n_duo = sum(1 for t in tasks if sum(cls_[t]) > majority)
    print(f"\n  Classified {n_duo}/{len(tasks)} as duo  ({elapsed:.1f}s)")

    # Score both scenarios
    for lbl, st, dt in [
        ("OPUS", data["opus_single"], data["opus_duo"]),
        ("SONNET", data["sonnet_single"], data["sonnet_duo"]),
    ]:
        sc = score(tasks, st, dt, cls_)
        lift = sc["score"] - sc["single"]
        ol = sc["oracle"] - sc["single"]
        pct = lift / ol * 100 if ol > 0 else 0
        print(
            f"\n  {lbl}: score={sc['score']:.4f}  single={sc['single']:.4f}  "
            f"duo={sc['duo']:.4f}  oracle={sc['oracle']:.4f}"
        )
        print(
            f"    correct={sc['n_correct']}/{sc['n']}  "
            f"lift={lift:+.4f}  oracle_lift={ol:+.4f}  captures={pct:.1f}%"
        )

        # Show misclassified
        mis = [(t, d) for t, d in sc["details"].items() if not d["correct"]]
        if mis:
            print(f"    Errors ({len(mis)}):")
            for t, d in sorted(
                mis, key=lambda x: abs(x[1]["dr"] - x[1]["sr"]), reverse=True
            ):
                opt = "DUO" if d["duo_better"] else "SNG"
                got = "DUO" if d["nd"] > 2 else "SNG"
                print(
                    f"      {t:42s} s={d['sr']:.1f} d={d['dr']:.1f} "
                    f"opt={opt} got={got}({d['nd']}/5)"
                )

    # Log
    entry = {
        "label": label,
        "time": elapsed,
        "prompt_len": len(sys_prompt),
        "n_duo_classified": n_duo,
        "classifications": {t: sum(cls_[t]) for t in tasks},
    }
    for lbl, st, dt in [
        ("opus", data["opus_single"], data["opus_duo"]),
        ("sonnet", data["sonnet_single"], data["sonnet_duo"]),
    ]:
        sc = score(tasks, st, dt, cls_)
        entry[f"{lbl}_score"] = sc["score"]
        entry[f"{lbl}_correct"] = sc["n_correct"]
        entry[f"{lbl}_captures_pct"] = round(
            (sc["score"] - sc["single"]) / (sc["oracle"] - sc["single"]) * 100
            if sc["oracle"] > sc["single"]
            else 0,
            1,
        )
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"\n  Logged to {LOG_FILE}")
    return cls_


if __name__ == "__main__":
    # Default: use the prompt defined in complexity_classifier_experiment.py
    from complexity_classifier_experiment import SYSTEM_PROMPT

    run_prompt(SYSTEM_PROMPT, "baseline")
