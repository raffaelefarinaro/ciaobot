"""Agent-mode insights extraction: the same task, with the vault in reach.

The one-shot extractor sees only what code pastes into its prompt — the
transcript plus a roster of known people and projects — so it cannot check
whether a note already says the thing it is about to tag. This module runs
the same extraction as a read-only agent: it may Read/Grep/Glob the vault
before answering, and it answers in the identical insights schema, so the
only thing a comparison between the two measures is extraction quality.

The agent writes nothing. :func:`run_agent_extraction` copies the archive
body into the vault, hands the model a path, and deletes the copy; the
insights it produces are returned as text for a caller to route, never
applied.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ciao.insights import _TEXT_MODE_SYSTEM_PROMPT
from ciao.providers.oneshot import AgentRunResult, run_readonly_agent

# Where the transcript copy lives. Inside the vault on purpose: the read-only
# gate allows one root, and a second allowed root is a second thing to keep
# correct.
_TMP_DIRNAME = ".ciao-tmp"


_AGENT_ADDENDUM = """
You have read tools over the vault, which is your working directory. Use them
before you answer — this is the one thing you can do that a pasted-in roster
cannot do:

- The chat transcript is a file in the vault (its path is in the prompt). Read
  it fully before answering; the prompt does not paste its contents.
- Before you tag a bullet `[people: X]` or `[project: X]`, or list X under
  "New entities", check whether a note for X already exists — `Glob
  People/*.md` and `Grep` the name — and, if it does, use the name exactly as
  the note spells it.
- Read that note and omit any fact it already states. A fact a note already
  carries is not a new fact, however true it is.
- Prefer routing a fact into an existing note over inventing a new entity. A
  New entry for something the vault already has fragments the memory.
- Start investigating from `INDEX.md`, which lists what is there, rather than
  walking the tree.
- Stop investigating after about 15 tool calls. Past that you are re-reading
  what you have already seen, and the answer is waiting to be written.

Your final message is only the insights sections, in the schema above. No
preamble, no summary of what you read, no mention of the tools.
"""

_OPENCODE_TOOLS_NOTE = """
Only the read tool is available here (no glob, no grep). Every path you pass
must be absolute. Start by reading {vault}/INDEX.md to find the notes, then
read the ones you need by path.
"""


AGENT_SYSTEM_PROMPT = _TEXT_MODE_SYSTEM_PROMPT + _AGENT_ADDENDUM


async def run_agent_extraction(
    archive_path: Path,
    *,
    vault_root: Path,
    guide_path: Path | None,
    model: str,
    provider: str = "claude",
) -> AgentRunResult:
    """Extract insights from an archived chat with a read-only agent.

    The archive's rendered body is copied into the vault under
    ``.ciao-tmp/`` and named in the prompt rather than pasted, so the agent
    can read it with the same tool it uses for everything else. The copy is
    removed afterwards whatever happens.
    """
    # Imported here, not at module scope: `insights_compare` imports back into
    # this module's caller, and `insights._call_text_model` is a monkeypatch
    # point the comparison's tests rely on. Both resolve at call time that way.
    from ciao.insights import _known_context_block
    from ciao.insights_compare import strip_insights

    body = strip_insights(archive_path.read_text(encoding="utf-8"))
    tmp_dir = vault_root / _TMP_DIRNAME
    tmp_dir.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, dir=tmp_dir,
        prefix="insights-transcript-", encoding="utf-8",
    )
    transcript_path = Path(handle.name)
    try:
        with handle:
            handle.write(body)
        vault = vault_root.resolve()
        system = AGENT_SYSTEM_PROMPT
        if provider == "opencode":
            # Claiming a Glob the V2 ruleset will deny costs a turn per
            # attempt; say what is actually reachable instead.
            system += _OPENCODE_TOOLS_NOTE.format(vault=vault)
        prompt = (
            f"The vault root is: {vault}\n\n"
            + _known_context_block(guide_path, vault_root, transcript=body)
            + f"The chat transcript (rendered Markdown) is at: {transcript_path}\n"
            "Read it fully before answering."
        )
        return await run_readonly_agent(
            prompt,
            system_prompt=system,
            model=model,
            cwd=vault_root,
            allowed_roots=[vault_root],
            provider=provider,
        )
    finally:
        transcript_path.unlink(missing_ok=True)
