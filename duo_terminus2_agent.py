"""Duo Terminus-2 agent: two Terminus-2 instances collaborating via a shared log file."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.agents.terminus_2 import Terminus2
from harbor.agents.terminus_2.tmux_session import TmuxSession
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths

COLLAB_FILE = "/tmp/agent-collab.log"

COLLAB_PROMPT_SUFFIX = textwrap.dedent("""\

    === COLLABORATION INSTRUCTIONS ===
    You are agent {id}, one of two AI agents working on the same task in the same \
    environment.

    You share an append-only log file at {collab_file} for communication.
    - Write: echo "[{id}|$(date -u +%H:%M:%S)] <msg>" >> {collab_file}
      (include this as a command in your commands array)
    - Read: cat {collab_file}
      (include this as a command to check what the other agent has written)

    Communicate constantly. Always be aware of what the other agent has done, \
    is currently doing, and plans to do.

    Your goal is to produce better results together than either of you could \
    alone. Divide work, verify independently, and build on each other's \
    contributions. In every successful collaboration, agents built on each \
    other's work. In every failed one, an agent replaced the other's work \
    — confident they were improving it, but wrong. When you disagree, talk \
    it out in the log.
    === END COLLABORATION INSTRUCTIONS ===
""")


class DuoTerminus2(BaseAgent):
    SUPPORTS_ATIF: bool = False

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        max_turns: int | None = None,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        max_thinking_tokens: int | None = None,
        extra_env: dict[str, str] | None = None,
        *args,
        **kwargs,
    ):
        super().__init__(logs_dir, model_name=model_name, *args, **kwargs)

        self._extra_env = extra_env or {}
        self._max_turns = max_turns
        self._temperature = temperature
        self._reasoning_effort = reasoning_effort
        self._max_thinking_tokens = max_thinking_tokens

        logs_p = logs_dir / "agent-p"
        logs_q = logs_dir / "agent-q"
        logs_p.mkdir(parents=True, exist_ok=True)
        logs_q.mkdir(parents=True, exist_ok=True)

        agent_kwargs: dict[str, Any] = {
            "model_name": model_name,
            "temperature": temperature,
            "record_terminal_session": False,
            "enable_summarize": True,
        }
        if max_turns is not None:
            agent_kwargs["max_turns"] = max_turns
        if reasoning_effort is not None:
            agent_kwargs["reasoning_effort"] = reasoning_effort
        if max_thinking_tokens is not None:
            agent_kwargs["max_thinking_tokens"] = max_thinking_tokens
        if extra_env:
            agent_kwargs["extra_env"] = extra_env

        self._agent_p = Terminus2(logs_dir=logs_p, **agent_kwargs)
        self._agent_q = Terminus2(logs_dir=logs_q, **agent_kwargs)

    @staticmethod
    def name() -> str:
        return "terminus-2-duo"

    def version(self) -> str | None:
        return "2.0.0-duo"

    async def setup(self, environment: BaseEnvironment) -> None:
        # Create the shared collab file
        await environment.exec(command=f"touch {COLLAB_FILE}", user="root")

        # Set up two separate tmux sessions in the same container
        agent_dir = EnvironmentPaths.agent_dir.as_posix()

        self._session_p = TmuxSession(
            session_name="agent-p",
            environment=environment,
            logging_path=EnvironmentPaths.agent_dir / "terminus_2_p.pane",
            local_asciinema_recording_path=None,
            remote_asciinema_recording_path=None,
            pane_width=160,
            pane_height=40,
            extra_env=self._extra_env,
            user=environment.default_user,
        )

        self._session_q = TmuxSession(
            session_name="agent-q",
            environment=environment,
            logging_path=EnvironmentPaths.agent_dir / "terminus_2_q.pane",
            local_asciinema_recording_path=None,
            remote_asciinema_recording_path=None,
            pane_width=160,
            pane_height=40,
            extra_env=self._extra_env,
            user=environment.default_user,
        )

        await self._session_p.start()
        await self._session_q.start()

        # Inject sessions into the agents
        self._agent_p._session = self._session_p
        self._agent_q._session = self._session_q

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        collab_suffix_p = COLLAB_PROMPT_SUFFIX.format(id="p", collab_file=COLLAB_FILE)
        collab_suffix_q = COLLAB_PROMPT_SUFFIX.format(id="q", collab_file=COLLAB_FILE)

        instruction_p = instruction + collab_suffix_p
        instruction_q = instruction + collab_suffix_q

        context_p = AgentContext()
        context_q = AgentContext()

        async def run_agent_p():
            try:
                await self._agent_p.run(instruction_p, environment, context_p)
            except Exception as e:
                self.logger.error(f"Agent P error: {e}")

        async def run_agent_q():
            try:
                await self._agent_q.run(instruction_q, environment, context_q)
            except Exception as e:
                self.logger.error(f"Agent Q error: {e}")

        await asyncio.gather(run_agent_p(), run_agent_q())

        # Merge token counts from both agents
        context.n_input_tokens = (context_p.n_input_tokens or 0) + (context_q.n_input_tokens or 0)
        context.n_output_tokens = (context_p.n_output_tokens or 0) + (context_q.n_output_tokens or 0)
        context.n_cache_tokens = (context_p.n_cache_tokens or 0) + (context_q.n_cache_tokens or 0)

        cost_p = context_p.cost_usd or 0
        cost_q = context_q.cost_usd or 0
        if cost_p > 0 or cost_q > 0:
            context.cost_usd = cost_p + cost_q

        # Save the collab log
        try:
            result = await environment.exec(command=f"cat {COLLAB_FILE}", user=environment.default_user)
            if result.stdout:
                collab_log_path = self.logs_dir / "agent-collab.log"
                collab_log_path.write_text(result.stdout)
                n_lines = len([l for l in result.stdout.strip().splitlines() if l.strip()])
                print(f"Collaboration log: {n_lines} messages, {len(result.stdout)} bytes")
        except Exception:
            print("No collaboration log found")
