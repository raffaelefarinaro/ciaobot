"""Post-archive session insights extraction.

When a chat is archived, the user/assistant text turns are rendered to
``memory-vault/Logs/Chats/<context>/claude/<file>.md`` by
``TranscriptStore.archive_session``. That renderer drops everything that
isn't plain text: tool_use, tool_result, thinking blocks, errors, retries.

This module mines the raw Claude Code session JSONL (at
``~/.claude/projects/-home-ubuntu-ciao/<session-id>.jsonl``) for the
durable signal those layers contain, runs it through a fast cheap model
(DeepSeek Flash by default), and appends a ``## Session insights``
section to the archived markdown. Downstream consumers (memory curation,
work daily log, weekly review) read that section instead of mining the
JSONL themselves.

The flow is split in two phases for safety:

* :func:`filter_session_jsonl` runs synchronously inside ``archive_chat``
  before ``delete_sdk_session_blob`` removes the JSONL from disk. It
  reads the file, drops noise, truncates large read-only tool_result
  bodies, and returns a much smaller string ready for the model.
* :func:`extract_and_append` runs asynchronously via
  ``asyncio.create_task`` from the route handler. It calls the model,
  retries once on failure, and appends the result to the archive file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from ciao import job_runs
from ciao.providers.ollama import OllamaSettings
from ciao.transcripts import _claude_projects_dir

logger = logging.getLogger(__name__)


def resolve_insights_model(config, workspace: str | None = None) -> str:
    """Pick the model for session-insights extraction.

    When the operator has not set an explicit override (Settings → Models →
    Session insights = Automatic), use the sonnet-tier model for the chat's
    workspace routing bucket. Scripts without workspace context fall back to
    ``config.insights_model``.
    """
    if config.insights_model_override:
        return config.insights_model_override
    if workspace is not None:
        return config.sonnet_model_for_workspace(workspace)
    return config.insights_model


_INSIGHTS_HEADER = "## Session insights"
_RETRY_DELAY_S = 30
_READ_TOOL_TRUNCATE_CHARS = 200
_KEEP_FULL_TOOLS = frozenset({"Edit", "Write", "Bash", "Task", "NotebookEdit"})
_TRUNCATE_TOOLS = frozenset({"Read", "Glob", "Grep", "WebFetch", "WebSearch"})


_INSIGHTS_SYSTEM_PROMPT = """\
You are extracting durable signal from a Claude Code session transcript.
The user is the workspace owner. Output Markdown with the exact section headers below.
Omit a section entirely if empty - do NOT write "none" or "n/a".
Cite the message index `[idx=N]` for every claim. Do not invent facts.
Do not summarise the conversation - that is already saved.

Rules:
- Skip routine successful tool calls.
- Skip anything obvious from user/assistant text alone.
- "Errors" = tool/model/system failure, not just things the user disliked.
- "User corrections" = the user pushed back, redirected, or rejected an approach.
- "New entities" = people/projects/places/products mentioned for the first time, not generic nouns.
- When citing wikilinks, use bare [[Target]] or [[Target|Display]] syntax. Do NOT wrap wikilinks in backticks, quotes, or other formatting.
- Be terse. One line per item where possible.

## Errors
- <what failed> -> <how it was resolved, or "unresolved"> [idx=N]

## Dead ends
- Tried <approach>; blocked by <reason>; switched to <alternative>. [idx=N]

## User corrections
- User said: "<short quote>" -> assistant changed <what>. [idx=N]

## New entities
- <type>: <name> - <one-line context>. [idx=N]

## Decisions
- Chose <X> over <Y> because <reason>. [idx=N]

## Reusable snippets
- <one-line description>:
  ```<lang>
  <command/query/config>
  ```

## Open loops
- <thing left undone, with any deadline or condition>. [idx=N]

## Vault changes
- <path> - <one-line summary of edit>. [idx=N]
"""


def filter_session_jsonl(workspace_root: Path, session_id: str) -> str | None:
    """Read and pre-filter a Claude Code session JSONL into a compact string.

    Returns None when the file doesn't exist or can't be parsed.
    The returned string is line-oriented JSON (one filtered record per
    line) ready to be passed to the model as the user prompt body.

    Filter rules:
    - Drop sidechain entries, system pings, hook outputs, summary records.
    - Keep user messages, assistant text/thinking blocks in full.
    - Keep Edit/Write/Bash/Task tool_use and matching tool_result in full.
    - Keep any tool_result with is_error=true in full.
    - Truncate Read/Glob/Grep/WebFetch tool_result bodies to a head + size.
    - Annotate every kept message with a sequential ``idx`` for citation.
    """
    if not session_id:
        return None
    path = _claude_projects_dir(workspace_root) / f"{session_id}.jsonl"
    if not path.exists():
        return None

    truncate_tool_use_ids: set[str] = set()
    out_lines: list[str] = []
    idx = 0
    try:
        with path.open(encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("isSidechain"):
                    continue
                otype = obj.get("type")
                if otype not in {"user", "assistant"}:
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue

                content = msg.get("content")
                kept_blocks = _filter_blocks(content, truncate_tool_use_ids)
                if not kept_blocks:
                    continue

                idx += 1
                record = {
                    "idx": idx,
                    "type": otype,
                    "ts": obj.get("timestamp", ""),
                    "content": kept_blocks,
                }
                out_lines.append(json.dumps(record, ensure_ascii=False))
    except OSError:
        logger.exception("Could not read session JSONL at %s", path)
        return None

    if not out_lines:
        return None
    return "\n".join(out_lines)


def _filter_blocks(content: object, truncate_tool_use_ids: set[str]) -> list[object]:
    """Keep durable blocks; truncate read-only tool_result bodies."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    if not isinstance(content, list):
        return []

    kept: list[object] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if isinstance(text, str) and text.strip():
                kept.append({"type": "text", "text": text})
        elif btype == "thinking":
            thinking = block.get("thinking", "")
            if isinstance(thinking, str) and thinking.strip():
                kept.append({"type": "thinking", "text": thinking})
        elif btype == "tool_use":
            name = block.get("name", "")
            tool_id = block.get("id", "")
            tool_input = block.get("input", {})
            kept.append({
                "type": "tool_use",
                "name": name,
                "id": tool_id,
                "input": _summarise_tool_input(name, tool_input),
            })
            if isinstance(tool_id, str) and name in _TRUNCATE_TOOLS:
                truncate_tool_use_ids.add(tool_id)
        elif btype == "tool_result":
            kept.append(_filter_tool_result(block, truncate_tool_use_ids))
    return kept


def _summarise_tool_input(name: str, tool_input: object) -> object:
    """Keep tool inputs small. Edit/Write keep full content; Read keeps path only."""
    if not isinstance(tool_input, dict):
        return tool_input
    if name in _KEEP_FULL_TOOLS:
        return tool_input
    if name in _TRUNCATE_TOOLS:
        keep_keys = ("file_path", "path", "pattern", "url", "query")
        return {k: tool_input[k] for k in keep_keys if k in tool_input}
    return tool_input


def _filter_tool_result(
    block: dict, truncate_tool_use_ids: set[str]
) -> dict:
    """Truncate read-only tool_result content; keep errors and writes in full."""
    tool_use_id = block.get("tool_use_id", "")
    is_error = bool(block.get("is_error"))
    raw_content = block.get("content")

    if is_error or tool_use_id not in truncate_tool_use_ids:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "is_error": is_error,
            "content": _stringify_content(raw_content),
        }

    text = _stringify_content(raw_content)
    full_len = len(text)
    truncated = text[:_READ_TOOL_TRUNCATE_CHARS]
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "is_error": False,
        "content": f"{truncated}…[truncated, total={full_len} chars]",
    }


def _stringify_content(content: object) -> str:
    """Flatten tool_result content into a single string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


async def extract_and_append(
    *,
    archive_path: Path,
    filtered_jsonl: str,
    config,
    model: str,
    session_id: str = "",
    trajectory_meta: dict[str, str] | None = None,
    workspace_root: Path | None = None,
    vault_root: Path | None = None,
    trajectories_enabled: bool = True,
    memory_proposals_enabled: bool = True,
    provider: str = "claude",
    project_doc_path: str = "",
) -> None:
    """Call the model with the filtered transcript and append insights to the archive.

    Idempotent: skips if the archive already contains a Session insights
    section. Retries once on failure (30s delay), then logs and skips.
    Always swallows exceptions — this runs as a fire-and-forget task and
    must never crash the route or leave the archive corrupted.

    The model call goes through a one-shot ``claude_agent_sdk.query()``
    call, routed to the configured upstream (Ollama / OpenRouter /
    Anthropic) via the ``env`` dict.

    When ``trajectories_enabled" and ``session_id`` are set, a JSON
    trajectory is written to ``~/.ciao/trajectories/YYYY-MM/`` after the
    insights section is appended. The trajectory uses the model output
    to populate decisions/errors/user_corrections and the filtered JSONL
    to populate tools_used/skills_loaded/turns. ``trajectory_meta`` may
    carry ``context``, ``project_id``, ``chat_id``, ``task_summary``,
    ``workspace``; missing keys default to empty strings.

    ``project_doc_path`` (workspace-root-relative or absolute) points at the
    chat's canonical project doc; when set and the extracted insights carry
    Decisions or Open loops, the doc is updated in place right away via
    :mod:`ciao.project_doc_update` instead of waiting for the nightly
    curation schedule.
    """
    output = ""
    try:
        if not archive_path.exists():
            logger.warning("Archive path %s missing, skipping insights", archive_path)
            return
        if _has_insights_section(archive_path):
            logger.info("Archive %s already has insights, skipping", archive_path)
            return

        if provider == "codex":
            effective_model, env, note = model, {}, None
        else:
            from ciao.providers.routing import resolve_with_fallback

            effective_model, env, note = resolve_with_fallback(
                model, config, default_model=config.insights_model
            )
        async with job_runs.track(
            "insights", "Session insights", model=effective_model,
            extra={"archive": archive_path.name, "session_id": session_id},
        ) as run:
            if note:
                run.extra["fallback"] = note
                logger.info("Insights %s", note)
            output = await _run_model_with_retry(
                filtered_jsonl=filtered_jsonl,
                model=effective_model,
                env=env,
                provider=provider,
                cwd=workspace_root,
            )
            if output:
                _append_section(archive_path, output)
                logger.info("Appended session insights to %s", archive_path)
            else:
                run.status = "error"
                run.error = "insights model returned no output (failed twice)"

        # Canonical project doc: fold Decisions/Open loops into the chat's
        # project doc while the insights are fresh. The nightly curation
        # schedule remains the cross-chat consolidator.
        if output and project_doc_path:
            try:
                from ciao.project_doc_update import update_project_doc

                doc = Path(project_doc_path)
                if not doc.is_absolute() and workspace_root is not None:
                    doc = workspace_root / project_doc_path
                async with job_runs.track(
                    "project_doc_update", "Project doc update",
                    model=effective_model,
                    extra={"doc": str(doc), "archive": archive_path.name},
                ) as run:
                    wrote = await update_project_doc(
                        doc_path=doc,
                        insights_md=output,
                        model=effective_model,
                        env=env,
                        provider=provider,
                        cwd=workspace_root,
                    )
                    run.extra["wrote"] = wrote
                    if not wrote:
                        run.skip("no material changes for the project doc")
            except Exception:  # noqa: BLE001 — never crash the loop
                logger.exception(
                    "Project doc update failed for %s", project_doc_path
                )
    except Exception:  # noqa: BLE001 — fire-and-forget, never crash the loop
        logger.exception("Insights extraction failed for %s", archive_path)
    finally:
        if trajectories_enabled and session_id and filtered_jsonl:
            try:
                from ciao.trajectory_builder import build_and_persist_trajectory

                meta = trajectory_meta or {}
                with job_runs.track_sync(
                    "trajectory", "Trajectory capture",
                    extra={"session_id": session_id},
                ) as run:
                    path = build_and_persist_trajectory(
                        session_id=session_id,
                        filtered_jsonl=filtered_jsonl,
                        archive_path=archive_path,
                        insights_text=output or "",
                        context=meta.get("context", ""),
                        project_id=meta.get("project_id", ""),
                        chat_id=meta.get("chat_id", ""),
                        task_summary=meta.get("task_summary", ""),
                        workspace=meta.get("workspace", ""),
                        workspace_root=workspace_root,
                    )
                    if path:
                        run.extra["path"] = str(path)
                    else:
                        run.skip("empty session / no trajectory written")
            except Exception:  # noqa: BLE001 — never crash the loop
                logger.exception(
                    "Trajectory persist failed for session %s", session_id
                )
        # Memory proposals: scan the freshly-appended insights section and
        # write actionable candidates to ``Workspace/Memory-Proposals.md``.
        # "User corrections" are auto-promoted to bounded memory (gated on
        # the config's memory_enabled); everything else waits for the
        # curator agent to promote via `ciao memory` on the next session.
        proposals_vault_root = vault_root or (
            workspace_root / "memory-vault" if workspace_root is not None else None
        )
        if memory_proposals_enabled and proposals_vault_root is not None and output:
            try:
                from ciao.memory_proposals import proposals_from_archive

                with job_runs.track_sync(
                    "memory_proposals", "Memory proposals",
                    extra={"archive": archive_path.name},
                ) as run:
                    wrote = proposals_from_archive(
                        archive_path,
                        proposals_vault_root,
                        auto_promote_memory=bool(
                            getattr(config, "memory_enabled", True)
                        ),
                    )
                    run.extra["wrote"] = bool(wrote)
            except Exception:  # noqa: BLE001 — fire-and-forget, never crash
                logger.exception(
                    "Memory proposals failed for %s", archive_path
                )


def _has_insights_section(path: Path) -> bool:
    try:
        return _INSIGHTS_HEADER in path.read_text(encoding="utf-8")
    except OSError:
        return False


def _append_section(path: Path, body: str) -> None:
    text = body.strip()
    if not text:
        return
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n\n{_INSIGHTS_HEADER}\n\n{text}\n")


def _ollama_env(model: str, settings: OllamaSettings) -> dict[str, str]:
    """Route the insights model to the right upstream.

    Delegates to :func:`ciao.providers.ollama.routine_env_for_model`:
    not gated on the per-chat ``models`` allowlist (the insights model is
    fixed at the server level), local-daemon models go to ``local_url``,
    Anthropic aliases stay on the subscription path with no overrides.
    """
    from ciao.providers.ollama import routine_env_for_model

    return routine_env_for_model(model, settings)


async def _run_model_with_retry(
    *,
    filtered_jsonl: str,
    model: str,
    env: dict[str, str],
    provider: str = "claude",
    cwd: Path | None = None,
) -> str:
    """Call the model; on failure, wait 30s and retry once."""
    async def call() -> str:
        if provider == "claude":
            return await _call_model(filtered_jsonl, model, env)
        return await _call_model(
            filtered_jsonl, model, env, provider=provider, cwd=cwd
        )

    try:
        return await call()
    except Exception as exc:  # noqa: BLE001
        logger.info("Insights model call failed (%s); retrying in %ds", exc, _RETRY_DELAY_S)

    await asyncio.sleep(_RETRY_DELAY_S)
    try:
        return await call()
    except Exception:
        logger.exception("Insights model call failed twice; skipping")
        return ""


async def _call_model(
    filtered_jsonl: str,
    model: str,
    env: dict[str, str],
    *,
    provider: str = "claude",
    cwd: Path | None = None,
) -> str:
    from ciao.providers.oneshot import run_oneshot

    user_prompt = (
        "Below is a coding-agent session transcript as line-oriented JSON.\n"
        "Each line is one message with a numeric `idx` you must cite.\n"
        "Extract durable signal per the system prompt's section schema.\n\n"
        f"{filtered_jsonl}"
    )

    kwargs = {
        "system_prompt": _INSIGHTS_SYSTEM_PROMPT,
        "model": model,
        "env": env,
        "timeout_s": 120.0,
    }
    if provider != "claude":
        kwargs.update({"provider": provider, "cwd": cwd})
    return await run_oneshot(user_prompt, **kwargs)


UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

_TEXT_MODE_SYSTEM_PROMPT = """\
You are extracting durable signal from a Claude Code chat transcript.
The user is the workspace owner. The transcript is a rendered Markdown summary -
tool calls, tool errors, thinking blocks, and intermediate states are
NOT included, only the user/assistant text turns. Adjust accordingly:
sections like Errors, Reusable snippets, and Vault changes will often
be empty. Omit empty sections - do NOT write "none" or "n/a".

Cite by short paraphrase or quote (no `[idx=N]` indices in this mode).
Do not invent facts. Do not summarise the conversation - that is the
transcript itself.

Rules:
- Skip anything obvious from the transcript prose alone.
- "User corrections" = the user pushed back, redirected, or rejected an approach.
- "New entities" = people/projects/places/products mentioned for the first time.
- Be terse. One line per item where possible.

## User corrections
- User said: "<short quote>" -> assistant changed <what>.

## New entities
- <type>: <name> - <one-line context>.

## Decisions
- Chose <X> over <Y> because <reason>.

## Open loops
- <thing left undone, with any deadline or condition>.

## Errors
- <if the transcript itself describes a failure resolution that's worth keeping>

## Reusable snippets
- <only if a fully formed command or query appears in the assistant text>
"""


async def backfill_insights_task(
    config,
    *,
    limit: int = 0,
    mode: str = "both",
    dry_run: bool = False,
    concurrency: int = 2,
    workspace: str = "",
) -> None:
    """Scan the vault's archived transcripts and extract/append missing insights."""
    vault_root = config.vault_root
    base = vault_root / "memory-vault" / "Logs" / "Chats"
    if not base.exists():
        logger.info("Vault directory %s does not exist, skipping backfill", base)
        return

    project_dir = _claude_projects_dir(config.workspace_root)

    todo = []
    # Loop over sorted files to ensure deterministic order (oldest first or alphabetic)
    for md in sorted(base.glob("*/claude/*.md")):
        if _has_insights_section(md):
            continue

        # Filter by workspace/context if requested
        context = md.parent.parent.name
        if workspace and context != workspace:
            continue

        match = UUID_RE.search(md.name)
        session_id = match.group(0) if match else None
        if not session_id:
            continue

        has_jsonl = bool(session_id and (project_dir / f"{session_id}.jsonl").exists())
        
        # Decide if we keep this one based on mode filter
        if has_jsonl and mode in {"both", "full"}:
            todo.append((md, session_id, True))
        elif (not has_jsonl) and mode in {"both", "text"}:
            todo.append((md, session_id, False))

    if limit > 0:
        todo = todo[:limit]

    if not todo:
        logger.info("No archives matching limit=%d, mode=%s, workspace=%s require backfill.", limit, mode, workspace)
        return

    logger.info("Starting backfill for %d archives (dry_run=%s, mode=%s)...", len(todo), dry_run, mode)
    if dry_run:
        for md, _, hj in todo[:20]:
            m = "full" if hj else "text"
            logger.info("  [%s] %s", m, md.relative_to(vault_root))
        if len(todo) > 20:
            logger.info("  ... and %d more", len(todo) - 20)
        return

    sem = asyncio.Semaphore(concurrency)

    async def worker(archive_path: Path, session_id: str, has_jsonl: bool):
        async with sem:
            try:
                insights_model = resolve_insights_model(config)
                if has_jsonl:
                    filtered = filter_session_jsonl(config.workspace_root, session_id)
                    if not filtered:
                        logger.warning("Session JSONL empty or filtered to nothing for %s", archive_path)
                        return
                    await extract_and_append(
                        archive_path=archive_path,
                        filtered_jsonl=filtered,
                        config=config,
                        model=insights_model,
                        session_id=session_id,
                        workspace_root=config.workspace_root,
                        vault_root=config.vault_root,
                        trajectories_enabled=getattr(config, "trajectories_enabled", True),
                    )
                    logger.info("Backfilled [full] insights for %s", archive_path.name)
                else:
                    body = archive_path.read_text(encoding="utf-8")
                    user_prompt = (
                        "Below is a rendered Markdown chat transcript. Tool calls, errors, "
                        "and thinking blocks are not preserved - only user/assistant text. "
                        "Extract durable signal per the system prompt's section schema.\n\n"
                        f"{body}"
                    )
                    from ciao.providers.routing import resolve_with_fallback
                    effective_model, env, note = resolve_with_fallback(
                        insights_model, config, default_model=config.insights_model
                    )

                    async def run_text_extract():
                        from ciao.providers.oneshot import run_oneshot
                        return await run_oneshot(
                            user_prompt,
                            system_prompt=_TEXT_MODE_SYSTEM_PROMPT,
                            model=effective_model,
                            env=env,
                            timeout_s=120.0,
                            cwd=config.workspace_root,
                        )

                    output = ""
                    try:
                        output = await run_text_extract()
                    except Exception as exc:
                        logger.info("Text fallback insights call failed (%s); retrying in %ds", exc, _RETRY_DELAY_S)
                        await asyncio.sleep(_RETRY_DELAY_S)
                        try:
                            output = await run_text_extract()
                        except Exception:
                            logger.exception("Text fallback insights call failed twice; skipping %s", archive_path)

                    if output:
                        _append_section(archive_path, output)
                        logger.info("Backfilled [text] insights for %s", archive_path.name)
            except Exception:
                logger.exception("Failed backfilling insights for %s", archive_path)

    tasks = [worker(md, sid, hj) for md, sid, hj in todo]
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Backfill task completed.")
