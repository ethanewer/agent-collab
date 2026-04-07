"""Control agent: single Claude Code with pinned version for reproducibility."""

from harbor.agents.installed.claude_code import ClaudeCode

CLAUDE_CODE_VERSION = "2.1.92"


class ControlClaudeCode(ClaudeCode):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("version", CLAUDE_CODE_VERSION)
        super().__init__(*args, **kwargs)

    @staticmethod
    def name() -> str:
        return "claude-code-control"
