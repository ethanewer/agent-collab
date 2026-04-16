"""Plan agent: Claude Code with plan-then-build flow.

Step 1 (plan):  claude --permission-mode plan --output-format json
  Claude explores the codebase read-only and produces a plan via ExitPlanMode.
  The plan is extracted from the last permission_denial that has a plan key
  (the ExitPlanMode denial). Earlier denials are tool-use blocks denied in
  read-only plan mode.

Step 2 (build): claude --resume SESSION_ID --dangerously-skip-permissions
  Resumes the same session (preserving planning context) with an approval
  message that mirrors the interactive CLI's tool_result on plan approval.
"""

from __future__ import annotations

import json
import os
import shlex
import textwrap

from harbor.agents.installed.claude_code import ClaudeCode, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths

CLAUDE_CODE_VERSION = "2.1.109"

CCPLAN_SCRIPT = textwrap.dedent("""\
    #!/usr/bin/env bash
    set -euo pipefail
    export PATH="$HOME/.local/bin:$PATH"

    INSTRUCTION="$1"
    EXTRA_FLAGS="${2:-}"
    PLAN_JSON="/logs/agent/plan-output.json"
    PLAN_LOG="/logs/agent/plan-stderr.log"
    BUILD_LOG="/logs/agent/claude-code.txt"

    # ── Step 1: Plan ───────────────────────────────────────────
    echo "=== PLAN STEP ===" >&2
    claude --output-format=json --permission-mode=plan \
        $EXTRA_FLAGS \
        --print -- "$INSTRUCTION" \
        >"$PLAN_JSON" 2>"$PLAN_LOG" || true

    SESSION_ID=$(jq -r '.session_id // empty' "$PLAN_JSON")
    PLAN_SUBTYPE=$(jq -r '.subtype // empty' "$PLAN_JSON")
    PLAN_DENIALS=$(jq '.permission_denials | length' "$PLAN_JSON")
    # The ExitPlanMode denial (with .plan key) is the last one; earlier denials
    # are tool-use blocks that were denied in read-only plan mode.
    PLAN_TEXT=$(jq -r '[.permission_denials[] | select(.tool_input.plan != null)] | last | .tool_input.plan // empty' "$PLAN_JSON")
    PLAN_FILE_PATH=$(jq -r '[.permission_denials[] | select(.tool_input.plan != null)] | last | .tool_input.planFilePath // empty' "$PLAN_JSON")
    PLAN_COST=$(jq -r '.total_cost_usd // 0' "$PLAN_JSON")
    PLAN_TURNS=$(jq -r '.num_turns // 0' "$PLAN_JSON")
    PLAN_DURATION=$(jq -r '.duration_ms // 0' "$PLAN_JSON")

    echo "  session:  $SESSION_ID" >&2
    echo "  subtype:  $PLAN_SUBTYPE" >&2
    echo "  denials:  $PLAN_DENIALS" >&2
    echo "  cost:     \\$$PLAN_COST" >&2
    echo "  turns:    $PLAN_TURNS" >&2
    echo "  duration: ${PLAN_DURATION}ms" >&2
    echo "  plan_len: ${#PLAN_TEXT} chars" >&2

    if [ -z "$SESSION_ID" ] || [ -z "$PLAN_TEXT" ]; then
        echo "ERROR: Plan step failed (subtype=$PLAN_SUBTYPE, denials=$PLAN_DENIALS)" >&2
        cat "$PLAN_JSON" >&2
        exit 1
    fi

    # ── Construct approval message ─────────────────────────────
    APPROVAL="User has approved your plan. You can now start coding. Start with updating your todo list if applicable"

    if [ -n "$PLAN_FILE_PATH" ]; then
        APPROVAL="${APPROVAL}

Your plan has been saved to: ${PLAN_FILE_PATH}
You can refer back to it if needed during implementation."
    fi

    APPROVAL="${APPROVAL}

## Approved Plan:
${PLAN_TEXT}"

    # ── Step 2: Build ──────────────────────────────────────────
    echo "=== BUILD STEP ===" >&2
    claude --verbose --output-format=stream-json \
        --dangerously-skip-permissions \
        --resume "$SESSION_ID" \
        $EXTRA_FLAGS \
        --print -- "$APPROVAL" 2>&1 </dev/null | tee "$BUILD_LOG"
""")


class PlanClaudeCode(ClaudeCode):
    """Claude Code with plan-then-build: plan in read-only mode, then execute."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("version", CLAUDE_CODE_VERSION)
        super().__init__(*args, **kwargs)

    @staticmethod
    def name() -> str:
        return "claude-code-plan"

    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        await self.exec_as_root(
            environment,
            command=(
                "if command -v apk &> /dev/null; then"
                "  apk add --no-cache jq;"
                " elif command -v apt-get &> /dev/null; then"
                "  apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y jq;"
                " elif command -v yum &> /dev/null; then"
                "  yum install -y jq;"
                " fi"
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )

    @with_prompt_template
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        escaped_instruction = shlex.quote(instruction)

        use_bedrock = self._is_bedrock_mode()

        env = {
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or "",
            "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", None),
            "CLAUDE_CODE_OAUTH_TOKEN": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": os.environ.get(
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS", None
            ),
            "FORCE_AUTO_BACKGROUND_TASKS": "1",
            "ENABLE_BACKGROUND_TASKS": "1",
        }

        if use_bedrock:
            env["CLAUDE_CODE_USE_BEDROCK"] = "1"
            bedrock_token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")
            if bedrock_token:
                env["AWS_BEARER_TOKEN_BEDROCK"] = bedrock_token
            for aws_var in (
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "AWS_PROFILE",
            ):
                val = os.environ.get(aws_var, "")
                if val:
                    env[aws_var] = val
            env["AWS_REGION"] = os.environ.get("AWS_REGION", "us-east-1")
            small_model_region = os.environ.get(
                "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION", ""
            )
            if small_model_region:
                env["ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION"] = small_model_region
            if os.environ.get("DISABLE_PROMPT_CACHING", "").strip() == "1":
                env["DISABLE_PROMPT_CACHING"] = "1"

        env = {k: v for k, v in env.items() if v}

        if self.model_name:
            if use_bedrock:
                if "/" in self.model_name:
                    env["ANTHROPIC_MODEL"] = self.model_name.split("/", 1)[-1]
                else:
                    env["ANTHROPIC_MODEL"] = self.model_name
            elif "ANTHROPIC_BASE_URL" in env:
                env["ANTHROPIC_MODEL"] = self.model_name
            else:
                env["ANTHROPIC_MODEL"] = self.model_name.split("/")[-1]
        elif "ANTHROPIC_MODEL" in os.environ:
            env["ANTHROPIC_MODEL"] = os.environ["ANTHROPIC_MODEL"]

        if "ANTHROPIC_BASE_URL" in env and "ANTHROPIC_MODEL" in env:
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = env["ANTHROPIC_MODEL"]
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = env["ANTHROPIC_MODEL"]
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = env["ANTHROPIC_MODEL"]
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = env["ANTHROPIC_MODEL"]

        if os.environ.get("CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING", "").strip() == "1":
            env["CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING"] = "1"

        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env["IS_SANDBOX"] = "1"
        env.update(self._resolved_env_vars)
        env["CLAUDE_CONFIG_DIR"] = (EnvironmentPaths.agent_dir / "sessions").as_posix()

        setup_command = (
            "mkdir -p $CLAUDE_CONFIG_DIR/debug $CLAUDE_CONFIG_DIR/projects/-app "
            "$CLAUDE_CONFIG_DIR/shell-snapshots $CLAUDE_CONFIG_DIR/statsig "
            "$CLAUDE_CONFIG_DIR/todos $CLAUDE_CONFIG_DIR/skills && "
            "if [ -d ~/.claude/skills ]; then "
            "cp -r ~/.claude/skills/. $CLAUDE_CONFIG_DIR/skills/ 2>/dev/null || true; "
            "fi"
        )

        skills_command = self._build_register_skills_command()
        if skills_command:
            setup_command += f" && {skills_command}"

        mcp_command = self._build_register_mcp_servers_command()
        if mcp_command:
            setup_command += f" && {mcp_command}"

        cli_flags = self.build_cli_flags()
        extra_flags = shlex.quote(cli_flags) if cli_flags else "''"

        await self.exec_as_agent(environment, command=setup_command, env=env)

        escaped_script = shlex.quote(CCPLAN_SCRIPT)
        await self.exec_as_agent(
            environment,
            command=f"printf '%s' {escaped_script} > /tmp/ccplan.sh && chmod +x /tmp/ccplan.sh",
            env=env,
        )

        await self.exec_as_agent(
            environment,
            command=f"/tmp/ccplan.sh {escaped_instruction} {extra_flags}",
            env=env,
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        plan_output = self.logs_dir / "plan-output.json"
        if plan_output.exists():
            try:
                data = json.loads(plan_output.read_text())
                plan_cost = data.get("total_cost_usd", 0)
                plan_turns = data.get("num_turns", 0)
                plan_duration = data.get("duration_ms", 0)
                plan_denial = next(
                    (d for d in reversed(data.get("permission_denials") or [])
                     if "plan" in d.get("tool_input", {})),
                    {},
                )
                plan_len = len(plan_denial.get("tool_input", {}).get("plan", ""))
                print(
                    f"Plan step: cost=${plan_cost}, turns={plan_turns}, "
                    f"duration={plan_duration}ms, plan_len={plan_len} chars"
                )
            except Exception as exc:
                print(f"Failed to read plan output: {exc}")

        super().populate_context_post_run(context)
