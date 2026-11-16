"""Provider-specific tool summaries shared by live streams and transcript replay."""

import json
from collections.abc import Mapping
from typing import cast


def summarize_claude_tool_input(name: str, tool_input: dict) -> str:
    """Extract the most relevant piece from Claude tool input for display."""
    if not tool_input:
        return ""
    if name in ("Read", "read"):
        path: str = tool_input.get("file_path", "")
        return path
    if name in ("Edit", "edit"):
        path = tool_input.get("file_path") or tool_input.get("path") or ""
        return path
    if name in ("Write", "write"):
        path = tool_input.get("file_path") or tool_input.get("path") or ""
        return path
    if name in ("Grep", "grep"):
        pattern: str = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f'"{pattern}"' + (f" in {path}" if path else "")
    if name in ("Glob", "glob"):
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return pattern + (f" in {path}" if path else "")
    if name in ("Bash", "bash"):
        cmd: str = tool_input.get("command", "")
        desc: str = tool_input.get("description", "")
        if desc:
            return desc
        return cmd[:120] + ("..." if len(cmd) > 120 else "")
    if name in ("Agent", "agent"):
        desc = tool_input.get("description", "")
        subtype: str = tool_input.get("subagent_type", "")
        if subtype and desc:
            return f"[{subtype}] {desc}"
        return desc or subtype
    if name in ("TaskCreate",):
        return cast(str, tool_input.get("subject", ""))
    if name in ("TaskUpdate",):
        tid = tool_input.get("taskId", "")
        status = tool_input.get("status", "")
        return f"#{tid} → {status}" if status else f"#{tid}"
    if name in ("Skill",):
        return cast(str, tool_input.get("skill", ""))
    if name in ("AskUserQuestion",):
        questions = tool_input.get("questions")
        if questions:
            try:
                return json.dumps({"questions": questions}, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                return ""
        return ""
    if name in ("WebSearch",):
        return cast(str, tool_input.get("query", ""))
    if name in ("WebFetch",):
        return cast(str, tool_input.get("url", ""))[:100]
    if name in ("NotebookEdit",):
        return cast(str, tool_input.get("cell_id", ""))
    if "__" in name:
        for key in ("query", "text", "message", "url", "path", "name", "title"):
            val = tool_input.get(key, "")
            if val:
                return str(val)[:100]
    for val in tool_input.values():
        if isinstance(val, str) and val:
            return val[:100]
    return ""


def summarize_opencode_tool_input(tool: str, raw: object) -> str:
    """Short, non-secret one-liner describing an OpenCode tool call for the UI."""
    if not isinstance(raw, Mapping):
        return ""
    for key in ("filePath", "path", "file", "pattern", "query", "command", "description"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            lines = [line.strip() for line in value.splitlines() if line.strip()]
            more = f" (+{len(lines) - 1} lines)" if len(lines) > 1 else ""
            return lines[0][:400] + more
    return json.dumps(raw, ensure_ascii=False)[:200] if raw else ""


def opencode_file_touches(tool: str, raw: object) -> list[dict[str, str]] | None:
    """Paths an OpenCode tool call writes, for the PWA's file-change cards."""
    if not isinstance(raw, Mapping):
        return None
    if tool.lower() in {"shell", "bash"}:
        from ciao.web.chat_broker import extract_file_touches

        return extract_file_touches("bash", raw) or None
    action = {"write": "write", "edit": "edit", "patch": "edit"}.get(tool.lower())
    if action is None:
        return None
    path = raw.get("filePath") or raw.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    return [{"file_path": path.strip(), "action": action}]
