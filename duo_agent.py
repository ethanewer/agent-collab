"""Duo agent: two Claude Code instances collaborating via a shared append-only log file."""

from __future__ import annotations

import json
import os
import shlex
import textwrap
from pathlib import Path
from typing import Any

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths

CLAUDE_CODE_VERSION = "2.1.92"
COLLAB_FILE = "/tmp/agent-collab.log"
AGENT_A_LOG = "/tmp/agent-a.log"
AGENT_B_LOG = "/tmp/agent-b.log"

COLLAB_PROMPT_A = textwrap.dedent("""\
    You are one of two equal AI agents (you are Agent A, the other is Agent B) working \
    together in the same environment on the same task.

    COMMUNICATION: You share a log file at {collab_file}.
    - Append only: echo "[A|$(date -u +%Y-%m-%dT%H:%M:%S)] <msg>" >> {collab_file}
    - Read it with: cat {collab_file}

    You go first. Before doing any real work:
    1. Analyze the task briefly.
    2. Post a proposed plan to the log file. In your plan:
       - List which output files need to be created or modified.
       - If there are multiple output files or areas, propose who handles which.
       - If the task produces only ONE output file, propose that one agent writes it \
    and the other reviews, tests, and debugs. This is critical — if both agents write \
    the same file, one will silently overwrite the other's work.
       - End with PLAN_READY.
    3. Wait briefly for Agent B to respond (poll with: \
    for i in $(seq 1 15); do grep -q '\\[B|' {collab_file} 2>/dev/null && break; sleep 2; \
    done; cat {collab_file}). If no response after ~30s, start working anyway.
    4. Once you've both agreed on the division, start your part.

    AVOIDING FILE CONFLICTS: Before creating or modifying any file, announce it in the \
    log (e.g. "CLAIMING: extract.js"). If the other agent already claimed that file, do \
    NOT write to it — help by reviewing their work, writing tests, or debugging instead. \
    Two agents writing the same file is the #1 cause of failure.

    STATUS UPDATES: Post brief progress updates to {collab_file} every few minutes so \
    Agent B knows you're alive and working. Example: "Still analyzing binary structure, \
    will write extract.js soon." This prevents Agent B from assuming you're stuck.

    Throughout the task, check {collab_file} regularly for messages from Agent B. Stick \
    to the agreed division. Discuss before completing. Never block in a loop waiting — \
    do useful work instead.
""").format(collab_file=COLLAB_FILE)

COLLAB_PROMPT_B = textwrap.dedent("""\
    You are one of two equal AI agents (you are Agent B, the other is Agent A) working \
    together in the same environment on the same task.

    COMMUNICATION: You share a log file at {collab_file}.
    - Append only: echo "[B|$(date -u +%Y-%m-%dT%H:%M:%S)] <msg>" >> {collab_file}
    - Read it with: cat {collab_file}

    Agent A goes first. Before doing any real work:
    1. Wait for Agent A's plan: \
    for i in $(seq 1 20); do grep -q 'PLAN_READY' {collab_file} 2>/dev/null && break; \
    sleep 3; done
    2. Read the plan: cat {collab_file}
    3. If no plan appeared after ~60s, start working on the task independently.
    4. Otherwise, reply with your thoughts and confirm the division of work. If there \
    is only one output file, agree on who writes it — the other agent should review, \
    test, and debug rather than writing a competing version.

    AVOIDING FILE CONFLICTS: Before creating or modifying any file, check {collab_file} \
    to see if Agent A already claimed it. If so, do NOT write to it — help by reviewing \
    their work, writing tests, or debugging instead. Two agents writing the same file \
    is the #1 cause of failure.

    PATIENCE: Your internal sense of elapsed time is unreliable. Before deciding Agent A \
    is inactive, you MUST run: cat {collab_file} and check the actual timestamps of \
    Agent A's messages. Only take over Agent A's claimed work if:
    (a) Agent A explicitly asks for help, OR
    (b) Agent A's last message timestamp is more than 5 minutes ago AND you have \
    re-read the log just now to confirm this.
    If Agent A is still working, do NOT write their files — instead help by analyzing \
    the problem, investigating edge cases, preparing tests, or posting useful insights \
    to the log. If you get impatient, write a status message to the log asking Agent A \
    for an update instead of taking over their work.

    Throughout the task, check {collab_file} regularly for messages from Agent A. Post \
    brief status updates. Stick to the agreed division. Discuss before completing. Never \
    block in a loop waiting — do useful work instead.
""").format(collab_file=COLLAB_FILE)


class DuoClaudeCode(BaseInstalledAgent):

    @staticmethod
    def name() -> str:
        return "claude-code-duo"

    async def install(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(
            environment,
            command=(
                "if command -v apk &> /dev/null; then"
                "  apk add --no-cache curl bash nodejs npm;"
                " elif command -v apt-get &> /dev/null; then"
                "  apt-get update && apt-get install -y curl;"
                " elif command -v yum &> /dev/null; then"
                "  yum install -y curl;"
                " else"
                '  echo "Warning: No known package manager found" >&2;'
                " fi"
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )
        version = self._version or CLAUDE_CODE_VERSION
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                "if command -v apk &> /dev/null; then"
                f"  npm install -g @anthropic-ai/claude-code@{version};"
                " else"
                f"  curl -fsSL https://claude.ai/install.sh | bash -s -- {version};"
                " fi && "
                "echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.bashrc && "
                'export PATH="$HOME/.local/bin:$PATH" && '
                "claude --version"
            ),
        )

    def get_version_command(self) -> str | None:
        return 'export PATH="$HOME/.local/bin:$PATH"; claude --version'

    def parse_version(self, stdout: str) -> str:
        import re
        match = re.search(r"(\d+\.\d+\.\d+)", stdout.strip())
        return match.group(1) if match else stdout.strip()

    def _build_env(self) -> dict[str, str]:
        """Build environment variables for Claude Code processes."""
        env: dict[str, str] = {}

        api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or ""
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key

        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        if base_url:
            env["ANTHROPIC_BASE_URL"] = base_url

        if self.model_name:
            if "ANTHROPIC_BASE_URL" in env:
                env["ANTHROPIC_MODEL"] = self.model_name
            else:
                env["ANTHROPIC_MODEL"] = self.model_name.split("/")[-1]

        if "ANTHROPIC_BASE_URL" in env and "ANTHROPIC_MODEL" in env:
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = env["ANTHROPIC_MODEL"]
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = env["ANTHROPIC_MODEL"]
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = env["ANTHROPIC_MODEL"]
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = env["ANTHROPIC_MODEL"]

        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env["IS_SANDBOX"] = "1"
        env["FORCE_AUTO_BACKGROUND_TASKS"] = "1"
        env["ENABLE_BACKGROUND_TASKS"] = "1"

        max_thinking = os.environ.get("MAX_THINKING_TOKENS")
        if max_thinking:
            env["MAX_THINKING_TOKENS"] = max_thinking

        return env

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        env = self._build_env()

        agent_dir = EnvironmentPaths.agent_dir.as_posix()
        sessions_a = f"{agent_dir}/sessions-a"
        sessions_b = f"{agent_dir}/sessions-b"
        saved_collab = f"{agent_dir}/agent-collab.log"
        saved_a_log = f"{agent_dir}/agent-a-output.log"
        saved_b_log = f"{agent_dir}/agent-b-output.log"

        escaped_instruction = shlex.quote(instruction)
        escaped_prompt_a = shlex.quote(COLLAB_PROMPT_A)
        escaped_prompt_b = shlex.quote(COLLAB_PROMPT_B)

        setup_command = (
            f"mkdir -p {sessions_a}/debug {sessions_a}/projects/-app "
            f"{sessions_a}/shell-snapshots {sessions_a}/statsig "
            f"{sessions_a}/todos {sessions_a}/skills && "
            f"mkdir -p {sessions_b}/debug {sessions_b}/projects/-app "
            f"{sessions_b}/shell-snapshots {sessions_b}/statsig "
            f"{sessions_b}/todos {sessions_b}/skills && "
            f"touch {COLLAB_FILE}"
        )

        await self.exec_as_agent(environment, command=setup_command, env=env)

        run_command = (
            'export PATH="$HOME/.local/bin:$PATH"; '

            f"CLAUDE_CONFIG_DIR={sessions_a} "
            f"claude --verbose --output-format=stream-json "
            f"--permission-mode=bypassPermissions "
            f"--append-system-prompt {escaped_prompt_a} "
            f"--print -- {escaped_instruction} "
            f">{AGENT_A_LOG} 2>&1 & PID_A=$!; "

            f"CLAUDE_CONFIG_DIR={sessions_b} "
            f"claude --verbose --output-format=stream-json "
            f"--permission-mode=bypassPermissions "
            f"--append-system-prompt {escaped_prompt_b} "
            f"--print -- {escaped_instruction} "
            f">{AGENT_B_LOG} 2>&1 & PID_B=$!; "

            "wait $PID_A; EXIT_A=$?; "
            "wait $PID_B; EXIT_B=$?; "

            f"cp {COLLAB_FILE} {saved_collab} 2>/dev/null || true; "
            f"cp {AGENT_A_LOG} {saved_a_log} 2>/dev/null || true; "
            f"cp {AGENT_B_LOG} {saved_b_log} 2>/dev/null || true; "

            'echo "Agent A exit=$EXIT_A, Agent B exit=$EXIT_B"; '
            "[ $EXIT_A -eq 0 ] || [ $EXIT_B -eq 0 ]"
        )

        try:
            await self.exec_as_agent(environment, command=run_command, env=env)
        except Exception:
            save_cmd = (
                f"cp {COLLAB_FILE} {saved_collab} 2>/dev/null || true; "
                f"cp {AGENT_A_LOG} {saved_a_log} 2>/dev/null || true; "
                f"cp {AGENT_B_LOG} {saved_b_log} 2>/dev/null || true"
            )
            try:
                await self.exec_as_agent(environment, command=save_cmd, env={})
            except Exception:
                pass

    def populate_context_post_run(self, context: AgentContext) -> None:
        collab_log = self.logs_dir / "agent-collab.log"
        if collab_log.exists():
            text = collab_log.read_text()
            n_lines = len([l for l in text.strip().splitlines() if l.strip()])
            print(f"Collaboration log: {n_lines} messages, {len(text)} bytes")
        else:
            print("No collaboration log found")

        total_input = 0
        total_output = 0
        for suffix in ("sessions-a", "sessions-b"):
            session_dir = self.logs_dir / suffix
            if not session_dir.exists():
                continue
            for jsonl_file in session_dir.rglob("*.jsonl"):
                try:
                    for line in jsonl_file.read_text().splitlines():
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        msg = event.get("message", {})
                        if isinstance(msg, dict):
                            usage = msg.get("usage", {})
                            if isinstance(usage, dict):
                                total_input += usage.get("input_tokens", 0)
                                total_input += usage.get("cache_read_input_tokens", 0)
                                total_input += usage.get("cache_creation_input_tokens", 0)
                                total_output += usage.get("output_tokens", 0)
                except Exception:
                    continue

        context.n_input_tokens = total_input
        context.n_output_tokens = total_output
