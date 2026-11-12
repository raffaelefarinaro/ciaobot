"""Vault answer check: does a turn's final answer match the vault notes it read?

A turn that read vault notes gets one cheap one-shot model call that splits
the answer into claims and labels each one supported, unsupported or general
knowledge. The model is asked for JSON only; the verdict is validated here
because no provider enforces a structure. Any failure records nothing: the
check is advisory and never delays or alters the turn.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CLAIM_STATUSES = ("supported", "unsupported", "general")
MAX_NOTES = 6
MAX_NOTE_CHARS = 6000
MAX_ANSWER_CHARS = 8000
MAX_CLAIMS = 12
VERIFY_TIMEOUT_S = 90.0
_CLAIM_TEXT_CHARS = 300
_READ_TOOLS = frozenset({"Read", "read"})

_SYSTEM_PROMPT = (
    "You check an assistant's answer against the user's own notes. Split the "
    "answer into its distinct factual claims (skip greetings, questions, advice "
    "and opinions). For each claim decide: supported = a note states it or "
    "directly implies it; unsupported = it presents something as coming from or "
    "being true about the user's notes/life/projects but no note supports it, or "
    "a note contradicts it; general = general knowledge not about the user's "
    "notes. Reply with JSON only: {\"claims\": [{\"text\": \"<claim, <= 25 words>\", "
    "\"status\": \"supported|unsupported|general\", \"source\": \"<note path for "
    "supported/unsupported, else empty>\"}]}. At most 12 claims."
)


def vault_read_path(
    tool_name: str, tool_input: str, *, vault_root: Path, agent_root: Path
) -> Path | None:
    """The resolved path of a Read of a markdown note inside the vault, else None."""
    if tool_name not in _READ_TOOLS or not tool_input:
        return None
    raw = Path(tool_input).expanduser()
    path = raw if raw.is_absolute() else agent_root / raw
    resolved = path.resolve(strict=False)
    if resolved.suffix.lower() != ".md":
        return None
    if not resolved.is_relative_to(vault_root.resolve()):
        return None
    return resolved


def build_verification_prompt(answer: str, notes: list[tuple[str, str]]) -> str:
    """The user prompt: the answer, then each note under its vault-relative path."""
    parts = ["## Answer\n", answer[:MAX_ANSWER_CHARS], "\n\n## Notes\n"]
    for rel, text in notes:
        parts.append(f"### {rel}\n{text}\n\n")
    return "".join(parts)


def parse_verification(text: str, known_sources: set[str]) -> dict | None:
    """Validate the model's JSON verdict; None when no valid claim remains."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        return None

    claims: list[dict] = []
    for raw in data["claims"]:
        if not isinstance(raw, dict):
            continue
        claim_text = str(raw.get("text") or "").strip()[:_CLAIM_TEXT_CHARS]
        status = raw.get("status")
        if not claim_text or status not in CLAIM_STATUSES:
            continue
        source = str(raw.get("source") or "").strip()
        if status == "general" or source not in known_sources:
            source = ""
        claims.append({"text": claim_text, "status": status, "source": source})
        if len(claims) == MAX_CLAIMS:
            break
    if not claims:
        return None
    return {
        **{s: sum(1 for c in claims if c["status"] == s) for s in CLAIM_STATUSES},
        "claims": claims,
        "sources": sorted(known_sources),
    }


async def verify_answer(
    answer: str,
    note_paths: list[Path],
    *,
    vault_root: Path,
    model: str,
    provider: str,
    cwd: Path | None,
) -> dict | None:
    """Check the answer's claims against the notes on disk; None on any failure."""
    # Function-local import: the provider layer imports back into the web layer.
    from ciao.providers.oneshot import run_oneshot

    root = vault_root.resolve()
    notes: list[tuple[str, str]] = []
    seen: set[Path] = set()
    for path in note_paths:
        if len(notes) == MAX_NOTES:
            break
        resolved = path.resolve(strict=False)
        if resolved in seen or not resolved.is_relative_to(root) or not resolved.is_file():
            continue
        seen.add(resolved)
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        notes.append((resolved.relative_to(root).as_posix(), text[:MAX_NOTE_CHARS]))
    if not notes:
        return None

    prompt = build_verification_prompt(answer, notes)
    try:
        reply = await run_oneshot(
            prompt,
            system_prompt=_SYSTEM_PROMPT,
            model=model,
            provider=provider,
            timeout_s=VERIFY_TIMEOUT_S,
            cwd=cwd,
        )
    except Exception:  # noqa: BLE001 — the check is advisory; any failure records nothing
        logger.info("Vault answer check failed", exc_info=True)
        return None
    return parse_verification(reply, {rel for rel, _ in notes})
