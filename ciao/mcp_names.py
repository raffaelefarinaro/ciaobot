"""Pure claude.ai connector-name classification shared by the Claude adapter and its web hosts."""

_CLAUDEAI_PREFIX = "claude.ai "


def is_claudeai_connector(name: str) -> bool:
    """A claude.ai connector: listed by the claude.ai account, connected on claude.ai, never by ``claude mcp login``."""
    return name.startswith(_CLAUDEAI_PREFIX)
