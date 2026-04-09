"""Duo agent: two Claude Code instances collaborating via a shared append-only log file."""

from __future__ import annotations

import json
import os
import shlex
import textwrap
from typing import Any

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths

CLAUDE_CODE_VERSION = "2.1.92"
COLLAB_FILE = "/tmp/agent-collab.log"
AGENT_A_LOG = "/tmp/agent-a.log"
AGENT_B_LOG = "/tmp/agent-b.log"

COLLAB_PROMPT = textwrap.dedent("""\
    You are agent {id}, one of two AI agents working on the same task in the same \
    environment.

    You share an append-only log file at {collab_file} for communication.
    - Write: echo "[{id}|$(date -u +%H:%M:%S)] <msg>" >> {collab_file}
    - Read: cat {collab_file}

    Communicate constantly — read the log and post an update at each step:
    1. Before starting: share your plan, read the other agent's plan, agree \
    on who writes which files.
    2. Before creating any file: read the log and check the filesystem. If \
    the other agent already created it, do NOT overwrite it — "fixing" \
    correct work with an incorrect fix has destroyed solutions repeatedly. \
    If you think it's wrong, explain why in the log.
    3. Before running anything expensive (compilation, model fitting): read \
    the log — simultaneous heavy operations cause OOM kills.
    4. After completing a step: post what you did and what you found.
    5. When verifying: form your own complete answer independently BEFORE \
    looking at the other agent's output, then compare. Simply reading their \
    output and confirming it "looks right" misses errors.

    Disagreements are valuable — if your analysis contradicts the other \
    agent's, say so in the log and resolve it before producing final output.
""")


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

        api_key = (
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or ""
        )
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
        prompt_p = COLLAB_PROMPT.format(id="p", collab_file=COLLAB_FILE)
        prompt_q = COLLAB_PROMPT.format(id="q", collab_file=COLLAB_FILE)
        escaped_prompt_a = shlex.quote(prompt_p)
        escaped_prompt_b = shlex.quote(prompt_q)

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
                                total_input += usage.get(
                                    "cache_creation_input_tokens", 0
                                )
                                total_output += usage.get("output_tokens", 0)
                except Exception:
                    continue

        context.n_input_tokens = total_input
        context.n_output_tokens = total_output
