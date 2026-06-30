"""Programmatic Claude Agent SDK hooks wired by ClaudeProvider.

Two hooks are wired today:

1. ``UserPromptSubmit`` injects two things into the model's context
   before it sees a user turn.
   a. Compact runtime context: today's date, active workspace, and GWS
      profile. Keeps schedules and reconnected sessions in sync without
      Raffa having to restate them.
   b. Vault entity tags: whole-word matches against memory-vault/INDEX.md
      get surfaced as ``- [[People/Name]] (person)`` bullets so the model
      can load the right note without guessing who "Emma" or "Ciao-
      Improvements" refers to.
2. ``PostToolUse`` on the ``WebSearch`` tool backfills results on
   Ollama-cloud-routed chats, where the Anthropic-compat layer doesn't
   execute the server-side ``web_search`` tool. See
   :func:`build_web_search_post_tooluse_hook`.

Kept small and fail-open: any exception becomes a DEBUG log and the
original prompt/tool output reaches the model untouched.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.context.entity_tagger import find_entities, format_entities

logger = logging.getLogger(__name__)


def _legacy_workspace_context(raw: str | None) -> str:
    """Return old-style logical workspace values carried in CIAO_WORKSPACE.

    Historically Ciao used ``CIAO_WORKSPACE=personal|work`` in provider env.
    Public setup needs ``CIAO_WORKSPACE`` to be a filesystem path, so only
    preserve the two legacy context values here.
    """
    value = (raw or "").strip()
    return value if value in {"personal", "work"} else ""


def _runtime_lines(cwd: Path, extra_env: dict[str, str] | None = None) -> list[str]:
    """Collect non-empty key=value runtime context lines.

    ``extra_env`` is the per-request env the provider hands the spawned CLI
    (workspace, GWS profile, active project). The hook callback runs in the
    ciao server process, so ``os.environ`` only ever holds the global
    defaults; merging ``extra_env`` on top is what makes ``workspace=`` track
    the active chat instead of always reading ``personal``. Mirrors
    ``ciao.providers.base.build_runtime_context``.
    """
    env = {**os.environ, **(extra_env or {})}
    lines = [f"today={datetime.now(UTC).date().isoformat()}"]
    workspace = (
        env.get("CIAO_ACTIVE_WORKSPACE")
        or _legacy_workspace_context(env.get("CIAO_WORKSPACE"))
        or env.get("GWS_PROFILE")
    )
    if workspace:
        lines.append(f"workspace={workspace}")
    gws = env.get("GWS_PROFILE")
    if gws and gws != workspace:
        lines.append(f"gws_profile={gws}")
    project = env.get("CIAO_ACTIVE_PROJECT")
    if project:
        lines.append(f"active_project={project}")
    lines.append(f"cwd={cwd}")
    return lines


def build_user_prompt_submit_hook(
    vault_root: Path, extra_env: dict[str, str] | None = None
):
    """Return a UserPromptSubmit callback bound to a vault root.

    ``extra_env`` carries the per-request workspace/profile/project the
    provider built for this chat (see ``_build_extra_env``). Captured in the
    closure so the injected ``<ciao-runtime>`` block reflects the active chat
    rather than the server's global default. The callback shape matches
    claude_agent_sdk.types.HookCallback.
    """

    async def on_user_prompt_submit(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: Any,  # HookContext; untyped here to avoid an import cycle
    ) -> dict[str, Any]:
        del tool_use_id, context  # unused
        try:
            prompt = input_data.get("prompt") or ""
            cwd = Path(input_data.get("cwd") or vault_root.parent)
            runtime = _runtime_lines(cwd, extra_env)
            env = {**os.environ, **(extra_env or {})}
            workspace = (
                env.get("CIAO_ACTIVE_WORKSPACE")
                or _legacy_workspace_context(env.get("CIAO_WORKSPACE"))
                or env.get("GWS_PROFILE")
            )
            entities = find_entities(prompt, vault_root, workspace=workspace)
            sections: list[str] = []
            sections.append("<ciao-runtime>\n" + "\n".join(runtime) + "\n</ciao-runtime>")
            tagged = format_entities(entities)
            if tagged:
                sections.append("<ciao-entities>\n" + tagged + "\n</ciao-entities>")
            additional = "\n".join(sections)
        except Exception:  # noqa: BLE001 — never block a user turn on hook failure
            logger.debug("UserPromptSubmit hook failed; skipping", exc_info=True)
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": additional,
            }
        }

    return on_user_prompt_submit


# --- WebSearch backfill via Ollama -------------------------------------------
#
# On chats routed to Ollama's Anthropic-compatible endpoint, Claude Code's
# built-in WebSearch returns an empty boilerplate string instead of results.
# The Anthropic server-side ``web_search`` tool is not executed by Ollama's
# compat layer (only the ``ollama launch claude`` wrapper supplies that glue),
# so the CLI's WebSearch "succeeds" with no sources. See the analysis in
# memory-vault/personal/projects/active/ciao-improvements/README.md.
#
# This PostToolUse hook detects that empty result, runs Ollama's standalone
# /api/web_search with the same query, and injects the real results as
# additionalContext. No-op on the Anthropic path (where WebSearch works
# natively) and on local-daemon Ollama routes (which don't expose the
# standalone search API). Fail-open: any error returns {} so the model still
# gets the original output.


_WEBSEARCH_RESULT_BEARER_PREFIX = "Bearer "


def _websearch_response_has_results(tool_response: Any) -> bool:
    """True when a WebSearch tool_response already carries real results.

    Real web search results always contain ``http`` links. The Ollama-compat
    empty boilerplate (``"I'll search the web for that query right away.
    REMINDER: You MUST include the sources above..."``) contains none, so the
    presence of ``http`` is a reliable signal that native WebSearch worked and
    we should not override.
    """
    if tool_response is None:
        return False
    if isinstance(tool_response, str):
        text = tool_response
    else:
        try:
            text = json.dumps(tool_response, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(tool_response)
    return "http" in text


def _ollama_cloud_route(
    extra_env: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    """Return ``(base_url, api_key)`` when this request hits Ollama cloud.

    Only the cloud endpoint (``ollama.com``) exposes the standalone
    ``/api/web_search`` REST API used to backfill WebSearch; the local daemon
    route (``localhost:11434`` with the literal ``"ollama"`` token) is skipped.
    Returns ``None`` when the chat is not Ollama-cloud-routed so the hook
    no-ops and native WebSearch is left untouched.

    Honours ``CIAO_OLLAMA_WEBSEARCH_HOOK`` (default enabled) as a kill switch.
    """
    extra = extra_env or {}
    # Kill switch is server-wide config (os.environ) with a per-chat override
    # (extra_env). extra_env wins so a chat can opt out even when the server
    # default is on.
    flag = {**os.environ, **extra}.get("CIAO_OLLAMA_WEBSEARCH_HOOK", "1")
    if flag not in ("1", "true", "True"):
        return None
    # Routing is per-chat: only extra_env carries ANTHROPIC_BASE_URL for
    # Ollama-routed chats (see ciao.providers.ollama.ollama_env_for_model).
    # Do NOT read os.environ here — a server-wide base_url must not leak into
    # non-Ollama chats and trigger a spurious search backfill.
    base_url = extra.get("ANTHROPIC_BASE_URL", "")
    api_key = extra.get("ANTHROPIC_AUTH_TOKEN", "")
    if "ollama.com" in base_url and api_key:
        return base_url, api_key
    return None


def _ollama_web_search(
    base_url: str, api_key: str, query: str, timeout_s: float = 10.0
) -> list[dict[str, str]]:
    """Call Ollama's standalone web search API. Returns ``[]`` on any failure.

    Endpoint: ``POST {base_url}/api/web_search`` with ``Authorization: Bearer
    <api_key>`` and a JSON body ``{"query": "..."}``. Response shape is
    ``{"results": [{"title", "url", "content"}, ...]}``.
    """
    url = base_url.rstrip("/") + "/api/web_search"
    payload = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": _WEBSEARCH_RESULT_BEARER_PREFIX + api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError, TimeoutError) as exc:
        logger.info("Ollama web search failed for %r: %s", query, exc)
        return []
    results = data.get("results") if isinstance(data, dict) else None
    return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []


def _format_search_results(
    query: str, results: list[dict[str, str]], max_results: int = 5, content_chars: int = 400
) -> str:
    """Render Ollama search results as a single additionalContext string.

    Capped at ``max_results`` items with each ``content`` truncated to
    ``content_chars`` so the whole block stays well under the 10,000-char
    additionalContext limit.
    """
    lines = [f'Web search results for "{query}" (via Ollama):', ""]
    for i, r in enumerate(results[:max_results], 1):
        title = str(r.get("title", "")).strip()
        url = str(r.get("url", "")).strip()
        content = str(r.get("content", "")).strip()
        if len(content) > content_chars:
            content = content[:content_chars].rstrip() + "..."
        lines.append(f"{i}. {title}")
        if url:
            lines.append(f"   {url}")
        if content:
            lines.append(f"   {content}")
        lines.append("")
    return "\n".join(lines).strip()


def build_web_search_post_tooluse_hook(extra_env: dict[str, str] | None = None):
    """Return a PostToolUse callback that backfills WebSearch via Ollama.

    Fires after Claude Code's WebSearch tool runs. When the chat is
    Ollama-cloud-routed and WebSearch returned an empty result, runs Ollama's
    standalone ``/api/web_search`` with the same query and injects the real
    results as ``additionalContext``. No-op otherwise.
    """

    async def on_post_tool_use(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: Any,  # HookContext; untyped here to avoid an import cycle
    ) -> dict[str, Any]:
        del tool_use_id, context  # unused
        try:
            if input_data.get("tool_name") != "WebSearch":
                return {}
            if _websearch_response_has_results(input_data.get("tool_response")):
                return {}  # native WebSearch worked (e.g. Anthropic path)
            route = _ollama_cloud_route(extra_env)
            if route is None:
                return {}  # not Ollama-cloud-routed or kill switch off
            base_url, api_key = route
            query = (input_data.get("tool_input") or {}).get("query", "")
            if not query:
                return {}
            results = _ollama_web_search(base_url, api_key, query)
            if not results:
                return {}
            additional = _format_search_results(query, results)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": additional,
                }
            }
        except Exception:  # noqa: BLE001 — never break a tool turn on hook failure
            logger.debug("WebSearch PostToolUse hook failed; skipping", exc_info=True)
        return {}

    return on_post_tool_use
